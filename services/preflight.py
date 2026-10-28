"""Preflight checks: verify every service a run depends on before any stage starts.

One function, `run_preflight`, returns a structured result that the pipeline logs
and refuses on, that `GET /health/deep` serves, and that the MCP `preflight` tool
returns. Checks run in parallel with short timeouts and the result is cached for
CACHE_SECONDS so a burst of scheduled runs does not hammer the hosts.

Status per check: "ok", "warn" (soft, the run continues), or "fail" (hard, the run
is refused). The overall `ok` is true when no check failed.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from services import tts

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_SECONDS = 30
MIN_FREE_BYTES_WARN = 2 * 1024**3
MIN_FREE_BYTES_FAIL = 512 * 1024**2

_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}


@dataclass
class Check:
    service: str
    status: str
    detail: str
    latency_ms: int


def _timeout() -> float:
    return float(os.environ.get("PREFLIGHT_TIMEOUT_SECONDS", "5"))


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _flag(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def _requests():
    return importlib.import_module("requests")


def _timed(service: str, fn: Callable[[], tuple[str, str]]) -> Check:
    """Run one check body, returning (status, detail); any exception becomes a fail."""
    started = time.perf_counter()
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001 - every failure must become a check result
        status, detail = "fail", f"{type(exc).__name__}: {str(exc)[:200]}"
    return Check(service, status, detail, int((time.perf_counter() - started) * 1000))


# ----------------------------------------------------------------------------- checks


def check_searxng() -> Check:
    def body() -> tuple[str, str]:
        base = _env("SEARXNG_BASE_URL").rstrip("/")
        if not base:
            return "fail", "SEARXNG_BASE_URL not set"
        requests = _requests()
        config = requests.get(f"{base}/config", timeout=_timeout())
        config.raise_for_status()
        engines = config.json().get("engines") or []
        wanted = {c.strip() for c in _env("SEARXNG_DEFAULT_CATEGORIES").split(",") if c.strip()}
        enabled = [e for e in engines if e.get("enabled") and (not wanted or wanted & set(e.get("categories") or []))]
        # A live search is slower than a config read; give it the pipeline's own search timeout.
        probe_timeout = max(_timeout(), float(os.environ.get("SEARXNG_DEFAULT_TIMEOUT_SECONDS", "20") or 20))
        probe = requests.get(f"{base}/search", params={"q": "preflight", "format": "json", "pageno": 1}, timeout=probe_timeout)
        if probe.status_code == 403:
            return "fail", "JSON format is disabled on this SearXNG instance (search.formats must include json)"
        probe.raise_for_status()
        if not enabled:
            return "warn", f"no enabled engines for categories {sorted(wanted)}"
        return "ok", f"{len(enabled)} enabled engines for categories {sorted(wanted) or 'all'}; json format ok"

    return _timed("searxng", body)


def check_llm() -> Check:
    def body() -> tuple[str, str]:
        base = _env("OPENAI_BASE_URL").rstrip("/")
        model = _env("OPENAI_MODEL")
        if not base:
            return "fail", "OPENAI_BASE_URL not set"
        if not model:
            return "fail", "OPENAI_MODEL not set"
        headers = {}
        if _env("OPENAI_API_KEY"):
            headers["Authorization"] = f"Bearer {_env('OPENAI_API_KEY')}"
        response = _requests().get(f"{base}/models", headers=headers, timeout=_timeout())
        response.raise_for_status()
        ids = [m.get("id") for m in response.json().get("data") or []]
        if model not in ids:
            return "fail", f"model {model!r} not served; available: {ids[:12]}"
        try:
            from services import llm

            info = llm.context_info(use_cache=False)
            effective = info.get("effective") or 0
            status = "warn" if effective and effective < 8192 else "ok"
            return status, f"model {model!r} present; {llm.describe(info)}" + ("; context under 8k, long prompts will be trimmed" if status == "warn" else "")
        except Exception as exc:  # noqa: BLE001
            return "ok", f"model {model!r} present; context detection failed: {exc}"

    return _timed("llm", body)


def check_reranker() -> Check:
    def body() -> tuple[str, str]:
        if not _flag("NEWS_CONTEXT_RERANK_ENABLED") and not _flag("NEWS_SOURCE_RANK_ENABLED"):
            return "ok", "reranking disabled"
        backend = (_env("NEWS_CONTEXT_RERANK_BACKEND") or "http").lower()
        if backend != "http":
            return "ok", f"backend {backend!r}; no remote check"
        endpoint = _env("NEWS_CONTEXT_RERANK_ENDPOINT")
        model = _env("NEWS_CONTEXT_RERANK_MODEL")
        if not endpoint:
            return "warn", "NEWS_CONTEXT_RERANK_ENDPOINT not set; ranking degrades to lexical"
        base = endpoint.split("/v1/")[0].rstrip("/") if "/v1/" in endpoint else endpoint.rsplit("/", 1)[0]
        requests = _requests()
        try:
            response = requests.get(f"{base}/v1/models", timeout=_timeout())
            if response.ok:
                ids = [m.get("id") for m in response.json().get("data") or []]
                if model and model not in ids:
                    return "warn", f"model {model!r} not served; available: {ids[:8]}; ranking degrades to lexical"
                return "ok", f"model {model or '(unspecified)'} present at {base}"
        except Exception:  # noqa: BLE001 - fall through to /health
            pass
        response = requests.get(f"{base}/health", timeout=_timeout())
        if response.ok:
            return "ok", f"health ok at {base} (model list unavailable)"
        return "warn", f"reranker unreachable at {base}; ranking degrades to lexical"

    check = _timed("reranker", body)
    if check.status == "fail":  # reranker is soft: never block a run on it
        check.status = "warn"
        check.detail += "; ranking degrades to lexical"
    return check


def check_tts(voice: Optional[str]) -> Check:
    return _timed(tts.service_name(), lambda: tts.check(voice, _timeout()))


def check_comfyui_video(anchor_path: Optional[Path]) -> Check:
    def body() -> tuple[str, str]:
        base = _env("VIDEO_COMFYUI_BASE_URL").rstrip("/")
        if not base:
            return "fail", "VIDEO_COMFYUI_BASE_URL not set"
        requests = _requests()
        requests.get(f"{base}/system_stats", timeout=_timeout()).raise_for_status()
        queue = requests.get(f"{base}/queue", timeout=_timeout())
        depth = ""
        if queue.ok:
            q = queue.json()
            depth = f"queue running={len(q.get('queue_running') or [])} pending={len(q.get('queue_pending') or [])}; "
        generate_video_comfyui = importlib.import_module("scripts.generate_video_comfyui")
        workflow_value = _env("VIDEO_COMFYUI_WORKFLOW")
        if not workflow_value:
            return "fail", "VIDEO_COMFYUI_WORKFLOW not set"
        workflow_path = generate_video_comfyui.resolve_project_path(workflow_value)
        if not workflow_path.is_file():
            return "fail", f"workflow template missing: {workflow_path}"
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        check = generate_video_comfyui.validate_any_video_workflow(workflow, workflow_path.name)
        pipeline = importlib.import_module("services.pipeline")
        if check.get("family") == "wan_s2v":
            settings = generate_video_comfyui.family_settings("wan_s2v")
            width, height = settings["width"], settings["height"]
        else:
            width, height = pipeline.target_video_dimensions()
        if anchor_path is not None and not Path(anchor_path).is_file():
            return "fail", f"anchor image missing: {anchor_path}"
        anchor_note = f"; anchor {Path(anchor_path).name}" if anchor_path is not None else ""
        return "ok", f"{depth}template {workflow_path.name} valid; target {width}x{height}{anchor_note}"

    return _timed("comfyui_video", body)


def check_comfyui_avatar() -> Check:
    def body() -> tuple[str, str]:
        base = _env("AVATAR_COMFYUI_BASE_URL").rstrip("/")
        if not base:
            return "fail", "AVATAR_COMFYUI_BASE_URL not set"
        _requests().get(f"{base}/system_stats", timeout=_timeout()).raise_for_status()
        workflow_value = _env("AVATAR_COMFYUI_WORKFLOW")
        if not workflow_value:
            return "fail", "AVATAR_COMFYUI_WORKFLOW not set"
        path = Path(workflow_value).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            return "fail", f"avatar workflow missing: {path}"
        return "ok", f"reachable; workflow {path.name} present"

    return _timed("comfyui_avatar", body)


def check_crawl4ai() -> Check:
    """Soft: extraction falls back to search snippets when crawl4ai is unavailable."""

    def body() -> tuple[str, str]:
        from services import extract

        if not extract.enabled():
            return "ok", "article extraction disabled (CRAWL4AI_API_URL unset or ARTICLE_EXTRACTION_ENABLED=false)"
        cfg = extract.settings()
        requests = _requests()
        health = requests.get(f"{cfg['url']}/health", timeout=_timeout())
        health.raise_for_status()
        auth = requests.get(f"{cfg['url']}/schema", headers={"Authorization": f"Bearer {cfg['token']}"} if cfg["token"] else {}, timeout=_timeout())
        if auth.status_code == 401:
            return "warn", "crawl4ai reachable but CRAWL4AI_API_TOKEN is missing or wrong; extraction will fall back to snippets"
        return "ok", f"crawl4ai {health.json().get('version', '')} reachable, token accepted, filter={cfg['filter']} max_words={cfg['max_words']}"

    check = _timed("crawl4ai", body)
    if check.status == "fail":
        check.status = "warn"
        check.detail += "; extraction will fall back to snippets"
    return check


def check_local() -> Check:
    def body() -> tuple[str, str]:
        problems: list[str] = []
        status = "ok"
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                problems.append(f"{tool} not on PATH (final video size will not be verified)")
                status = "warn"
        root = Path(_env("NEWS_OUTPUT_DIRECTORY") or "output").expanduser()
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        try:
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=root, prefix=".preflight-", delete=True):
                pass
        except OSError as exc:
            return "fail", f"output directory not writable: {root} ({exc})"
        free = shutil.disk_usage(root).free
        if free < MIN_FREE_BYTES_FAIL:
            return "fail", f"only {free // 1024**2} MB free under {root}"
        if free < MIN_FREE_BYTES_WARN:
            problems.append(f"low disk: {free // 1024**2} MB free under {root}")
            status = "warn"
        detail = f"output {root} writable; {free // 1024**3} GB free"
        if problems:
            detail += "; " + "; ".join(problems)
        return status, detail

    return _timed("local", body)


# ----------------------------------------------------------------------------- entry point


def run_preflight(
    *,
    voice: Optional[str] = None,
    anchor_path: Optional[Path] = None,
    need_search: bool = True,
    need_llm: bool = True,
    need_video: bool = True,
    need_avatar: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Run every applicable check in parallel and return a structured result.

    Result: {"ok": bool, "checks": [Check as dict, ...], "checked_at": iso, "duration_ms": int, "cached": bool}
    """
    key = (voice, str(anchor_path) if anchor_path else None, need_search, need_llm, need_video, need_avatar)
    now = time.time()
    if use_cache:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return {**hit[1], "cached": True}

    tasks: list[Callable[[], Check]] = []
    if need_search:
        tasks.append(check_searxng)
        tasks.append(check_reranker)
        tasks.append(check_crawl4ai)
    if need_llm:
        tasks.append(check_llm)
    tasks.append(lambda: check_tts(voice))
    if need_video:
        tasks.append(lambda: check_comfyui_video(anchor_path))
    if need_avatar:
        tasks.append(check_comfyui_avatar)
    tasks.append(check_local)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        checks = list(pool.map(lambda fn: fn(), tasks))
    result = {
        "ok": all(c.status != "fail" for c in checks),
        "checks": [asdict(c) for c in checks],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "cached": False,
    }
    _cache[key] = (now, result)
    return result


def failure_summary(result: dict[str, Any]) -> str:
    return "; ".join(f"{c['service']}: {c['detail']}" for c in result["checks"] if c["status"] == "fail")


def clear_cache() -> None:
    _cache.clear()
