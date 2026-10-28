"""Unit tests for services/comfyui_models.py: template scan, manifest coverage, host audit and ensure.

No ComfyUI host is needed: HTTP goes through a fake `requests` module that answers from a dict.
Run: python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import comfyui_models as cm  # noqa: E402
from services.errors import ApiError  # noqa: E402

HOST = "http://comfy.test:8188"


class FakeResponse:
    def __init__(self, status: int, body: Any = None, text: str = "") -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    """Answers GET/POST from `routes`: {("GET", "/path"): body-or-FakeResponse}. Records POSTs."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.posts: list[tuple[str, Any]] = []
        self.gets: list[str] = []

    def _lookup(self, method: str, url: str) -> FakeResponse:
        path = url.split("//", 1)[-1].split("/", 1)[1] if "/" in url.split("//", 1)[-1] else ""
        hit = self.routes.get((method, "/" + path))
        if hit is None:
            return FakeResponse(404, {})
        return hit if isinstance(hit, FakeResponse) else FakeResponse(200, hit)

    def get(self, url: str, **_: Any) -> FakeResponse:
        self.gets.append(url)
        return self._lookup("GET", url)

    def post(self, url: str, json: Any = None, **_: Any) -> FakeResponse:  # noqa: A002
        self.posts.append((url, json))
        return self._lookup("POST", url)


def combo(*options: str) -> list[Any]:
    return [list(options), {}]


def object_info_routes(installed: dict[str, list[str]]) -> dict[tuple[str, str], Any]:
    """object_info for the three loader classes of the test template, with the given files installed."""
    return {
        ("GET", "/object_info/CheckpointLoaderSimple"): {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": combo(*installed.get("checkpoints", []))}}}},
        ("GET", "/object_info/LoraLoaderModelOnly"): {"LoraLoaderModelOnly": {"input": {"required": {"model": ["MODEL"], "lora_name": combo(*installed.get("loras", []))}}}},
        ("GET", "/object_info/VAELoader"): {"VAELoader": {"input": {"required": {"vae_name": combo(*installed.get("vae", []))}}}},
    }


TEMPLATE = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "big-model-fp8.safetensors"}},
    "2": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "speed.safetensors", "strength_model": 1.0}},
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "video_vae.safetensors"}},
    "4": {"class_type": "LoadImage", "inputs": {"image": "{{FIRST_FRAME_IMAGE}}"}},
    "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{PROMPT}}", "clip": ["1", 1]}},
}

MANIFEST = {
    "big-model-fp8.safetensors": {"url": "https://huggingface.co/org/repo/resolve/main/big-model-fp8.safetensors", "folder": "checkpoints", "sha256": "a" * 64},
    "speed.safetensors": {"url": "https://huggingface.co/org/repo/resolve/main/loras/speed.safetensors", "folder": "loras"},
    # video_vae.safetensors deliberately absent: the "unknown" case
}


