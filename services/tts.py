"""Text-to-speech: the one place that talks to the TTS server.

The server is OpenAI-compatible (Qwen3-TTS behind vLLM-Omni or mlx): /v1/audio/speech for synthesis
and /v1/audio/voices for the voice library (list, upload a sample, delete). Configured by
TTS_OPENAI_BASE_URL (ends in /v1), TTS_OPENAI_MODEL, TTS_OPENAI_API_KEY and TTS_OPENAI_TIMEOUT_SECONDS.
The pipeline, preflight, tenant voices, runs, the API and the CLI all call the functions here; none of
them builds a TTS URL or reads a TTS env var itself. The Chatterbox provider was removed; a leftover
TTS_PROVIDER=chatterbox in .env is refused with a clear message.
"""

from __future__ import annotations

import importlib
import os
import wave
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

PREVIEW_TEXT = "This is a sample of this voice reading a short news line."


@dataclass
class VoiceServiceError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


class VoiceResolutionError(ValueError):
    pass


def _requests():
    return importlib.import_module("requests")


# ----------------------------------------------------------------------------- server


def _refuse_removed_provider() -> None:
    value = os.environ.get("TTS_PROVIDER", "").strip().lower()
    if value and value != "openai":
        raise VoiceServiceError(f"TTS_PROVIDER={value!r} is not supported: the Chatterbox provider was removed. Unset TTS_PROVIDER and set TTS_OPENAI_*.")


def service_name() -> str:
    """Name of the TTS service in preflight checks, system info and logs."""
    return "tts_openai"


def label() -> str:
    """Name of the TTS service in messages shown to a person."""
    return "TTS server"


def base_url_env() -> str:
    return "TTS_OPENAI_BASE_URL"


def base_url() -> str:
    _refuse_removed_provider()
    value = os.environ.get(base_url_env(), "").strip()
    if not value:
        raise VoiceServiceError(f"Set {base_url_env()}.")
    return value.rstrip("/")


def default_timeout() -> int:
    return int(os.environ.get("TTS_OPENAI_TIMEOUT_SECONDS", "120"))


def _headers() -> dict[str, str]:
    key = os.environ.get("TTS_OPENAI_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _voices_url() -> str:
    return f"{base_url()}/audio/voices"


# ----------------------------------------------------------------------------- voice library


def _voice_entries(timeout: float) -> list[dict]:
    """The server's voice library as [{"name", "aliases"}]. The server lists bare names; objects with aliases are accepted too."""
    response = _requests().get(_voices_url(), headers=_headers(), timeout=timeout)
    response.raise_for_status()
    entries = []
    for raw in response.json().get("voices") or []:
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, dict):
            continue
        entries.append({"name": raw.get("name"), "aliases": [alias for alias in raw.get("aliases") or [] if alias]})
    return entries


def voice_names(timeout: float) -> set[str]:
    names: set[str] = set()
    for entry in _voice_entries(timeout):
        if entry["name"]:
            names.add(entry["name"])
        names.update(entry["aliases"])
    return names


def list_voices(timeout: Optional[float] = None) -> dict:
    entries = [entry for entry in _voice_entries(timeout if timeout is not None else default_timeout()) if entry["name"]]
    entries.sort(key=lambda entry: entry["name"].lower())
    return {
        "voices": entries,
        "count": len(entries),
    }


def delete_voice(voice: str, timeout: float) -> None:
    response = _requests().delete(f"{_voices_url()}/{voice}", headers=_headers(), timeout=timeout)
    response.raise_for_status()


def upload_voice(voice: str, voice_sample_path: Path, timeout: float) -> None:
    with voice_sample_path.open("rb") as voice_file:
        # The server refuses an upload without a consent record id.
        data = {"name": voice, "consent": f"news-generator-{date.today().isoformat()}"}
        files = {"audio_sample": (voice_sample_path.name, voice_file, "audio/wav")}
        response = _requests().post(_voices_url(), data=data, files=files, headers=_headers(), timeout=timeout)
    response.raise_for_status()


def replace_voice_sample(voice: str, voice_sample_path: Path, timeout: float) -> str:
    if not voice_sample_path.is_file():
        raise VoiceResolutionError(f"Voice sample file does not exist: {voice_sample_path}")

    if voice in voice_names(timeout):
        delete_voice(voice, timeout)
        voice_mode = "library-replaced"
    else:
        voice_mode = "library-uploaded"
    upload_voice(voice, voice_sample_path, timeout)
    return voice_mode


def resolve_voice(voice_arg: Optional[str], timeout: float) -> tuple[str, str]:
    if not voice_arg:
        raise VoiceResolutionError("Provide --voice. Refusing to use the API/file-system default voice implicitly.")

    if voice_arg in voice_names(timeout):
        return voice_arg, "library"

    raise VoiceResolutionError(f"Voice {voice_arg!r} is not available in the {label()} voice library.")


# ----------------------------------------------------------------------------- synthesis


def wav_duration_seconds(path: Path) -> Optional[float]:
    try:
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate()
            if rate <= 0:
                return None
            return frames / float(rate)
    except wave.Error:
        return None


def _speech(text: str, voice: str, timeout: float):
    """POST the speech request and return the response, whose body is the WAV."""
    payload = {
        "input": text,
        "response_format": "wav",
        "voice": voice,
    }
    model = os.environ.get("TTS_OPENAI_MODEL", "").strip()
    if model:
        payload["model"] = model
    response = _requests().post(f"{base_url()}/audio/speech", json=payload, headers=_headers(), timeout=timeout)
    response.raise_for_status()
    return response


def preview(voice: str, timeout: float) -> bytes:
    """A short WAV of a library voice for the admin UI's players.

    Synthesized on every call and never stored: a library name can be re-uploaded as a different voice.
    """
    if voice not in voice_names(min(timeout, 10)):
        raise VoiceResolutionError(f"Voice {voice!r} is not available in the {label()} voice library.")
    return _speech(PREVIEW_TEXT, voice, timeout).content


def synthesize(text: str, output_path: Path, voice: str, voice_mode: str, timeout: float) -> dict:
    response = _speech(text, voice, timeout)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(response.content)

    return {
        "output_path": str(output_path),
        "voice": voice,
        "voice_mode": voice_mode,
        "duration_seconds": wav_duration_seconds(output_path),
        "content_type": response.headers.get("content-type"),
        "size_bytes": output_path.stat().st_size,
    }


# ----------------------------------------------------------------------------- preflight


def check(voice: Optional[str], timeout: float) -> tuple[str, str]:
    """Preflight body, returning (status, detail): the server answers, its model is up, and it has `voice` when one is given."""
    base = base_url()
    requests = _requests()
    model = os.environ.get("TTS_OPENAI_MODEL", "").strip()
    listing = requests.get(f"{base}/models", headers=_headers(), timeout=timeout)
    listing.raise_for_status()
    served = sorted(item["id"] for item in listing.json().get("data") or [] if isinstance(item, dict) and item.get("id"))
    if model and model not in served:
        return "fail", f"model {model!r} not served; available: {served}"
    ready = f"serving {model or served}"
    if voice:
        names = voice_names(timeout)
        if voice not in names:
            return "fail", f"voice {voice!r} not in library; available: {sorted(names)}"
        return "ok", f"{ready}; voice {voice!r} present"
    return "ok", ready
