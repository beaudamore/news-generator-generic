"""Make sure a ComfyUI host has every model file a workflow template needs.

Three steps, each usable on its own:

1. `workflow_model_refs(workflow)` reads an API-format template and returns every input
   whose value names a model file (``ckpt_name``, ``unet_name``, ``lora_name``, ...).
2. `audit(base_url, workflow_path)` asks that ComfyUI host, through ``GET /object_info/{class}``,
   which files each loader node currently offers, and marks every reference as installed or
   missing. A missing file is looked up in the manifest ``comfyui-workflows/models.json`` for
   its download URL and target folder.
3. `ensure(base_url, workflow_path, download=True)` runs the audit and asks the host to
   download what is missing, through the news-generator helper node
   (``comfyui-helper-node/``), or through ComfyUI-Manager when the helper is absent and
   ``COMFYUI_MODEL_DOWNLOADER`` allows it.

The manifest is the only place download URLs live: ComfyUI's API-format export drops the
``properties.models`` block the UI export carries, so the repository keeps that information
beside the templates. `check_manifest()` reports every template file reference that has no
manifest entry, so adding a template without its manifest rows is caught early.

Environment:

    COMFYUI_MODELS_MANIFEST          path to the manifest (default comfyui-workflows/models.json)
    COMFYUI_MODEL_DOWNLOADER         auto (helper, then Manager), helper, manager, or none
    COMFYUI_AUTO_DOWNLOAD_MODELS     true: preflight starts downloads for missing models
    COMFYUI_HF_TOKEN                 Hugging Face token passed with each download request
    COMFYUI_CIVITAI_TOKEN            Civitai token passed with each download request
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from services.errors import ApiError

log = logging.getLogger("news_pipeline")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = PROJECT_ROOT / "comfyui-workflows"
DEFAULT_MANIFEST = WORKFLOWS_DIR / "models.json"

MODEL_EXTENSIONS = (".safetensors", ".sft", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".onnx")
# Input names that carry a model file in core and common custom loader nodes. Values on other
# inputs are still picked up when they end with a model extension; this list only documents intent.
MODEL_INPUT_NAMES = {
    "ckpt_name", "unet_name", "clip_name", "clip_name1", "clip_name2", "clip_name3", "vae_name",
    "lora_name", "model_name", "text_encoder", "audio_encoder_name", "control_net_name",
    "style_model_name", "upscale_model_name", "clip_vision_name", "gligen_name", "embedding_name",
}
# Where a loader input's files live when the manifest has no entry (reporting only; downloads need
# the manifest's folder). Keys are (class_type, input) with a wildcard fallback on input alone.
DEFAULT_FOLDERS: dict[str, str] = {
    "ckpt_name": "checkpoints",
    "unet_name": "diffusion_models",
    "clip_name": "text_encoders",
    "clip_name1": "text_encoders",
    "clip_name2": "text_encoders",
    "clip_name3": "text_encoders",
    "text_encoder": "text_encoders",
    "vae_name": "vae",
    "lora_name": "loras",
    "audio_encoder_name": "audio_encoders",
    "control_net_name": "controlnet",
    "clip_vision_name": "clip_vision",
    "upscale_model_name": "upscale_models",
}
CLASS_FOLDERS: dict[tuple[str, str], str] = {
    ("LatentUpscaleModelLoader", "model_name"): "latent_upscale_models",
    ("UpscaleModelLoader", "model_name"): "upscale_models",
    ("LTXAVTextEncoderLoader", "ckpt_name"): "checkpoints",
    ("LTXVAudioVAELoader", "ckpt_name"): "checkpoints",
}

OBJECT_INFO_CACHE_SECONDS = 60
_object_info_cache: dict[tuple[str, str], tuple[float, Optional[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def _requests():
    return importlib.import_module("requests")


def _timeout() -> float:
    return float(os.environ.get("PREFLIGHT_TIMEOUT_SECONDS", "5") or 5)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------------------- manifest


def manifest_path() -> Path:
    value = _env("COMFYUI_MODELS_MANIFEST")
    path = Path(value).expanduser() if value else DEFAULT_MANIFEST
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def load_manifest(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """{filename: {"url", "folder", "sha256"?, "size_bytes"?, "gated"?, "source"?, "note"?}}; an absent file is an empty manifest."""
    path = path or manifest_path()
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        raise ApiError("invalid", f"{path}: expected an object with a 'models' map")
    for name, entry in models.items():
        if not isinstance(entry, dict) or not entry.get("folder"):
            raise ApiError("invalid", f"{path}: entry {name!r} needs at least a 'folder'")
    return models


def manifest_entry(filename: str, manifest: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Exact filename match first, then the basename (templates may reference a subfolder/file)."""
    if filename in manifest:
        return manifest[filename]
    base = Path(filename).name
    return manifest.get(base)


