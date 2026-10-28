#!/usr/bin/env python3
"""Generate narration audio through the configured TTS server (services/tts.py)."""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import tts  # noqa: E402

DEFAULT_OUTPUT_NAME = "narration.wav"


def load_env_file(path: Path) -> None:
    dotenv = importlib.import_module("dotenv")
    dotenv.load_dotenv(path)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "narration"


def read_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text.strip()
    if args.text_file:
        return Path(args.text_file).expanduser().read_text(encoding="utf-8").strip()
    raise SystemExit("Provide --text or --text-file.")


def build_output_path(output_arg: Optional[str], text: str) -> Path:
    if output_arg:
        output_path = Path(output_arg).expanduser()
        if not output_path.is_absolute():
            output_path = PROJECT_ROOT / output_path
        return output_path

    output_root = Path(os.environ.get("NEWS_OUTPUT_DIRECTORY", PROJECT_ROOT / "output")).expanduser()
    if not output_root.is_absolute():
        output_root = PROJECT_ROOT / output_root
    output_dir = output_root / "audio"
    stem = slugify(text[:80])
    return output_dir / f"{stem}.wav"


def resolve_project_path(path_arg: str) -> Path:
    path = Path(path_arg).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate narration audio with the configured TTS server.")
    parser.add_argument("--text", help="Text to synthesize.")
    parser.add_argument("--text-file", help="Path to a UTF-8 text file to synthesize.")
    parser.add_argument("--output", help="Output WAV path. Relative paths are resolved from the project root.")
    parser.add_argument("--voice", required=True, help="Required TTS library voice name.")
    parser.add_argument("--voice-sample", help="Path to a replacement voice sample WAV for --voice.")
    parser.add_argument("--timeout", type=int, default=tts.default_timeout(), help="HTTP timeout in seconds.")
    return parser.parse_args()


def main() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args()

    text = read_text(args)
    try:
        tts.base_url()
    except tts.VoiceServiceError as exc:
        raise SystemExit(str(exc)) from exc
    voice_sample_path = resolve_project_path(args.voice_sample) if args.voice_sample else None
    if voice_sample_path:
        try:
            voice = args.voice
            voice_mode = tts.replace_voice_sample(voice, voice_sample_path, min(args.timeout, 30))
        except tts.VoiceResolutionError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        try:
            voice, voice_mode = tts.resolve_voice(args.voice, min(args.timeout, 10))
        except tts.VoiceResolutionError as exc:
            raise SystemExit(str(exc)) from exc
    output_path = build_output_path(args.output, text)

    result = tts.synthesize(text, output_path, voice, voice_mode, args.timeout)
    duration = result["duration_seconds"]
    duration_text = f"{duration:.2f}s" if duration is not None else "unknown"

    print(f"audio_path={result['output_path']}")
    print(f"voice={result['voice']}")
    print(f"voice_mode={result['voice_mode']}")
    print(f"voice_sample_path={voice_sample_path}")
    print(f"duration_seconds={duration_text}")
    print(f"content_type={result['content_type']}")
    print(f"size_bytes={result['size_bytes']}")


if __name__ == "__main__":
    main()
