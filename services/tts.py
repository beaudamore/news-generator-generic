"""Text-to-speech: the one place that talks to the TTS server.

TTS_PROVIDER picks the server:

  chatterbox (default)  CHATTERBOX_BASE_URL, CHATTERBOX_DEFAULT_TIMEOUT_SECONDS
  openai                an OpenAI-compatible /v1/audio/speech server (vLLM-Omni serving Qwen3-TTS):
                        TTS_OPENAI_BASE_URL (ends in /v1), TTS_OPENAI_MODEL, TTS_OPENAI_API_KEY,
                        TTS_OPENAI_TIMEOUT_SECONDS

Both keep a voice library (list, upload a sample, delete) and synthesize a WAV from text and a
library voice name. The pipeline, preflight, tenant voices, runs, the API and the CLI all call the
functions here; none of them builds a TTS URL or reads a TTS env var itself.
"""

from __future__ import annotations

import importlib
import os
import wave
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

PROVIDERS = ("chatterbox", "openai")
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


# ----------------------------------------------------------------------------- provider


def provider() -> str:
    return os.environ.get("TTS_PROVIDER", "").strip().lower() or "chatterbox"


def _openai() -> bool:
    name = provider()
    if name not in PROVIDERS:
        raise VoiceServiceError(f"TTS_PROVIDER must be one of {PROVIDERS}, got {name!r}.")
    return name == "openai"


def service_name() -> str:
    """Name of the TTS service in preflight checks, system info and logs."""
    return "tts_openai" if provider() == "openai" else "chatterbox"


def label() -> str:
    """Name of the TTS service in messages shown to a person."""
    return "OpenAI-compatible TTS" if provider() == "openai" else "Chatterbox"


def base_url_env() -> str:
    return "TTS_OPENAI_BASE_URL" if provider() == "openai" else "CHATTERBOX_BASE_URL"


def base_url() -> str:
    _openai()  # refuse an unknown provider before reading its settings
    name = base_url_env()
    value = os.environ.get(name, "").strip()
    if not value:
        raise VoiceServiceError(f"Set {name}.")
    return value.rstrip("/")


def default_timeout() -> int:
    name = "TTS_OPENAI_TIMEOUT_SECONDS" if provider() == "openai" else "CHATTERBOX_DEFAULT_TIMEOUT_SECONDS"
    return int(os.environ.get(name, "120"))


def _headers() -> dict[str, str]:
    key = os.environ.get("TTS_OPENAI_API_KEY", "").strip() if _openai() else ""
    return {"Authorization": f"Bearer {key}"} if key else {}


def _voices_url() -> str:
    return f"{base_url()}/audio/voices" if _openai() else f"{base_url()}/voices"


# ----------------------------------------------------------------------------- voice library


def _voice_entries(timeout: float) -> list[dict]:
    """The server's voice library as [{"name", "aliases"}]. Chatterbox lists objects; the OpenAI-compatible server lists bare names."""
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
        if _openai():
            # The server refuses an upload without a consent record id.
            data = {"name": voice, "consent": f"news-generator-{date.today().isoformat()}"}
            files = {"audio_sample": (voice_sample_path.name, voice_file, "audio/wav")}
        else:
            data = {"voice_name": voice, "language": "en"}
            files = {"voice_file": (voice_sample_path.name, voice_file, "audio/wav")}
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
    if _openai():
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
    if _openai():
        model = os.environ.get("TTS_OPENAI_MODEL", "").strip()
        listing = requests.get(f"{base}/models", headers=_headers(), timeout=timeout)
        listing.raise_for_status()
        served = sorted(item["id"] for item in listing.json().get("data") or [] if isinstance(item, dict) and item.get("id"))
        if model and model not in served:
            return "fail", f"model {model!r} not served; available: {served}"
        ready = f"serving {model or served}"
    else:
        health = requests.get(f"{base}/health", timeout=timeout)
        health.raise_for_status()
        data = health.json()
        if not data.get("model_loaded"):
            return "fail", f"model not loaded (state={data.get('initialization_state')!r})"
        ready = f"model loaded on {data.get('device')}"
    if voice:
        names = voice_names(timeout)
        if voice not in names:
            return "fail", f"voice {voice!r} not in library; available: {sorted(names)}"
        return "ok", f"{ready}; voice {voice!r} present"
    return "ok", ready