# ----------------------------------------------------------------------------- template scan


def is_model_filename(value: Any) -> bool:
    return isinstance(value, str) and value.lower().endswith(MODEL_EXTENSIONS)


def default_folder(class_type: str, input_name: str) -> Optional[str]:
    return CLASS_FOLDERS.get((class_type, input_name)) or DEFAULT_FOLDERS.get(input_name)


def workflow_model_refs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Every (node, input) in an API-format template whose value names a model file, in node order.

    Returns [{"node_id", "class_type", "input", "filename"}]. UI exports (nodes/links) are refused
    because their widget values carry no input names.
    """
    if "nodes" in workflow and "links" in workflow:
        raise ApiError("invalid", "this is a ComfyUI UI export (nodes/links); export it in API format")
    refs: list[dict[str, Any]] = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        for input_name, value in (node.get("inputs") or {}).items():
            if is_model_filename(value) or (input_name in MODEL_INPUT_NAMES and isinstance(value, str) and value and not value.startswith("{{")):
                refs.append({"node_id": str(node_id), "class_type": class_type, "input": input_name, "filename": value})
    return refs


def workflow_files(include_avatar: bool = True) -> list[Path]:
    paths = sorted(WORKFLOWS_DIR.glob("*.json"))
    if include_avatar:
        paths += sorted((WORKFLOWS_DIR / "avatar").glob("*.json"))
    return [p for p in paths if p.name != DEFAULT_MANIFEST.name]


def check_manifest(manifest: Optional[dict[str, dict[str, Any]]] = None) -> dict[str, Any]:
    """Which model references across every template have a manifest entry, and which do not."""
    manifest = load_manifest() if manifest is None else manifest
    covered: dict[str, list[str]] = {}
    uncovered: dict[str, list[str]] = {}
    for path in workflow_files():
        workflow = json.loads(path.read_text(encoding="utf-8"))
        for ref in workflow_model_refs(workflow):
            bucket = covered if manifest_entry(ref["filename"], manifest) else uncovered
            bucket.setdefault(ref["filename"], [])
            if path.name not in bucket[ref["filename"]]:
                bucket[ref["filename"]].append(path.name)
    unused = sorted(set(manifest) - set(covered) - set(uncovered))
    return {"manifest": str(manifest_path()), "covered": covered, "uncovered": uncovered, "unused": unused, "ok": not uncovered}


# ----------------------------------------------------------------------------- host queries


def object_info(base_url: str, class_type: str, use_cache: bool = True) -> Optional[dict[str, Any]]:
    """The host's node definition for one class, or None when the host does not have that node."""
    key = (base_url.rstrip("/"), class_type)
    now = time.time()
    if use_cache:
        with _cache_lock:
            hit = _object_info_cache.get(key)
        if hit and now - hit[0] < OBJECT_INFO_CACHE_SECONDS:
            return hit[1]
    response = _requests().get(f"{key[0]}/object_info/{class_type}", timeout=_timeout())
    if response.status_code == 404:
        info = None
    else:
        response.raise_for_status()
        payload = response.json()
        info = payload.get(class_type) if isinstance(payload, dict) else None
        if not info:  # ComfyUI answers {} for an unknown class
            info = None
    with _cache_lock:
        _object_info_cache[key] = (now, info)
    return info


