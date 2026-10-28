"""Search profiles: the per-tenant bundle of search, ranking, voice, avatar, and timing settings a run uses."""

from __future__ import annotations

from typing import Any, Optional

from sqlmodel import select

from services import audit, avatars, prompts
from services.db import session
from services.errors import conflict, invalid, not_found
from services.models import Schedule, SearchProfile, Tenant, utcnow

EDITABLE_FIELDS = (
    "name", "search_text", "categories", "language", "safesearch", "max_articles", "max_age_days",
    "rank_query", "rank_candidates", "voice", "avatar", "intended_video_max_seconds", "wpm",
    "summarize_prompt", "video_visual_prompt", "video_sound_prompt", "hero_prompt", "hero_workflow",
)
PROMPT_FIELDS = ("summarize_prompt", "video_visual_prompt", "video_sound_prompt", "hero_prompt")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _serialize(profile: SearchProfile) -> dict[str, Any]:
    data = {field: getattr(profile, field) for field in EDITABLE_FIELDS}
    data.update({"id": str(profile.id), "created_at": profile.created_at.isoformat(), "updated_at": profile.updated_at.isoformat()})
    return data


def _validate(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in EDITABLE_FIELDS:
            raise invalid(f"Unknown profile field {key!r}.")
        if isinstance(value, str):
            value = value.strip() or None
        clean[key] = value
    if "name" in clean and not clean["name"]:
        raise invalid("name is required.")
    if "search_text" in clean and not clean["search_text"]:
        raise invalid("search_text is required.")
    for key in ("max_articles", "max_age_days", "rank_candidates"):
        if clean.get(key) is not None:
            try:
                clean[key] = int(clean[key])
            except (TypeError, ValueError) as exc:
                raise invalid(f"{key} must be an integer.") from exc
            if clean[key] < 0:
                raise invalid(f"{key} must not be negative.")
    for key in ("intended_video_max_seconds", "wpm"):
        if clean.get(key) is not None:
            try:
                clean[key] = float(clean[key])
            except (TypeError, ValueError) as exc:
                raise invalid(f"{key} must be a number.") from exc
            if clean[key] <= 0:
                raise invalid(f"{key} must be greater than zero.")
    return clean


def _paginate(limit: Optional[int], cursor: Optional[str]) -> tuple[int, int]:
    size = min(max(int(limit or DEFAULT_LIMIT), 1), MAX_LIMIT)
    try:
        offset = int(cursor) if cursor else 0
    except ValueError as exc:
        raise invalid("cursor is not valid.") from exc
    return size, max(offset, 0)


def list_profiles(tenant: Tenant, limit: Optional[int] = None, cursor: Optional[str] = None) -> dict[str, Any]:
    size, offset = _paginate(limit, cursor)
    with session() as db:
        rows = db.exec(
            select(SearchProfile).where(SearchProfile.tenant_id == tenant.id).order_by(SearchProfile.name).offset(offset).limit(size + 1)
        ).all()
    items = [_serialize(p) for p in rows[:size]]
    return {"items": items, "next_cursor": str(offset + size) if len(rows) > size else None}


def _load(db, tenant: Tenant, profile_id: str) -> SearchProfile:
    try:
        pk = int(profile_id)
    except (TypeError, ValueError) as exc:
        raise not_found(f"Profile {profile_id!r}") from exc
    profile = db.get(SearchProfile, pk)
    if profile is None or profile.tenant_id != tenant.id:
        raise not_found(f"Profile {profile_id!r}")
    return profile


def get_profile(tenant: Tenant, profile_id: str) -> dict[str, Any]:
    with session() as db:
        return _serialize(_load(db, tenant, profile_id))


def upsert_profile(tenant: Tenant, fields: dict[str, Any], profile_id: Optional[str] = None, actor: str = "system") -> dict[str, Any]:
    clean = _validate(fields)
    with session() as db:
        if profile_id:
            profile = _load(db, tenant, profile_id)
            before = _serialize(profile)
            action = "update_search_profile"
        else:
            if not clean.get("name") or not clean.get("search_text"):
                raise invalid("name and search_text are required to create a profile.")
            profile = SearchProfile(tenant_id=tenant.id, name=clean["name"], search_text=clean["search_text"])
            before = None
            action = "create_search_profile"
        if "name" in clean:
            duplicate = db.exec(
                select(SearchProfile).where(SearchProfile.tenant_id == tenant.id, SearchProfile.name == clean["name"])
            ).first()
            if duplicate is not None and duplicate.id != profile.id:
                raise conflict(f"A profile named {clean['name']!r} already exists.")
        for key, value in clean.items():
            setattr(profile, key, value)
        # A profile always carries its own prompts: a new one is filled from the default files,
        # and a field cleared on save is refilled the same way.
        defaults = prompts.profile_defaults()
        for key in PROMPT_FIELDS:
            if not getattr(profile, key, None):
                setattr(profile, key, defaults[key] or None)
        if not profile.hero_workflow:
            profile.hero_workflow = avatars.hero_default_workflow() or None
        profile.updated_at = utcnow()
        db.add(profile)
        db.commit()
        db.refresh(profile)
        result = _serialize(profile)
    audit.record(tenant_id=tenant.id, actor=actor, action=action, object_type="search_profile", object_id=result["id"], before=before, after=result)
    return result


def delete_profile(tenant: Tenant, profile_id: str, actor: str = "system") -> dict[str, Any]:
    with session() as db:
        profile = _load(db, tenant, profile_id)
        referenced = db.exec(select(Schedule).where(Schedule.profile_id == profile.id)).first()
        if referenced is not None:
            raise conflict(f"Profile {profile_id!r} is referenced by schedule {referenced.id}; delete the schedule first.")
        before = _serialize(profile)
        db.delete(profile)
        db.commit()
    audit.record(tenant_id=tenant.id, actor=actor, action="delete_search_profile", object_type="search_profile", object_id=profile_id, before=before)
    return {"deleted": profile_id}
