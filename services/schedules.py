"""Schedules: a cron expression bound to a search profile. The scheduler thread fires them."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlmodel import select

from services import audit, profiles, runs, tenancy
from services.db import session
from services.errors import invalid, not_found
from services.models import Schedule, Tenant, utcnow


def _tz(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo((name or "UTC").strip() or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise invalid(f"Unknown timezone {name!r}.") from exc


def validate_cron(expression: str) -> str:
    expression = (expression or "").strip()
    if not expression or not croniter.is_valid(expression):
        raise invalid(f"Invalid cron expression {expression!r}. Use five fields: minute hour day month weekday.")
    return expression


def compute_next(expression: str, tz_name: Optional[str], after: Optional[datetime] = None) -> datetime:
    """Next fire time in UTC, evaluated in the schedule's timezone."""
    tz = _tz(tz_name)
    base = (after or utcnow()).astimezone(tz)
    nxt = croniter(expression, base).get_next(datetime)
    return nxt.astimezone(timezone.utc)


def _serialize(schedule: Schedule) -> dict[str, Any]:
    return {
        "id": str(schedule.id),
        "name": schedule.name,
        "profile_id": str(schedule.profile_id),
        "cron": schedule.cron,
        "timezone": schedule.timezone or "UTC",
        "enabled": schedule.enabled,
        "last_run_id": schedule.last_run_id,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "last_error": schedule.last_error,
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
        "updated_at": schedule.updated_at.isoformat() if schedule.updated_at else None,
    }


def _load(db, tenant: Tenant, schedule_id: str) -> Schedule:
    try:
        pk = int(schedule_id)
    except (TypeError, ValueError) as exc:
        raise not_found(f"Schedule {schedule_id!r}") from exc
    schedule = db.get(Schedule, pk)
    if schedule is None or schedule.tenant_id != tenant.id:
        raise not_found(f"Schedule {schedule_id!r}")
    return schedule


def list_schedules(tenant: Tenant) -> list[dict[str, Any]]:
    with session() as db:
        rows = db.exec(select(Schedule).where(Schedule.tenant_id == tenant.id).order_by(Schedule.id)).all()
        return [_serialize(s) for s in rows]


def get_schedule(tenant: Tenant, schedule_id: str) -> dict[str, Any]:
    with session() as db:
        return _serialize(_load(db, tenant, schedule_id))


def upsert_schedule(
    tenant: Tenant,
    profile_id: Optional[str] = None,
    cron: Optional[str] = None,
    enabled: Optional[bool] = None,
    name: Optional[str] = None,
    timezone_name: Optional[str] = None,
    schedule_id: Optional[str] = None,
    actor: Optional[str] = None,
) -> dict[str, Any]:
    with session() as db:
        if schedule_id:
            schedule = _load(db, tenant, schedule_id)
            before = _serialize(schedule)
            action = "update_schedule"
        else:
            if not profile_id or not cron:
                raise invalid("profile_id and cron are required to create a schedule.")
            schedule = Schedule(tenant_id=tenant.id, profile_id=0, cron="")
            before = None
            action = "create_schedule"
        if profile_id is not None:
            profiles.get_profile(tenant, profile_id)  # raises not_found if it is not this tenant's
            schedule.profile_id = int(profile_id)
        if cron is not None:
            schedule.cron = validate_cron(cron)
        if timezone_name is not None:
            _tz(timezone_name)
            schedule.timezone = timezone_name.strip() or "UTC"
        if name is not None:
            schedule.name = name.strip() or None
        if enabled is not None:
            schedule.enabled = bool(enabled)
        schedule.next_run_at = compute_next(schedule.cron, schedule.timezone) if schedule.enabled else None
        schedule.updated_at = utcnow()
        if schedule.created_at is None:
            schedule.created_at = utcnow()
        db.add(schedule)
        db.commit()
        db.refresh(schedule)
        result = _serialize(schedule)
    audit.record(tenant_id=tenant.id, actor=actor or tenancy.current_actor(), action=action, object_type="schedule", object_id=result["id"], before=before, after=result)
    return result


def delete_schedule(tenant: Tenant, schedule_id: str, actor: Optional[str] = None) -> dict[str, Any]:
    with session() as db:
        schedule = _load(db, tenant, schedule_id)
        before = _serialize(schedule)
        db.delete(schedule)
        db.commit()
    audit.record(tenant_id=tenant.id, actor=actor or tenancy.current_actor(), action="delete_schedule", object_type="schedule", object_id=schedule_id, before=before)
    return {"deleted": schedule_id}


def fire(tenant: Tenant, schedule_id: str, actor: str = "scheduler") -> dict[str, Any]:
    """Enqueue the schedule's profile now and record the result on the schedule. Used by run_now and the scheduler."""
    with session() as db:
        schedule = _load(db, tenant, schedule_id)
        profile_id = str(schedule.profile_id)
    try:
        run = runs.enqueue_run(tenant, profile_id=profile_id, actor=actor)
        error = None
    except Exception as exc:  # noqa: BLE001 - record and re-raise so callers see the failure
        run = None
        error = f"{type(exc).__name__}: {exc}"
    with session() as db:
        schedule = _load(db, tenant, schedule_id)
        if run is not None:
            schedule.last_run_id = run["id"]
            schedule.last_run_at = utcnow()
            schedule.last_error = None
        else:
            schedule.last_error = error
        db.add(schedule)
        db.commit()
    if run is None:
        raise runs.ApiError("pipeline_failed", error or "enqueue failed", stage="enqueue")
    return run


def run_now(tenant: Tenant, schedule_id: str, actor: Optional[str] = None) -> dict[str, Any]:
    return fire(tenant, schedule_id, actor or tenancy.current_actor())
