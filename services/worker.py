"""Run worker: one background thread that executes queued runs stage by stage.

Sequential for now (one run at a time). The pipelining described in the plan,
audio for run N+1 while run N renders, is a later optimization. Every stage
reuses the pipeline stage functions unchanged, so preflight, template
validation, and run.log behave exactly as they do for the blocking /generate.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from sqlmodel import select

from services import logging_setup, pipeline, preflight, prompts, tenancy
from services.db import session
from services.models import Run, Tenant, utcnow
from services.runs import STAGES

LOGGER = logging.getLogger("news_pipeline")
POLL_SECONDS = 2.0


class CancelledRun(Exception):
    pass


def _tenant(tenant_id: int) -> Tenant:
    with session() as db:
        tenant = db.get(Tenant, tenant_id)
        db.expunge(tenant)
        return tenant


def _update(run_id: str, **fields: Any) -> None:
    with session() as db:
        run = db.get(Run, run_id)
        for key, value in fields.items():
            setattr(run, key, value)
        db.add(run)
        db.commit()


def _cancel_requested(run_id: str) -> bool:
    with session() as db:
        return bool(db.get(Run, run_id).cancel_requested)


def _checkpoint(run_id: str, stage: str) -> None:
    if _cancel_requested(run_id):
        raise CancelledRun(stage)
    _update(run_id, stage=stage)


def execute_run(run_id: str) -> None:
    """Run every stage from the run's start_stage. Sets status, stage, timings, error, metrics."""
    with session() as db:
        run = db.get(Run, run_id)
        tenant_id, run_dir = run.tenant_id, Path(run.run_directory)
        settings = json.loads(run.settings or "{}")
        start_stage = run.start_stage or "preflight"
        retry_of = run.retry_of
    tenant = _tenant(tenant_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    tokens = (logging_setup.run_id_var.set(run_id), logging_setup.tenant_id_var.set(tenant.id), logging_setup.tenant_slug_var.set(tenant.slug))
    try:
        _execute_stages(run_id, run_dir, tenant, settings, start_stage, retry_of)
    finally:
        logging_setup.run_id_var.reset(tokens[0])
        logging_setup.tenant_id_var.reset(tokens[1])
        logging_setup.tenant_slug_var.reset(tokens[2])


def _execute_stages(run_id: str, run_dir: Path, tenant: Tenant, settings: dict[str, Any], start_stage: str, retry_of: Optional[str]) -> None:
    _update(run_id, status="running", started_at=utcnow(), stage=start_stage)
    pipeline.log_event(run_dir, "run", f"worker start run_id={run_id} tenant={tenant.slug} start_stage={start_stage}" + (f" retry_of={retry_of}" if retry_of else ""))

    metrics: dict[str, Any] = {}
    script_text = settings.get("script_text")
    voice_library = settings["voice_library_name"]
    anchor_path = Path(settings["anchor_path"])
    stage_index = STAGES.index(start_stage)

    try:
        # ---- preflight
        if stage_index <= STAGES.index("preflight"):
            _checkpoint(run_id, "preflight")
            checks = preflight.run_preflight(voice=voice_library, anchor_path=anchor_path, need_search=not script_text, need_llm=not script_text)
            for check in checks["checks"]:
                pipeline.log_event(run_dir, "preflight", f"{check['service']}: {check['status']} ({check['latency_ms']} ms) {check['detail']}")
            (run_dir / "preflight.json").write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
            _update(run_id, preflight=json.dumps(checks))
            if not checks["ok"]:
                raise pipeline.PipelineError("preflight", f"Preflight failed: {preflight.failure_summary(checks)}")

        # ---- search + script
        if stage_index <= STAGES.index("script"):
            _checkpoint(run_id, "script")
            result = pipeline.generate_script_stage(
                run_id=run_id,
                run_dir=run_dir,
                search_text=settings.get("search_text") or "provided script",
                script_text=script_text,
                categories=settings.get("categories"),
                language=settings.get("language"),
                safesearch=settings.get("safesearch"),
                max_articles=settings.get("max_articles"),
                max_age_days=settings.get("max_age_days"),
                # Rank by the profile's own search terms unless it sets a rank query; the tenant-wide
                # source_rank_query prompt would bias every profile toward one topic.
                rank_query=settings.get("rank_query") or settings.get("search_text") or None,
                rank_candidates=settings.get("rank_candidates"),
                summarize_prompt=settings.get("summarize_prompt") or prompts.default_body("summarize"),
                intended_video_max_seconds=settings.get("intended_video_max_seconds"),
                wpm=settings.get("wpm"),
            )
            script = result["script"]
            metrics.update({"word_count": result["word_count"], "target_words": result["target_words"]})
        else:
            script = (run_dir / "narration.txt").read_text(encoding="utf-8").strip()

        # ---- images (segment heroes)
        if stage_index <= STAGES.index("images"):
            _checkpoint(run_id, "images")
            heroes = pipeline.generate_heroes_stage(
                run_id=run_id,
                run_dir=run_dir,
                script=script,
                anchor_path=anchor_path,
                hero_prompt=settings.get("hero_prompt"),
                hero_workflow=settings.get("hero_workflow"),
                should_cancel=lambda: _cancel_requested(run_id),
            )
            metrics["heroes"] = heroes.get("generated", 0) + heroes.get("reused", 0)
            if heroes.get("failed"):
                metrics["heroes_failed"] = heroes["failed"]

        # ---- audio
        if stage_index <= STAGES.index("audio"):
            _checkpoint(run_id, "audio")
            audio = pipeline.narrate_segmented(run_id=run_id, run_dir=run_dir, script=script, voice=voice_library, should_cancel=lambda: _cancel_requested(run_id))
            metrics["audio_seconds"] = audio["audio_duration_seconds"]
            metrics["chunks"] = audio.get("chunks")
        else:
            metrics["audio_seconds"] = pipeline.generate_video_comfyui.wav_duration_seconds(run_dir / "narration.wav")

        # ---- video
        _checkpoint(run_id, "video")
        video = pipeline.render_segmented(
            run_id=run_id,
            run_dir=run_dir,
            script=script,
            anchor_path=anchor_path,
            video_prompt=settings.get("video_visual_prompt") or prompts.default_body("video_visual") or None,
            video_sound_prompt=settings.get("video_sound_prompt") or prompts.default_body("video_sound") or None,
            prompt_enhance=settings.get("prompt_enhance"),
            should_cancel=lambda: _cancel_requested(run_id),
        )
        metrics.update({"video_seconds": video["video_duration_seconds"], "video_width": video.get("video_width"), "video_height": video.get("video_height"), "chunks": video.get("chunks", metrics.get("chunks"))})

        _update(run_id, status="done", stage="video", finished_at=utcnow(), metrics=json.dumps(metrics))
        pipeline.log_event(run_dir, "run", f"worker done run_id={run_id} metrics={json.dumps(metrics)}")

    except CancelledRun as exc:
        _update(run_id, status="cancelled", stage=str(exc), finished_at=utcnow(), metrics=json.dumps(metrics))
        pipeline.log_event(run_dir, "run", f"cancelled at stage {exc}")
    except pipeline.PipelineError as exc:
        if _cancel_requested(run_id):
            _update(run_id, status="cancelled", stage=exc.stage, finished_at=utcnow(), metrics=json.dumps(metrics))
            pipeline.log_event(run_dir, "run", f"cancelled during stage {exc.stage}: {exc.message}")
        else:
            _update(run_id, status="failed", stage=exc.stage, error_stage=exc.stage, error_message=exc.message, finished_at=utcnow(), metrics=json.dumps(metrics))
            pipeline.log_event(run_dir, "run", f"failed at stage {exc.stage}: {exc.message}")
    except Exception as exc:  # noqa: BLE001 - the worker must never die on one run
        with session() as db:
            stage = db.get(Run, run_id).stage or "unknown"
        message = f"{type(exc).__name__}: {exc}"
        _update(run_id, status="failed", error_stage=stage, error_message=message, finished_at=utcnow(), metrics=json.dumps(metrics))
        pipeline.log_event(run_dir, "run", f"failed at stage {stage}: {message}\n{traceback.format_exc()}")


def next_queued_run_id() -> Optional[str]:
    with session() as db:
        run = db.exec(
            select(Run).where(Run.status == "queued").order_by(Run.priority.desc(), Run.queued_at.asc())
        ).first()
        return run.id if run else None


def recover_interrupted_runs() -> int:
    """On startup, runs left in progress by a previous process are marked failed."""
    with session() as db:
        stale = db.exec(select(Run).where(Run.status.in_(("preflight", "running")))).all()
        for run in stale:
            run.status = "failed"
            run.error_stage = run.stage or "unknown"
            run.error_message = "interrupted by restart"
            run.finished_at = utcnow()
            db.add(run)
        db.commit()
        if stale:
            LOGGER.warning("[worker] marked %d interrupted run(s) failed", len(stale))
        return len(stale)


class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.current_run_id: Optional[str] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="news-worker", daemon=True)
        self._thread.start()
        LOGGER.info("[worker] started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            run_id = None
            try:
                run_id = next_queued_run_id()
                if run_id is None:
                    self._stop.wait(POLL_SECONDS)
                    continue
                self.current_run_id = run_id
                execute_run(run_id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("[worker] unexpected error on run %s: %s", run_id, exc)
                self._stop.wait(POLL_SECONDS)
            finally:
                self.current_run_id = None


_worker: Optional[Worker] = None


def enabled() -> bool:
    return os.environ.get("NEWS_WORKER_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def start_worker() -> Optional[Worker]:
    global _worker
    if not enabled():
        LOGGER.info("[worker] disabled by NEWS_WORKER_ENABLED")
        return None
    recover_interrupted_runs()
    if _worker is None:
        _worker = Worker()
        _worker.start()
    return _worker


def stop_worker() -> None:
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None