def _input_options(info: dict[str, Any], input_name: str) -> Optional[list[str]]:
    """The combo choices of one input, or None when the input is not a combo (free text, link)."""
    for section in ("required", "optional"):
        spec = (info.get("input") or {}).get(section, {}).get(input_name)
        if spec is None:
            continue
        if isinstance(spec, (list, tuple)) and spec:
            first = spec[0]
            if isinstance(first, list):
                return [str(v) for v in first]
            if isinstance(first, dict) and isinstance(first.get("options"), list):  # newer COMBO shape
                return [str(v) for v in first["options"]]
            if first == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict) and isinstance(spec[1].get("options"), list):
                return [str(v) for v in spec[1]["options"]]
        return None
    return None


def host_files(base_url: str, folder: str) -> Optional[list[str]]:
    """Files ComfyUI lists for a models folder (GET /models/{folder}); None when the folder is unknown there."""
    response = _requests().get(f"{base_url.rstrip('/')}/models/{folder}", timeout=_timeout())
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json()
    return [str(v) for v in data] if isinstance(data, list) else None


def clear_cache() -> None:
    with _cache_lock:
        _object_info_cache.clear()


# ----------------------------------------------------------------------------- audit


def _load_workflow(workflow_path: Path) -> dict[str, Any]:
    if not workflow_path.is_file():
        raise ApiError("not_found", f"workflow template {workflow_path}")
    return json.loads(workflow_path.read_text(encoding="utf-8"))


def audit(base_url: str, workflow_path: Path, manifest: Optional[dict[str, dict[str, Any]]] = None, use_cache: bool = True) -> dict[str, Any]:
    """Which model files the template needs, and whether the host has each one.

    Result: {"base_url", "workflow", "models": [{"filename", "folder", "installed", "in_manifest", "url",
    "nodes": [{"node_id", "class_type", "input"}], "class_missing": bool}], "missing": [filenames],
    "downloadable": [filenames], "unknown": [filenames needing a manifest row], "missing_classes": [...], "ok": bool}
    """
    base = base_url.rstrip("/")
    manifest = load_manifest() if manifest is None else manifest
    workflow = _load_workflow(workflow_path)
    by_file: dict[str, dict[str, Any]] = {}
    missing_classes: list[str] = []
    folder_lists: dict[str, Optional[list[str]]] = {}
    for ref in workflow_model_refs(workflow):
        entry = manifest_entry(ref["filename"], manifest) or {}
        folder = entry.get("folder") or default_folder(ref["class_type"], ref["input"])
        info = object_info(base, ref["class_type"], use_cache=use_cache)
        class_missing = info is None
        if class_missing and ref["class_type"] not in missing_classes:
            missing_classes.append(ref["class_type"])
        installed: Optional[bool] = None
        options = None if info is None else _input_options(info, ref["input"])
        if options is not None:
            installed = ref["filename"] in options or Path(ref["filename"]).name in {Path(o).name for o in options}
        elif folder:
            # The node is missing or the input is not a combo: fall back to the folder listing.
            if folder not in folder_lists:
                try:
                    folder_lists[folder] = host_files(base, folder)
                except Exception:  # noqa: BLE001
                    folder_lists[folder] = None
            files = folder_lists[folder]
            if files is not None:
                installed = ref["filename"] in files or Path(ref["filename"]).name in {Path(f).name for f in files}
        record = by_file.setdefault(ref["filename"], {
            "filename": ref["filename"],
            "folder": folder,
            "installed": installed,
            "in_manifest": bool(entry),
            "url": entry.get("url"),
            "sha256": entry.get("sha256"),
            "size_bytes": entry.get("size_bytes"),
            "gated": bool(entry.get("gated")),
            "nodes": [],
            "class_missing": class_missing,
        })
        record["nodes"].append({"node_id": ref["node_id"], "class_type": ref["class_type"], "input": ref["input"]})
        if installed is not None and record["installed"] is None:
            record["installed"] = installed
        if installed is False:
            record["installed"] = False
        record["class_missing"] = record["class_missing"] or class_missing
    models = list(by_file.values())
    missing = [m["filename"] for m in models if m["installed"] is False]
    unverified = [m["filename"] for m in models if m["installed"] is None]
    downloadable = [m["filename"] for m in models if m["installed"] is False and m["in_manifest"] and m.get("url")]
    unknown = [m["filename"] for m in models if m["installed"] is False and not (m["in_manifest"] and m.get("url"))]
    return {
        "base_url": base,
        "workflow": str(workflow_path),
        "models": models,
        "missing": missing,
        "unverified": unverified,
        "downloadable": downloadable,
        "unknown": unknown,
        "missing_classes": missing_classes,
        "ok": not missing and not missing_classes,
    }


