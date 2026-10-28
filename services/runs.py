"""Runs: enqueue, inspect, cancel, retry, and fetch artifacts. The worker executes them.

A run row records what was asked (profile, overrides, resolved settings) and what
happened (status, stage, timings, error, metrics). Artifacts live in the run
directory under the tenant's output folder; this module reads them on demand.
"""

from __future__ import annotations

import json
import os
import subprocess
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from sqlmodel import select

from services import audit, avatars, profiles, segments, tenancy
from services.db import session
from services.errors import ApiError, conflict, invalid, not_found
from services.models import Run, Tenant, utcnow

STAGES = ("preflight", "search", "script", "images", "audio", "video")
ACTIVE_STATUSES = ("queued", "preflight", "running")
ARTIFACTS = {
    "video": ("video.mp4", "video/mp4"),
    "audio": ("narration.wav", "audio/wav"),
    "narration": ("narration.txt", "text/plain"),
    "sources": ("sources.json", "application/json"),
    "report": ("report.md", "text/markdown"),
    "log": ("run.log", "text/plain"),
    "preflight": ("preflight.json", "application/json"),
    "chunks": ("chunks.json", "application/json"),
}
TEXT_ARTIFACTS = ("narration", "sources", "report", "log", "preflight", "chunks")
OVERRIDE_FIELDS = (
    "search_text", "script_text", "categories", "language", "safesearch", "max_articles", "max_age_days",
    "rank_query", "rank_candidates", "voice", "avatar", "intended_video_max_seconds", "wpm", "prompt_enhance",
    "summarize_prompt", "video_visual_prompt", "video_sound_prompt", "hero_prompt", "hero_workflow",
)


# ----------------------------------------------------------------------------- settings resolution


def resolve_voice(tenant: Tenant, name: Optional[str]) -> str:
    """Tenant voice -> TTS library name; otherwise the name as-is (pre-existing library voice)."""
    if not name:
        raise invalid("A voice is required (profile.voice or override).")
    from services import tenant_voices, tts

    try:
        return tenant_voices.resolve_library_name(tenant, name)
    except ApiError:
        pass
    # Not a tenant voice: accept only if the TTS library already has it, so a typo fails at enqueue, not in the worker.
    try:
        available = tts.voice_names(10)
    except Exception as exc:  # noqa: BLE001
        raise ApiError("service_unavailable", f"{tts.label()} could not be reached to verify voice {name!r}: {exc}") from exc
    if name not in available:
        raise not_found(f"Voice {name!r} (not a tenant voice and not in the {tts.label()} library; available: {sorted(available)})")
    return name


def resolve_anchor(tenant: Tenant, name: Optional[str]) -> Path:
    """Tenant avatar -> its image path; otherwise the legacy shared avatar under assets/avatars/."""
    if not name:
        raise invalid("An avatar is required (profile.avatar or override).")
    from services import tenant_avatars

    try:
        return tenant_avatars.image_path(tenant, name)
    except ApiError:
        try:
            return avatars.avatar_file_path(name)
        except (FileNotFoundError, avatars.AvatarServiceError) as exc:
            raise not_found(f"Avatar {name!r}") from exc


