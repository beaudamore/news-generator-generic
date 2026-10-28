"""LLM backend detection and context-window handling.

The script stage must never silently truncate its prompt. This module works out
which server is behind OPENAI_BASE_URL (Ollama, vLLM, SGLang, or a generic
OpenAI-compatible server), what the model's maximum context is, what context the
server will actually use, and asks for a larger one where the server allows it.

  Ollama   context is per request: the app sends options.num_ctx and reads back the
           loaded context from /api/ps. LLM_CONTEXT_SIZE=auto picks min(model max,
           LLM_CONTEXT_CAP); a number pins it.
  vLLM     context is fixed at launch (max_model_len from /v1/models). Read-only.
  SGLang   context is fixed at launch (context_len from /get_server_info). Read-only.
  other    unknown; the app assumes LLM_CONTEXT_FALLBACK and warns.

`fit_to_context` estimates prompt tokens and reports how many are over budget so the
caller can trim evidence before the call instead of losing the end of the prompt.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from typing import Any, Optional

LOGGER = logging.getLogger("news_pipeline")
CHARS_PER_TOKEN = 3.5
_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def base_url() -> str:
    return _env("OPENAI_BASE_URL", "").rstrip("/")


def root_url() -> str:
    base = base_url()
    return base[:-3] if base.endswith("/v1") else base


def _get(url: str, timeout: float = 5.0, headers: Optional[dict[str, str]] = None) -> Optional[Any]:
    requests = importlib.import_module("requests")
    try:
        response = requests.get(url, timeout=timeout, headers=headers or {})
        if response.ok:
            try:
                return response.json()
            except ValueError:
                return response.text
    except Exception:  # noqa: BLE001
        return None
    return None


def _headers() -> dict[str, str]:
    key = _env("OPENAI_API_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}


def detect_backend() -> str:
    root = root_url()
    if isinstance(_get(f"{root}/api/version"), dict):
        return "ollama"
    if isinstance(_get(f"{root}/get_server_info"), dict):
        return "sglang"
    version = _get(f"{root}/version")
    if isinstance(version, dict) and "version" in version:
        return "vllm"
    models = _get(f"{base_url()}/models", headers=_headers())
    if isinstance(models, dict) and any("max_model_len" in m for m in models.get("data") or []):
        return "vllm"
    return "openai"


def context_info(use_cache: bool = True) -> dict[str, Any]:
    """Backend, model maximum, server/loaded context, and the context the app will request."""
    now = time.time()
    if use_cache and _cache["value"] is not None and now - _cache["at"] < 60:
        return dict(_cache["value"])
    model = _env("OPENAI_MODEL", "")
    backend = detect_backend()
    info: dict[str, Any] = {"backend": backend, "model": model, "model_max": None, "loaded": None, "requested": None, "effective": None, "adjustable": backend == "ollama", "note": ""}
    cap = int(_env("LLM_CONTEXT_CAP", "131072"))
    configured = _env("LLM_CONTEXT_SIZE", "auto").lower()
    root = root_url()

    if backend == "ollama":
        requests = importlib.import_module("requests")
        try:
            show = requests.post(f"{root}/api/show", json={"model": model}, timeout=10).json()
            model_info = show.get("model_info") or {}
            maxes = [v for k, v in model_info.items() if k.endswith(".context_length") and isinstance(v, int)]
            info["model_max"] = max(maxes) if maxes else None
            params = show.get("parameters") or ""
            for line in params.splitlines():
                if line.strip().startswith("num_ctx"):
                    info["note"] = f"model file sets {line.strip()}"
        except Exception as exc:  # noqa: BLE001
            info["note"] = f"/api/show failed: {exc}"
        ps = _get(f"{root}/api/ps")
        if isinstance(ps, dict):
            for entry in ps.get("models") or []:
                if entry.get("name") == model or entry.get("model") == model:
                    info["loaded"] = entry.get("context_length")
        if configured != "auto" and configured.isdigit():
            info["requested"] = int(configured)
        else:
            info["requested"] = min(info["model_max"] or cap, cap)
        info["effective"] = info["requested"]
    elif backend == "vllm":
        models = _get(f"{base_url()}/models", headers=_headers())
        for m in (models or {}).get("data") or []:
            if m.get("id") == model:
                info["model_max"] = m.get("max_model_len")
        info["effective"] = info["model_max"]
        info["note"] = "vLLM context is fixed at launch (max_model_len)"
    elif backend == "sglang":
        server = _get(f"{root}/get_server_info") or {}
        info["model_max"] = server.get("context_len") or server.get("max_total_num_tokens")
        info["effective"] = info["model_max"]
        info["note"] = "SGLang context is fixed at launch (context_len)"
    elif configured != "auto" and configured.isdigit():
        info["effective"] = int(configured)
        info["note"] = "unknown backend; using LLM_CONTEXT_SIZE as set"
    else:
        info["effective"] = int(_env("LLM_CONTEXT_FALLBACK", "8192"))
        info["note"] = "unknown backend; assuming LLM_CONTEXT_FALLBACK (set LLM_CONTEXT_SIZE to a number to pin it)"
    _cache.update(at=now, value=dict(info))
    return info


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def budget(info: Optional[dict[str, Any]] = None) -> int:
    """Tokens available for the prompt after reserving room for the answer."""
    info = info or context_info()
    reserve = int(_env("LLM_RESERVE_TOKENS", "2048"))
    effective = info.get("effective") or int(_env("LLM_CONTEXT_FALLBACK", "8192"))
    return max(int(effective) - reserve, 1024)


def request_options() -> dict[str, Any]:
    """Extra fields for the chat request. Ollama honors options.num_ctx; others get nothing."""
    info = context_info()
    if info["backend"] == "ollama" and info.get("requested"):
        return {"options": {"num_ctx": int(info["requested"])}}
    return {}


def loaded_context_after_call() -> Optional[int]:
    """For Ollama: the context the model is actually loaded with now (verifies num_ctx took effect)."""
    if context_info().get("backend") != "ollama":
        return None
    ps = _get(f"{root_url()}/api/ps")
    model = _env("OPENAI_MODEL", "")
    for entry in (ps or {}).get("models") or []:
        if entry.get("name") == model or entry.get("model") == model:
            return entry.get("context_length")
    return None


def describe(info: Optional[dict[str, Any]] = None) -> str:
    info = info or context_info()
    parts = [f"backend={info['backend']}", f"model_max={info.get('model_max')}", f"effective={info.get('effective')}"]
    if info.get("loaded") is not None:
        parts.append(f"loaded={info['loaded']}")
    if info.get("note"):
        parts.append(info["note"])
    return " ".join(parts)
