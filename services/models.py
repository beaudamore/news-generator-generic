"""Database schema (SQLModel). Every table except `tenants` is tenant-scoped.

ANY CHANGE TO THIS FILE NEEDS A MIGRATION: add a numbered entry to services/migrations.py
and a row to docs/migrations.md. Existing databases are updated only by migrations.

JSON-valued fields (preflight, meta, before, after) are stored as text. Avatars and voices
have no table: the tenant folders under assets/ are their source of truth. Prompts have no
table either: the defaults are files (services.prompts) and each profile stores its own copy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(SQLModel, table=True):
    __tablename__ = "tenants"

    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    name: str
    active: bool = True
    is_admin: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class ApiKey(SQLModel, table=True):
    __tablename__ = "api_keys"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenants.id", index=True)
    key_hash: str = Field(index=True, unique=True)
    key_prefix: str
    label: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    revoked_at: Optional[datetime] = None


class Voice(SQLModel, table=True):
    __tablename__ = "voices"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenants.id", index=True)
    name: str
    library_name: str
    sample_path: Optional[str] = None
    language: str = "en"
    created_at: datetime = Field(default_factory=utcnow)


class SearchProfile(SQLModel, table=True):
    __tablename__ = "search_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenants.id", index=True)
    name: str
    search_text: str
    categories: Optional[str] = None
    language: Optional[str] = None
    safesearch: Optional[str] = None
    max_articles: Optional[int] = None
    max_age_days: Optional[int] = None
    rank_query: Optional[str] = None
    rank_candidates: Optional[int] = None
    voice: Optional[str] = None
    avatar: Optional[str] = None
    intended_video_max_seconds: Optional[float] = None
    wpm: Optional[float] = None
    # Prompts are per profile. A new profile is filled from the default files (services.prompts).
    summarize_prompt: Optional[str] = None
    video_visual_prompt: Optional[str] = None
    video_sound_prompt: Optional[str] = None
    # Segment hero images: the instruction that turns a segment into an image prompt, and the image template file name.
    hero_prompt: Optional[str] = None
    hero_workflow: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Schedule(SQLModel, table=True):
    __tablename__ = "schedules"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenants.id", index=True)
    profile_id: int = Field(foreign_key="search_profiles.id", index=True)
    cron: str
    enabled: bool = True
    name: Optional[str] = None
    timezone: str = "UTC"
    last_run_id: Optional[str] = None
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = Field(default_factory=utcnow)
    updated_at: Optional[datetime] = Field(default_factory=utcnow)


class Run(SQLModel, table=True):
    __tablename__ = "runs"

    id: str = Field(primary_key=True)
    tenant_id: int = Field(foreign_key="tenants.id", index=True)
    profile_id: Optional[int] = Field(default=None, foreign_key="search_profiles.id")
    status: str = Field(default="queued", index=True)
    stage: Optional[str] = None
    priority: int = 0
    queued_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_stage: Optional[str] = None
    error_message: Optional[str] = None
    run_directory: Optional[str] = None
    preflight: Optional[str] = None
    search_text: Optional[str] = None
    settings: Optional[str] = None
    metrics: Optional[str] = None
    retry_of: Optional[str] = None
    start_stage: Optional[str] = None
    cancel_requested: bool = False


class UsageEvent(SQLModel, table=True):
    __tablename__ = "usage_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenants.id", index=True)
    run_id: Optional[str] = Field(default=None, foreign_key="runs.id", index=True)
    stage: Optional[str] = None
    resource: str = Field(index=True)
    quantity: float
    unit: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    meta: Optional[str] = None


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="tenants.id", index=True)
    actor: str
    action: str = Field(index=True)
    object_type: str
    object_id: Optional[str] = None
    before: Optional[str] = None
    after: Optional[str] = None
    ip: Optional[str] = None
    correlation_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