def resolve_settings(tenant: Tenant, profile_id: Optional[str], overrides: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Merge profile fields with overrides into the settings a run executes with."""
    settings: dict[str, Any] = {}
    if profile_id:
        profile = profiles.get_profile(tenant, profile_id)
        settings.update({k: v for k, v in profile.items() if k in OVERRIDE_FIELDS and v is not None})
        settings["profile_name"] = profile["name"]
    for key, value in (overrides or {}).items():
        if key not in OVERRIDE_FIELDS:
            raise invalid(f"Unknown override {key!r}.")
        if value is not None:
            settings[key] = value
    if not settings.get("search_text") and not settings.get("script_text"):
        raise invalid("search_text (from the profile or an override) or script_text is required.")
    if not settings.get("voice"):
        raise invalid("voice is required (profile.voice or override).")
    if not settings.get("avatar"):
        raise invalid("avatar is required (profile.avatar or override).")
    # Validate now so a bad name fails at enqueue, not minutes later in the worker.
    settings["voice_library_name"] = resolve_voice(tenant, settings["voice"])
    settings["anchor_path"] = str(resolve_anchor(tenant, settings["avatar"]))
    return settings


# ----------------------------------------------------------------------------- serialization


def _probe_video(path: Path) -> dict[str, Any]:
    if not path.is_file() or not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
            text=True,
        )
        data = json.loads(out)
        stream = (data.get("streams") or [{}])[0]
        return {
            "video_seconds": float(data.get("format", {}).get("duration") or 0) or None,
            "video_width": stream.get("width"),
            "video_height": stream.get("height"),
        }
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {}


def _artifact_entry(run: Run, name: str) -> Optional[dict[str, Any]]:
    if not run.run_directory:
        return None
    filename, content_type = ARTIFACTS[name]
    path = Path(run.run_directory) / filename
    if not path.is_file():
        return None
    return {"url": f"/api/v1/runs/{run.id}/files/{name}", "size": path.stat().st_size, "content_type": content_type}


def _metrics(run: Run) -> dict[str, Any]:
    metrics: dict[str, Any] = json.loads(run.metrics) if run.metrics else {}
    if run.run_directory:
        run_dir = Path(run.run_directory)
        narration = run_dir / "narration.txt"
        if narration.is_file() and "word_count" not in metrics:
            metrics["word_count"] = len(narration.read_text(encoding="utf-8").split())
        if "video_seconds" not in metrics:
            metrics.update(_probe_video(run_dir / "video.mp4"))
    return metrics


def _selected_sources(run: Run) -> list[dict[str, Any]]:
    if not run.run_directory:
        return []
    path = Path(run.run_directory) / "sources.json"
    if not path.is_file():
        return []
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    from services.prompting import outlet_name

    return [
        {
            "title": s.get("title"),
            "url": s.get("url"),
            "source": s.get("source"),
            "outlet": outlet_name(s.get("source") or ""),
            "published_at": s.get("published_at"),
            "source_rank_score": s.get("source_rank_score"),
            "extraction": s.get("extraction"),
        }
        for s in bundle.get("selected_sources") or []
    ]


def serialize(run: Run, full: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": run.id,
        "profile_id": str(run.profile_id) if run.profile_id is not None else None,
        "status": run.status,
        "stage": run.stage,
        "priority": run.priority,
        "search_text": run.search_text,
        "queued_at": run.queued_at.isoformat() if run.queued_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error_stage": run.error_stage,
        "error_message": run.error_message,
        "retry_of": run.retry_of,
        "cancel_requested": run.cancel_requested,
        "preflight": json.loads(run.preflight) if run.preflight else None,
        "artifacts": {name: _artifact_entry(run, name) for name in ARTIFACTS},
    }
    if full:
        data["settings"] = json.loads(run.settings) if run.settings else {}
        data["metrics"] = _metrics(run)
        data["selected_sources"] = _selected_sources(run)
    return data


# ----------------------------------------------------------------------------- operations


def _load(db, tenant: Tenant, run_id: str) -> Run:
    run = db.get(Run, run_id)
    if run is None or run.tenant_id != tenant.id:
        raise not_found(f"Run {run_id!r}")
    return run


def _new_run_id(search_text: str) -> str:
    from services import pipeline

    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    import uuid

    return f"{stamp}-{pipeline.slugify(search_text)[:64]}-{uuid.uuid4().hex[:8]}"


def enqueue_run(
    tenant: Tenant,
    profile_id: Optional[str] = None,
    overrides: Optional[dict[str, Any]] = None,
    priority: int = 0,
    wait: bool = False,
    wait_timeout_seconds: int = 3600,
    actor: Optional[str] = None,
) -> dict[str, Any]:
    settings = resolve_settings(tenant, profile_id, overrides)
    subject = settings.get("search_text") or "provided script"
    run_id = _new_run_id(subject)
    run_dir = tenancy.tenant_paths(tenant)["output"] / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    run = Run(
        id=run_id,
        tenant_id=tenant.id,
        profile_id=int(profile_id) if profile_id else None,
        status="queued",
        priority=int(priority),
        search_text=subject,
        settings=json.dumps(settings, default=str),
        run_directory=str(run_dir),
        start_stage="preflight",
    )
    with session() as db:
        db.add(run)
        db.commit()
        db.refresh(run)
        result = serialize(run)
    audit.record(tenant_id=tenant.id, actor=actor or tenancy.current_actor(), action="enqueue_run", object_type="run", object_id=run_id, after={"profile_id": profile_id, "overrides": overrides, "priority": priority})
    if wait:
        return wait_for_run(tenant, run_id, wait_timeout_seconds)
    return result


def wait_for_run(tenant: Tenant, run_id: str, timeout_seconds: int = 3600, poll_seconds: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        current = get_run(tenant, run_id)
        if current["status"] not in ACTIVE_STATUSES or time.monotonic() >= deadline:
            return current
        time.sleep(poll_seconds)


def get_run(tenant: Tenant, run_id: str) -> dict[str, Any]:
    with session() as db:
        return serialize(_load(db, tenant, run_id))


def list_runs(
    tenant: Tenant,
    status: Optional[str] = None,
    profile_id: Optional[str] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> dict[str, Any]:
    size = min(max(int(limit or 50), 1), 200)
    offset = int(cursor) if cursor else 0
    with session() as db:
        query = select(Run).where(Run.tenant_id == tenant.id)
        if status:
            query = query.where(Run.status == status)
        if profile_id:
            query = query.where(Run.profile_id == int(profile_id))
        rows = db.exec(query.order_by(Run.queued_at.desc()).offset(offset).limit(size + 1)).all()
        items = [serialize(r, full=False) for r in rows[:size]]
    return {"items": items, "next_cursor": str(offset + size) if len(rows) > size else None}


def cancel_run(tenant: Tenant, run_id: str, actor: Optional[str] = None) -> dict[str, Any]:
    with session() as db:
        run = _load(db, tenant, run_id)
        before = run.status
        if run.status == "queued":
            run.status = "cancelled"
            run.finished_at = utcnow()
        elif run.status in ("preflight", "running"):
            run.cancel_requested = True
            if run.stage == "video":
                _interrupt_comfyui()
        else:
            raise conflict(f"Run {run_id!r} is already {run.status}.")
        db.add(run)
        db.commit()
        db.refresh(run)
        result = serialize(run)
    audit.record(tenant_id=tenant.id, actor=actor or tenancy.current_actor(), action="cancel_run", object_type="run", object_id=run_id, before={"status": before}, after={"status": result["status"], "cancel_requested": result["cancel_requested"]})
    return result


def delete_run(tenant: Tenant, run_id: str, actor: Optional[str] = None) -> dict[str, Any]:
    """Remove a finished run: its row and its directory on disk. Queued or running runs must be cancelled first."""
    with session() as db:
        run = _load(db, tenant, run_id)
        if run.status in ("queued", "preflight", "running"):
            raise conflict(f"Run {run_id!r} is {run.status}; cancel it before deleting.")
        before = serialize(run)
        run_dir = Path(run.run_directory) if run.run_directory else None
        db.delete(run)
        db.commit()
    if run_dir and run_dir.is_dir():
        shutil.rmtree(run_dir, ignore_errors=True)
    audit.record(tenant_id=tenant.id, actor=actor or tenancy.current_actor(), action="delete_run", object_type="run", object_id=run_id, before=before)
    return {"deleted": run_id}


def _interrupt_comfyui() -> None:
    import importlib

    base = os.environ.get("VIDEO_COMFYUI_BASE_URL", "").strip().rstrip("/")
    if not base:
        return
    try:
        importlib.import_module("requests").post(f"{base}/interrupt", timeout=10)
    except Exception:  # noqa: BLE001 - best effort
        pass


def resume_stage(run_dir: Path) -> Optional[str]:
    """Where a retry picks up, judged from the artifacts on disk, newest first.

    video clips -> audio chunks -> script -> sources -> nothing. Returns None when every chunk is
    already rendered and the final video exists.
    """
    script_path = run_dir / "narration.txt"
    script = script_path.read_text(encoding="utf-8").strip() if script_path.is_file() else ""
    manifest = segments.load_manifest(run_dir) if script else None
    if manifest and manifest.get("script") == script and manifest.get("chunks"):
        statuses = [chunk.get("status") for chunk in manifest["chunks"]]
        if all(status == "rendered" for status in statuses) and (run_dir / "video.mp4").is_file():
            return None
        if all(status in ("narrated", "rendered") for status in statuses):
            return "video"
        return "images"  # heroes that exist are kept; the stage then hands over to audio
    if script:
        return "images"
    if (run_dir / "sources.json").is_file():
        return "script"
    return "preflight"


def retry_run(tenant: Tenant, run_id: str, actor: Optional[str] = None) -> dict[str, Any]:
    """Queue a new run that resumes the failed run where its artifacts on disk stop, reusing its directory."""
    with session() as db:
        failed = _load(db, tenant, run_id)
        if failed.status not in ("failed", "cancelled"):
            raise conflict(f"Run {run_id!r} is {failed.status}; only failed or cancelled runs can be retried.")
        start_stage = resume_stage(Path(failed.run_directory)) if failed.run_directory else "preflight"
        if start_stage is None:
            raise conflict(f"Run {run_id!r} already has every chunk rendered and a final video; nothing to retry.")
        new_id = _new_run_id(failed.search_text or "retry")
        run = Run(
            id=new_id,
            tenant_id=tenant.id,
            profile_id=failed.profile_id,
            status="queued",
            priority=failed.priority,
            search_text=failed.search_text,
            settings=failed.settings,
            run_directory=failed.run_directory,
            retry_of=failed.id,
            start_stage=start_stage,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        result = serialize(run)
    audit.record(tenant_id=tenant.id, actor=actor or tenancy.current_actor(), action="retry_run", object_type="run", object_id=new_id, after={"retry_of": run_id, "start_stage": start_stage})
    return result


def artifact_path(tenant: Tenant, run_id: str, artifact: str) -> tuple[Path, str]:
    if artifact not in ARTIFACTS:
        raise invalid(f"Unknown artifact {artifact!r}. Valid: {', '.join(ARTIFACTS)}.")
    with session() as db:
        run = _load(db, tenant, run_id)
    if not run.run_directory:
        raise not_found(f"Artifact {artifact!r} for run {run_id!r}")
    filename, content_type = ARTIFACTS[artifact]
    path = Path(run.run_directory) / filename
    if not path.is_file():
        raise not_found(f"Artifact {artifact!r} for run {run_id!r}")
    return path, content_type


def chunk_hero_path(tenant: Tenant, run_id: str, index: int) -> Path:
    """The hero image of one chunk, for the run detail page."""
    with session() as db:
        run = _load(db, tenant, run_id)
        run_dir = Path(run.run_directory) if run.run_directory else None
    if run_dir is None:
        raise not_found(f"Hero for chunk {index} of run {run_id!r}")
    from services import segments  # local: keep runs importable without the media helpers

    hero = segments.hero_for(run_dir / "chunks" / f"{int(index):02d}")
    if hero is None:
        raise not_found(f"Hero for chunk {index} of run {run_id!r}")
    return hero


def get_run_file(tenant: Tenant, run_id: str, artifact: str, inline: bool = False) -> dict[str, Any]:
    path, content_type = artifact_path(tenant, run_id, artifact)
    result: dict[str, Any] = {"url": f"/api/v1/runs/{run_id}/files/{artifact}", "size": path.stat().st_size, "content_type": content_type}
    if inline:
        if artifact not in TEXT_ARTIFACTS:
            raise invalid(f"Artifact {artifact!r} is binary; fetch it from the url.")
        result["content"] = path.read_text(encoding="utf-8", errors="replace")
    return result