@pytest.fixture
def template_path(tmp_path: Path) -> Path:
    path = tmp_path / "video_test.json"
    path.write_text(json.dumps(TEMPLATE), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cm.clear_cache()


def use(monkeypatch: pytest.MonkeyPatch, fake: FakeRequests) -> FakeRequests:
    monkeypatch.setattr(cm, "_requests", lambda: fake)
    return fake


# ----------------------------------------------------------------------------- template scan


def test_workflow_model_refs_finds_loader_inputs_only() -> None:
    refs = cm.workflow_model_refs(TEMPLATE)
    assert [(r["node_id"], r["input"], r["filename"]) for r in refs] == [
        ("1", "ckpt_name", "big-model-fp8.safetensors"),
        ("2", "lora_name", "speed.safetensors"),
        ("3", "vae_name", "video_vae.safetensors"),
    ]


def test_workflow_model_refs_refuses_ui_export() -> None:
    with pytest.raises(ApiError) as exc:
        cm.workflow_model_refs({"nodes": [], "links": []})
    assert exc.value.code == "invalid"


def test_default_folder_prefers_class_specific_mapping() -> None:
    assert cm.default_folder("LatentUpscaleModelLoader", "model_name") == "latent_upscale_models"
    assert cm.default_folder("UpscaleModelLoader", "model_name") == "upscale_models"
    assert cm.default_folder("UNETLoader", "unet_name") == "diffusion_models"
    assert cm.default_folder("SomethingElse", "no_such_input") is None


def test_every_repository_template_reference_has_a_manifest_entry() -> None:
    """Adding a template means adding its model rows to comfyui-workflows/models.json."""
    report = cm.check_manifest()
    assert report["uncovered"] == {}, f"templates reference files without a manifest entry: {report['uncovered']}"


def test_repository_manifest_entries_are_well_formed() -> None:
    manifest = cm.load_manifest()
    assert manifest, "comfyui-workflows/models.json is empty"
    for name, entry in manifest.items():
        assert entry.get("folder"), f"{name}: no folder"
        url = entry.get("url")
        assert url is None or url.startswith("https://"), f"{name}: url must be https"
        sha = entry.get("sha256")
        assert sha is None or (len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)), f"{name}: sha256 must be 64 lowercase hex"


# ----------------------------------------------------------------------------- audit