# ----------------------------------------------------------------------------- downloaders


def helper_info(base_url: str) -> Optional[dict[str, Any]]:
    """The helper node's identity when it is installed on the host, else None."""
    try:
        response = _requests().get(f"{base_url.rstrip('/')}/news-generator/helper", timeout=_timeout())
    except Exception:  # noqa: BLE001
        return None
    if not response.ok:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("name") == "news-generator-helper" else None


def _token_for(url: str) -> Optional[str]:
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    if host.endswith("huggingface.co") or host.endswith("hf.co"):
        return _env("COMFYUI_HF_TOKEN") or None
    if host.endswith("civitai.com"):
        return _env("COMFYUI_CIVITAI_TOKEN") or None
    return None


def helper_download(base_url: str, model: dict[str, Any]) -> dict[str, Any]:
    """Ask the helper node to fetch one manifest model; returns the helper's job record."""
    body = {"url": model["url"], "folder": model["folder"], "filename": Path(model["filename"]).name}
    subfolder = str(Path(model["filename"]).parent)
    if subfolder not in ("", "."):
        body["subfolder"] = subfolder
    if model.get("sha256"):
        body["sha256"] = model["sha256"]
    token = _token_for(model["url"])
    if token:
        body["token"] = token
    response = _requests().post(f"{base_url.rstrip('/')}/news-generator/models/download", json=body, timeout=max(_timeout(), 30))
    if not response.ok:
        detail = response.text[:300]
        try:
            detail = response.json().get("error", detail)
        except ValueError:
            pass
        raise ApiError("service_unavailable", f"helper refused download of {model['filename']}: {response.status_code} {detail}")
    return response.json().get("job") or {}


def helper_jobs(base_url: str) -> list[dict[str, Any]]:
    response = _requests().get(f"{base_url.rstrip('/')}/news-generator/models/jobs", timeout=_timeout())
    response.raise_for_status()
    return list(response.json().get("jobs") or [])


def manager_available(base_url: str) -> bool:
    try:
        response = _requests().get(f"{base_url.rstrip('/')}/v2/manager/version", timeout=_timeout())
        return response.ok
    except Exception:  # noqa: BLE001
        return False


def manager_download(base_url: str, model: dict[str, Any]) -> dict[str, Any]:
    """Queue one download through ComfyUI-Manager's model queue (best effort; its routes are undocumented).

    Manager expects the fields of its model-list.json entries. `save_path` is the models/ subfolder;
    `filename` the target name; `url` the direct link. The queue is started right after.
    """
    base = base_url.rstrip("/")
    payload = {
        "name": Path(model["filename"]).name,
        "type": model["folder"],
        "base": model.get("base") or "",
        "save_path": model["folder"],
        "description": "news-generator",
        "reference": model["url"],
        "filename": Path(model["filename"]).name,
        "url": model["url"],
    }
    requests = _requests()
    response = requests.post(f"{base}/v2/manager/queue/install_model", json=payload, timeout=max(_timeout(), 30))
    if not response.ok:
        raise ApiError("service_unavailable", f"ComfyUI-Manager refused download of {model['filename']}: {response.status_code} {response.text[:300]}")
    requests.get(f"{base}/v2/manager/queue/start", timeout=max(_timeout(), 30))
    return {"status": "queued", "via": "manager", "filename": model["filename"], "url": model["url"]}


