"""Scheduler: fires due schedules by enqueuing runs. Runs beside the worker in the API process."""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from sqlmodel import select

from services import preflight, schedules
from services.db import session
from services.models import Schedule, Tenant, utcnow

LOGGER = logging.getLogger("news_pipeline")
POLL_SECONDS = 15.0


def _record_skip(schedule_id: int, reason: str) -> None:
    with session() as db:
        schedule = db.get(Schedule, schedule_id)
        schedule.last_error = reason
        schedule.next_run_at = schedules.compute_next(schedule.cron, schedule.timezone)
        schedule.updated_at = utcnow()
        db.add(schedule)
        db.commit()


def fire_due(now=None) -> int:
    """Fire every enabled schedule whose next_run_at has passed. Returns how many were processed."""
    now = now or utcnow()
    with session() as db:
        due = db.exec(
            select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None), Schedule.next_run_at <= now)
        ).all()
        items = [(s.id, s.tenant_id) for s in due]
    if not items:
        return 0
    checks = preflight.run_preflight(need_video=True)
    for schedule_id, tenant_id in items:
        with session() as db:
            tenant = db.get(Tenant, tenant_id)
            db.expunge(tenant)
        if not tenant.active:
            _record_skip(schedule_id, "skipped: tenant inactive")
            continue
        if not checks["ok"]:
            reason = f"skipped: preflight failed ({preflight.failure_summary(checks)})"
            _record_skip(schedule_id, reason)
            LOGGER.warning("[scheduler] schedule %s %s", schedule_id, reason)
            continue
        try:
            run = schedules.fire(tenant, str(schedule_id), actor="scheduler")
            LOGGER.info("[scheduler] schedule %s enqueued run %s", schedule_id, run["id"])
        except Exception as exc:  # noqa: BLE001 - one bad schedule must not stop the others
            LOGGER.error("[scheduler] schedule %s failed to enqueue: %s", schedule_id, exc)
        with session() as db:
            schedule = db.get(Schedule, schedule_id)
            schedule.next_run_at = schedules.compute_next(schedule.cron, schedule.timezone)  # from write time, never the past
            schedule.updated_at = utcnow()
            db.add(schedule)
            db.commit()
    return len(items)


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="news-scheduler", daemon=True)
        self._thread.start()
        LOGGER.info("[scheduler] started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                fire_due()
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("[scheduler] unexpected error: %s", exc)
            self._stop.wait(POLL_SECONDS)


_scheduler: Optional[Scheduler] = None


def enabled() -> bool:
    return os.environ.get("NEWS_SCHEDULER_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def start_scheduler() -> Optional[Scheduler]:
    global _scheduler
    if not enabled():
        LOGGER.info("[scheduler] disabled by NEWS_SCHEDULER_ENABLED")
        return None
    if _scheduler is None:
        _scheduler = Scheduler()
        _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None
