"""System information for the `get_system_info` tool and `GET /api/v1/system`."""

from __future__ import annotations

import os
import shutil
from typing import Any

from services import db, logging_setup, pipeline, tts
from services.models import Tenant

APP_VERSION = "0.2.0"


def _service_env() -> dict[str, str]:
    return {
        "searxng": "SEARXNG_BASE_URL",
        "llm": "OPENAI_BASE_URL",
        "reranker": "NEWS_CONTEXT_RERANK_ENDPOINT",
        tts.service_name(): tts.base_url_env(),
        "comfyui_video": "VIDEO_COMFYUI_BASE_URL",
        "comfyui_avatar": "AVATAR_COMFYUI_BASE_URL",
    }


def get_system_info(tenant: Tenant) -> dict[str, Any]:
    """Version, video settings, which services are configured, and storage. URLs only for admin."""
    width, height = pipeline.target_video_dimensions()
    data_dir = db.data_directory()
    services = []
    for name, env_name in _service_env().items():
        value = os.environ.get(env_name, "").strip()
        entry: dict[str, Any] = {"name": name, "configured": bool(value)}
        if tenant.is_admin:
            entry["url"] = value or None
        services.append(entry)
    return {
        "version": APP_VERSION,
        "tenant": tenant.slug,
        "video": {
            "width": width,
            "height": height,
            "fps": int(os.environ.get("VIDEO_COMFYUI_FPS", "24")),
            "workflow": os.environ.get("VIDEO_COMFYUI_WORKFLOW", "").strip() or None,
            "prompt_enhance": os.environ.get("VIDEO_COMFYUI_PROMPT_ENHANCE", "false").strip().lower() in ("1", "true", "yes", "on"),
        },
        "llm_model": os.environ.get("OPENAI_MODEL", "").strip() or None,
        "logging": {"file": str(logging_setup.log_dir() / "app.jsonl"), "seq": logging_setup.seq_stats()},
        "services": services,
        "storage": {
            "data_dir": str(data_dir),
            "output_dir": str(pipeline.output_root()),
            "free_bytes": shutil.disk_usage(data_dir).free,
        },
    }