def downloader_for(base_url: str) -> Optional[str]:
    """Which download route the host offers under COMFYUI_MODEL_DOWNLOADER: "helper", "manager", or None."""
    mode = (_env("COMFYUI_MODEL_DOWNLOADER") or "auto").lower()
    if mode == "none":
        return None
    if mode in ("auto", "helper") and helper_info(base_url):
        return "helper"
    if mode in ("auto", "manager") and manager_available(base_url):
        return "manager"
    return None


# ----------------------------------------------------------------------------- ensure


def ensure(base_url: str, workflow_path: Path, download: bool = True, manifest: Optional[dict[str, dict[str, Any]]] = None) -> dict[str, Any]:
    """Audit the template against the host and, when asked, start downloads for every missing manifest model.

    Adds to the audit result: "downloader" (helper, manager, or null), "started": [job records],
    "errors": [{"filename", "message"}]. Downloads are asynchronous on the host; poll `jobs()`.
    """
    result = audit(base_url, workflow_path, manifest=manifest, use_cache=False)
    result["downloader"] = None
    result["started"] = []
    result["errors"] = []
    if not download or not result["downloadable"]:
        if result["unknown"]:
            result["errors"] = [{"filename": f, "message": "no download URL in the manifest"} for f in result["unknown"]]
        return result
    downloader = downloader_for(base_url)
    result["downloader"] = downloader
    if downloader is None:
        result["errors"].append({"filename": None, "message": "host has neither the news-generator helper node nor ComfyUI-Manager (or COMFYUI_MODEL_DOWNLOADER=none); install comfyui-helper-node/ on it"})
        return result
    for model in result["models"]:
        if model["filename"] not in result["downloadable"]:
            continue
        try:
            job = helper_download(base_url, model) if downloader == "helper" else manager_download(base_url, model)
            result["started"].append(job)
            log.info("[models] %s: download of %s started via %s", base_url, model["filename"], downloader)
        except ApiError as exc:
            result["errors"].append({"filename": model["filename"], "message": exc.message})
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({"filename": model["filename"], "message": f"{type(exc).__name__}: {exc}"})
    for f in result["unknown"]:
        result["errors"].append({"filename": f, "message": "no download URL in the manifest"})
    clear_cache()
    return result


def jobs(base_url: str) -> dict[str, Any]:
    """Download jobs the host reports. The helper gives per-job progress; Manager only its queue state."""
    base = base_url.rstrip("/")
    if helper_info(base):
        return {"base_url": base, "downloader": "helper", "jobs": helper_jobs(base)}
    if manager_available(base):
        response = _requests().get(f"{base}/v2/manager/queue/status", timeout=_timeout())
        return {"base_url": base, "downloader": "manager", "queue": response.json() if response.ok else None, "jobs": []}
    return {"base_url": base, "downloader": None, "jobs": []}


# ----------------------------------------------------------------------------- configured hosts