def test_audit_marks_installed_and_missing_from_object_info(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    fake = use(monkeypatch, FakeRequests(object_info_routes({"checkpoints": ["big-model-fp8.safetensors"], "loras": [], "vae": ["video_vae.safetensors"]})))
    result = cm.audit(HOST, template_path, manifest=MANIFEST)
    by_name = {m["filename"]: m for m in result["models"]}
    assert by_name["big-model-fp8.safetensors"]["installed"] is True
    assert by_name["speed.safetensors"]["installed"] is False
    assert by_name["speed.safetensors"]["folder"] == "loras"
    assert by_name["speed.safetensors"]["url"].endswith("speed.safetensors")
    assert by_name["video_vae.safetensors"]["installed"] is True
    assert result["missing"] == ["speed.safetensors"]
    assert result["downloadable"] == ["speed.safetensors"]
    assert result["unknown"] == []
    assert result["ok"] is False
    assert all("/object_info/" in u for u in fake.gets)


def test_audit_reports_files_missing_without_manifest_entry_as_unknown(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    use(monkeypatch, FakeRequests(object_info_routes({"checkpoints": ["big-model-fp8.safetensors"], "loras": ["speed.safetensors"], "vae": []})))
    result = cm.audit(HOST, template_path, manifest=MANIFEST)
    assert result["missing"] == ["video_vae.safetensors"]
    assert result["downloadable"] == []
    assert result["unknown"] == ["video_vae.safetensors"]
    assert {m["filename"]: m["folder"] for m in result["models"]}["video_vae.safetensors"] == "vae"  # heuristic folder


def test_audit_matches_file_inside_a_subfolder_option(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    use(monkeypatch, FakeRequests(object_info_routes({"checkpoints": ["ltx/big-model-fp8.safetensors"], "loras": ["speed.safetensors"], "vae": ["video_vae.safetensors"]})))
    result = cm.audit(HOST, template_path, manifest=MANIFEST)
    assert result["ok"] is True


def test_audit_reports_uninstalled_node_class_and_falls_back_to_folder_listing(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    routes = object_info_routes({"checkpoints": ["big-model-fp8.safetensors"], "vae": ["video_vae.safetensors"]})
    del routes[("GET", "/object_info/LoraLoaderModelOnly")]  # host has no such node -> 404
    routes[("GET", "/models/loras")] = ["speed.safetensors"]
    use(monkeypatch, FakeRequests(routes))
    result = cm.audit(HOST, template_path, manifest=MANIFEST)
    assert result["missing_classes"] == ["LoraLoaderModelOnly"]
    assert {m["filename"]: m["installed"] for m in result["models"]}["speed.safetensors"] is True
    assert result["missing"] == []
    assert result["ok"] is False  # a missing node class still fails the audit


def test_audit_handles_newer_combo_input_shape(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    routes = object_info_routes({"loras": ["speed.safetensors"], "vae": ["video_vae.safetensors"]})
    routes[("GET", "/object_info/CheckpointLoaderSimple")] = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": ["COMBO", {"options": ["big-model-fp8.safetensors"]}]}}}}
    use(monkeypatch, FakeRequests(routes))
    assert cm.audit(HOST, template_path, manifest=MANIFEST)["ok"] is True


def test_object_info_is_cached_per_host_and_class(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use(monkeypatch, FakeRequests(object_info_routes({"checkpoints": ["x.safetensors"]})))
    cm.object_info(HOST, "CheckpointLoaderSimple")
    cm.object_info(HOST, "CheckpointLoaderSimple")
    assert len(fake.gets) == 1
    cm.object_info(HOST, "CheckpointLoaderSimple", use_cache=False)
    assert len(fake.gets) == 2


# ----------------------------------------------------------------------------- ensure


def helper_routes() -> dict[tuple[str, str], Any]:
    return {
        ("GET", "/news-generator/helper"): {"name": "news-generator-helper", "version": "0.1.0"},
        ("POST", "/news-generator/models/download"): FakeResponse(202, {"job": {"id": "abc123", "status": "queued", "filename": "speed.safetensors"}}),
        ("GET", "/news-generator/models/jobs"): {"jobs": [{"id": "abc123", "status": "downloading", "filename": "speed.safetensors", "bytes_done": 10, "bytes_total": 100, "percent": 10.0, "speed_bps": 5}]},
    }


def test_ensure_starts_helper_downloads_for_missing_manifest_models(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    monkeypatch.setenv("COMFYUI_HF_TOKEN", "hf_test")
    routes = {**object_info_routes({"checkpoints": [], "loras": [], "vae": ["video_vae.safetensors"]}), **helper_routes()}
    fake = use(monkeypatch, FakeRequests(routes))
    result = cm.ensure(HOST, template_path, download=True, manifest=MANIFEST)
    assert result["downloader"] == "helper"
    assert len(result["started"]) == 2
    bodies = {body["filename"]: body for _, body in fake.posts}
    assert bodies["big-model-fp8.safetensors"] == {
        "url": MANIFEST["big-model-fp8.safetensors"]["url"], "folder": "checkpoints", "filename": "big-model-fp8.safetensors", "sha256": "a" * 64, "token": "hf_test",
    }
    assert "sha256" not in bodies["speed.safetensors"]
    assert result["errors"] == []


def test_ensure_without_download_only_audits(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    routes = {**object_info_routes({"checkpoints": [], "loras": [], "vae": []}), **helper_routes()}
    fake = use(monkeypatch, FakeRequests(routes))
    result = cm.ensure(HOST, template_path, download=False, manifest=MANIFEST)
    assert fake.posts == []
    assert result["started"] == []
    assert result["downloader"] is None
    assert [e["filename"] for e in result["errors"]] == ["video_vae.safetensors"]


def test_ensure_reports_when_host_has_no_downloader(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    use(monkeypatch, FakeRequests(object_info_routes({"checkpoints": [], "loras": [], "vae": ["video_vae.safetensors"]})))
    result = cm.ensure(HOST, template_path, download=True, manifest=MANIFEST)
    assert result["downloader"] is None
    assert result["started"] == []
    assert "helper node" in result["errors"][0]["message"]


def test_ensure_falls_back_to_manager_when_helper_absent(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    routes = {
        **object_info_routes({"checkpoints": ["big-model-fp8.safetensors"], "loras": [], "vae": ["video_vae.safetensors"]}),
        ("GET", "/v2/manager/version"): {"version": "3.0"},
        ("POST", "/v2/manager/queue/install_model"): {"status": "ok"},
        ("GET", "/v2/manager/queue/start"): {},
    }
    fake = use(monkeypatch, FakeRequests(routes))
    result = cm.ensure(HOST, template_path, download=True, manifest=MANIFEST)
    assert result["downloader"] == "manager"
    assert [j["via"] for j in result["started"]] == ["manager"]
    url, body = fake.posts[0]
    assert url.endswith("/v2/manager/queue/install_model")
    assert body["save_path"] == "loras" and body["filename"] == "speed.safetensors"


def test_downloader_mode_none_disables_downloads(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    monkeypatch.setenv("COMFYUI_MODEL_DOWNLOADER", "none")
    routes = {**object_info_routes({"checkpoints": [], "loras": [], "vae": ["video_vae.safetensors"]}), **helper_routes()}
    fake = use(monkeypatch, FakeRequests(routes))
    result = cm.ensure(HOST, template_path, download=True, manifest=MANIFEST)
    assert result["downloader"] is None and fake.posts == []


def test_jobs_reads_helper_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, FakeRequests(helper_routes()))
    report = cm.jobs(HOST)
    assert report["downloader"] == "helper"
    assert report["jobs"][0]["percent"] == 10.0


# ----------------------------------------------------------------------------- preflight hook


def test_preflight_detail_ok_and_fail_messages(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    monkeypatch.setattr(cm, "load_manifest", lambda path=None: MANIFEST)
    use(monkeypatch, FakeRequests(object_info_routes({"checkpoints": ["big-model-fp8.safetensors"], "loras": ["speed.safetensors"], "vae": ["video_vae.safetensors"]})))
    assert cm.preflight_detail(HOST, template_path)[0] == "ok"

    cm.clear_cache()
    use(monkeypatch, FakeRequests(object_info_routes({"checkpoints": [], "loras": ["speed.safetensors"], "vae": []})))
    status, detail = cm.preflight_detail(HOST, template_path)
    assert status == "fail"
    assert "big-model-fp8.safetensors" in detail and "video_vae.safetensors" in detail
    assert "no manifest entry" in detail


def test_preflight_detail_starts_downloads_when_auto_enabled(monkeypatch: pytest.MonkeyPatch, template_path: Path) -> None:
    monkeypatch.setattr(cm, "load_manifest", lambda path=None: MANIFEST)
    monkeypatch.setenv("COMFYUI_AUTO_DOWNLOAD_MODELS", "true")
    routes = {**object_info_routes({"checkpoints": [], "loras": ["speed.safetensors"], "vae": ["video_vae.safetensors"]}), **helper_routes()}
    fake = use(monkeypatch, FakeRequests(routes))
    status, detail = cm.preflight_detail(HOST, template_path)
    assert status == "fail"
    assert "download started via helper" in detail
    assert len(fake.posts) == 1 and fake.posts[0][1]["filename"] == "big-model-fp8.safetensors"


# ----------------------------------------------------------------------------- configured targets


def test_configured_targets_follow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_COMFYUI_BASE_URL", "http://v:8188")
    monkeypatch.setenv("VIDEO_COMFYUI_WORKFLOW", "comfyui-workflows/video_ltx2_3_ia2v-extend.json")
    monkeypatch.setenv("AVATAR_COMFYUI_BASE_URL", "http://a:8123")
    monkeypatch.setenv("AVATAR_COMFYUI_WORKFLOW", "comfyui-workflows/avatar/image_z_image_turbo_int8.json")
    monkeypatch.setenv("HERO_COMFYUI_WORKFLOW", "image_qwen_image_2_1.json")
    targets = cm.configured_targets()
    assert [(t["host"], t["workflow"].name) for t in targets] == [
        ("video", "video_ltx2_3_ia2v-extend.json"),
        ("avatar", "image_z_image_turbo_int8.json"),
        ("avatar", "image_qwen_image_2_1.json"),
    ]
    assert [t["host"] for t in cm.configured_targets("video")] == ["video"]
    assert cm.configured_targets("avatar", "image_qwen_Image_2512.json")[0]["workflow"].name == "image_qwen_Image_2512.json"
    with pytest.raises(ApiError):
        cm.configured_targets("tts")
