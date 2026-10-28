"""Global default prompts, kept in files and editable by the admin tenant.

Prompts are stored per profile (services.profiles): a new profile is filled from these defaults,
and every save stores the profile's own text. The defaults themselves live in the files named by
.env and are edited from the Prompts screen by the admin tenant only.

Kinds and their files:
  summarize      NEWS_SUMMARIZE_PROMPT_FILE   (or the inline NEWS_SUMMARIZE_PROMPT, which then is not editable)
  video_visual   NEWS_VIDEO_PROMPT_FILE       (or the inline NEWS_VIDEO_PROMPT)
  video_sound    NEWS_VIDEO_SOUND_PROMPT_FILE (or the inline NEWS_VIDEO_SOUND_PROMPT)
  hero_image     NEWS_HERO_PROMPT_FILE        (or the inline NEWS_HERO_PROMPT): turns a segment into an image prompt
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services import audit
from services.db import PROJECT_ROOT
from services.errors import forbidden, invalid
from services.models import Tenant

KINDS = ("summarize", "video_visual", "video_sound", "hero_image")
PROFILE_FIELDS = {"summarize": "summarize_prompt", "video_visual": "video_visual_prompt", "video_sound": "video_sound_prompt", "hero_image": "hero_prompt"}
_SOURCES = {
    "summarize": ("NEWS_SUMMARIZE_PROMPT", "NEWS_SUMMARIZE_PROMPT_FILE", "prompts/generic-news-reporter.md"),
    "video_visual": ("NEWS_VIDEO_PROMPT", "NEWS_VIDEO_PROMPT_FILE", "prompts/video-anchor-scene.md"),
    "video_sound": ("NEWS_VIDEO_SOUND_PROMPT", "NEWS_VIDEO_SOUND_PROMPT_FILE", "prompts/video-sound.md"),
    "hero_image": ("NEWS_HERO_PROMPT", "NEWS_HERO_PROMPT_FILE", "prompts/hero-image.md"),
}
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")


def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise invalid(f"Unknown prompt kind {kind!r}. Valid kinds: {', '.join(KINDS)}.")
    return kind


def default_path(kind: str) -> Path:
    """The file holding a kind's default."""
    _, file_var, fallback = _SOURCES[_check_kind(kind)]
    path = Path(os.environ.get(file_var, "").strip() or fallback).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _inline(kind: str) -> str:
    return os.environ.get(_SOURCES[kind][0], "").strip()


def default_body(kind: str) -> str:
    """The default text for a kind, read fresh each time: the inline .env value if set, else the file."""
    inline = _inline(_check_kind(kind))
    if inline:
        return inline
    path = default_path(kind)
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def profile_defaults() -> dict[str, str]:
    """Default bodies keyed by profile field name, used to fill a new profile."""
    return {PROFILE_FIELDS[kind]: default_body(kind) for kind in KINDS}


def _serialize(kind: str, with_body: bool = True) -> dict[str, Any]:
    path = default_path(kind)
    inline = bool(_inline(kind))
    data: dict[str, Any] = {
        "kind": kind,
        "path": path.relative_to(PROJECT_ROOT).as_posix() if path.is_relative_to(PROJECT_ROOT) else str(path),
        "editable": not inline,
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat() if path.is_file() else None,
    }
    if with_body:
        body = default_body(kind)
        data["body"] = body
        data["variables"] = sorted(set(_PLACEHOLDER.findall(body)))
    return data


def list_prompts() -> list[dict[str, Any]]:
    """Every default with its body, so the profile editor can fill a new profile in one request."""
    return [_serialize(kind) for kind in KINDS]


def get_prompt(kind: str) -> dict[str, Any]:
    return _serialize(_check_kind(kind))


def update_prompt(tenant: Tenant, kind: str, body: str, actor: str = "system") -> dict[str, Any]:
    """Write a new default to its file. Admin tenant only; existing profiles keep their own text."""
    _check_kind(kind)
    if not tenant.is_admin:
        raise forbidden("Only the admin tenant edits the default prompts. Edit the prompts on your profile instead.")
    if not body or not body.strip():
        raise invalid("Prompt body must not be empty.")
    if _inline(kind):
        raise invalid(f"{_SOURCES[kind][0]} is set in .env, which overrides the file; clear it to edit the default here.")
    path = default_path(kind)
    before = default_body(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.strip() + "\n", encoding="utf-8")
    audit.record(tenant_id=tenant.id, actor=actor, action="update_prompt", object_type="prompt", object_id=kind, before={"body": before}, after={"body": body.strip()})
    return _serialize(kind)


def test_prompt(tenant: Tenant, kind: str = "summarize", search_text: Optional[str] = None, max_articles: Optional[int] = None) -> dict[str, Any]:
    """Run only the search and script stages with the default summarize prompt. Creates no queued run.

    The run directory is created under the tenant's output folder and left in place
    for inspection (sources.json, narration.txt, report.md, run.log).
    """
    if kind != "summarize":
        raise invalid("Only the 'summarize' prompt can be tested; the others are used by the video stage.")
    if not search_text or not search_text.strip():
        raise invalid("search_text is required to test the summarize prompt.")
    from services import pipeline, tenancy  # local import: pipeline is heavy and imports scripts

    prompt_body = default_body("summarize")
    run_root = tenancy.tenant_paths(tenant)["output"]
    run_root.mkdir(parents=True, exist_ok=True)
    run_id, run_dir = pipeline.create_run_directory(f"prompt-test-{search_text}", root=run_root)
    result = pipeline.generate_script_stage(
        run_id=run_id,
        run_dir=run_dir,
        search_text=search_text.strip(),
        max_articles=max_articles,
        summarize_prompt=prompt_body,
    )
    return {
        "run_id": run_id,
        "run_directory": str(run_dir),
        "script": result["script"],
        "word_count": result["word_count"],
        "target_words": result["target_words"],
        "sources_used": [
            {"title": s.get("title"), "url": s.get("url"), "source": s.get("source"), "published_at": s.get("published_at"), "source_rank_score": s.get("source_rank_score")}
            for s in result["selected_sources"]
        ],
    }