def resolve_workflow(value: str, avatar: bool = False) -> Path:
    """A template reference from .env or a profile: absolute, project-relative, or a bare name under comfyui-workflows/avatar/."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    if avatar and "/" not in value and "\\" not in value:
        candidate = WORKFLOWS_DIR / "avatar" / value
        if candidate.is_file():
            return candidate
    return PROJECT_ROOT / path


def configured_targets(host: Optional[str] = None, workflow: Optional[str] = None) -> list[dict[str, Any]]:
    """(host name, base URL, template) triples the app is configured to use.

    host: "video", "avatar", or None for both. workflow: one template to check instead of the
    configured ones (resolved against comfyui-workflows/ and comfyui-workflows/avatar/).
    """
    targets: list[dict[str, Any]] = []
    video_base = _env("VIDEO_COMFYUI_BASE_URL")
    avatar_base = _env("AVATAR_COMFYUI_BASE_URL")
    if host in (None, "video") and video_base:
        if workflow:
            targets.append({"host": "video", "base_url": video_base, "workflow": resolve_workflow(workflow)})
        elif _env("VIDEO_COMFYUI_WORKFLOW"):
            targets.append({"host": "video", "base_url": video_base, "workflow": resolve_workflow(_env("VIDEO_COMFYUI_WORKFLOW"))})
    if host in (None, "avatar") and avatar_base:
        if workflow:
            targets.append({"host": "avatar", "base_url": avatar_base, "workflow": resolve_workflow(workflow, avatar=True)})
        else:
            seen: set[Path] = set()
            for name in (_env("AVATAR_COMFYUI_WORKFLOW"), _env("HERO_COMFYUI_WORKFLOW")):
                if not name:
                    continue
                path = resolve_workflow(name, avatar=True)
                if path not in seen:
                    seen.add(path)
                    targets.append({"host": "avatar", "base_url": avatar_base, "workflow": path})
    if host not in (None, "video", "avatar"):
        raise ApiError("invalid", "host must be 'video' or 'avatar'")
    return targets


def audit_configured(host: Optional[str] = None, workflow: Optional[str] = None) -> dict[str, Any]:
    manifest = load_manifest()
    results = [{**_safe(lambda t=t: audit(t["base_url"], t["workflow"], manifest=manifest, use_cache=False)), **t, "workflow": str(t["workflow"])} for t in configured_targets(host, workflow)]
    return {"targets": results, "ok": all(r.get("ok") for r in results)}


def ensure_configured(host: Optional[str] = None, workflow: Optional[str] = None, download: bool = True) -> dict[str, Any]:
    manifest = load_manifest()
    results = [{**_safe(lambda t=t: ensure(t["base_url"], t["workflow"], download=download, manifest=manifest)), **t, "workflow": str(t["workflow"])} for t in configured_targets(host, workflow)]
    return {"targets": results, "ok": all(r.get("ok") for r in results)}


def jobs_configured(host: Optional[str] = None) -> dict[str, Any]:
    hosts: dict[str, str] = {}
    if host in (None, "video") and _env("VIDEO_COMFYUI_BASE_URL"):
        hosts["video"] = _env("VIDEO_COMFYUI_BASE_URL")
    if host in (None, "avatar") and _env("AVATAR_COMFYUI_BASE_URL"):
        hosts["avatar"] = _env("AVATAR_COMFYUI_BASE_URL")
    return {"hosts": [{**_safe(lambda url=url: jobs(url)), "host": name, "base_url": url} for name, url in hosts.items()]}


def _safe(fn) -> dict[str, Any]:
    try:
        return fn()
    except ApiError as exc:
        return {"ok": False, "error": exc.to_dict()["error"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": {"code": "service_unavailable", "stage": None, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}}


# ----------------------------------------------------------------------------- preflight hook


def preflight_detail(base_url: str, workflow_path: Path) -> tuple[str, str]:
    """(status, detail) for the preflight `comfyui_models` check: fail when the template names a file the host lacks.

    With COMFYUI_AUTO_DOWNLOAD_MODELS=true the missing manifest models are queued for download
    on the host before the check fails, so the next run can find them in place.
    """
    result = audit(base_url, workflow_path, use_cache=False)
    if result["ok"]:
        extra = f"; {len(result['unverified'])} unverified (not a combo input)" if result["unverified"] else ""
        return "ok", f"{len(result['models'])} model files present on {base_url}{extra}"
    parts: list[str] = []
    if result["missing_classes"]:
        parts.append(f"node classes not installed: {result['missing_classes']}")
    if result["missing"]:
        parts.append(f"missing model files: {result['missing']}")
    if result["missing"] and _flag("COMFYUI_AUTO_DOWNLOAD_MODELS"):
        ensured = ensure(base_url, workflow_path, download=True)
        if ensured["started"]:
            names = [j.get("filename") for j in ensured["started"]]
            parts.append(f"download started via {ensured['downloader']} for {names}; retry once they finish")
        for err in ensured["errors"]:
            parts.append(f"{err['filename'] or 'download'}: {err['message']}")
    elif result["unknown"]:
        parts.append(f"no manifest entry (add to {manifest_path().name}): {result['unknown']}")
    return "fail", "; ".join(parts)
