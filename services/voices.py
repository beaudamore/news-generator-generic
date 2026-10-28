"""Voice discovery service for Chatterbox."""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass


@dataclass
class VoiceServiceError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def chatterbox_base_url() -> str:
    base_url = os.environ.get("CHATTERBOX_BASE_URL", "").strip()
    if not base_url:
        raise VoiceServiceError("Set CHATTERBOX_BASE_URL.")
    return base_url.rstrip("/")


def list_voices(timeout: int | None = None) -> dict:
    requests = importlib.import_module("requests")
    request_timeout = timeout if timeout is not None else int(os.environ.get("CHATTERBOX_DEFAULT_TIMEOUT_SECONDS", "120"))
    response = requests.get(f"{chatterbox_base_url()}/voices", timeout=request_timeout)
    response.raise_for_status()

    raw_voices = response.json().get("voices") or []
    voices = []
    for raw_voice in raw_voices:
        if not isinstance(raw_voice, dict):
            continue
        name = raw_voice.get("name")
        if not name:
            continue
        aliases = raw_voice.get("aliases") or []
        voices.append(
            {
                "name": name,
                "aliases": [alias for alias in aliases if alias],
            }
        )

    voices.sort(key=lambda voice: voice["name"].lower())
    return {
        "voices": voices,
        "count": len(voices),
    }