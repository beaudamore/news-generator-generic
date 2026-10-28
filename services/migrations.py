"""Versioned schema migrations.

Every change to services/models.py, and every data transform a code change needs, is a numbered
entry in MIGRATIONS. The database records which versions have been applied in `schema_version`.
On startup (services.db.init_db) and from `scripts/init_db.py`:

  fresh database   -> tables are created from the models and stamped at the latest version
  existing database -> every migration above the recorded version runs, in order, each in its own transaction

Rules (see docs/migrations.md):
  1. Never edit or delete an entry that has shipped. Add a new one.
  2. Versions are consecutive integers. LATEST is derived, never typed by hand.
  3. A migration must be safe to run on a production database: check before dropping, copy before retyping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Connection], None]


def _v1_baseline(connection: Connection) -> None:
    """Tables as defined by services.models at the time versioning began. Created by SQLModel; nothing to do."""


def _v2_drop_avatars_table(connection: Connection) -> None:
    """Avatars live only in assets/avatars/<tenant>/ (png + json); the table duplicated them and could drift."""
    if inspect(connection).has_table("avatars"):
        connection.execute(text('DROP TABLE "avatars"'))


def _v3_prompts_on_profiles(connection: Connection) -> None:
    """Prompts move from one set per tenant to a copy per profile; the tenant rows seed the profiles, then go away."""
    inspector = inspect(connection)
    columns = {c["name"] for c in inspector.get_columns("search_profiles")}
    for column in ("summarize_prompt", "video_visual_prompt", "video_sound_prompt"):
        if column not in columns:
            connection.execute(text(f'ALTER TABLE "search_profiles" ADD COLUMN "{column}" TEXT'))
    if inspector.has_table("prompts"):
        for kind, column in (("summarize", "summarize_prompt"), ("video_visual", "video_visual_prompt"), ("video_sound", "video_sound_prompt")):
            connection.execute(text(
                f'UPDATE "search_profiles" SET "{column}" = (SELECT body FROM prompts WHERE prompts.tenant_id = search_profiles.tenant_id AND prompts.kind = :kind) '
                f'WHERE "{column}" IS NULL'
            ), {"kind": kind})
        connection.execute(text('DROP TABLE "prompts"'))


def _v4_hero_fields_on_profiles(connection: Connection) -> None:
    """Segment hero images: each profile gets the hero prompt and the hero image template, filled from the defaults."""
    from services import avatars, prompts  # local: keep the migration module free of service imports at load time

    columns = {c["name"] for c in inspect(connection).get_columns("search_profiles")}
    for column in ("hero_prompt", "hero_workflow"):
        if column not in columns:
            connection.execute(text(f'ALTER TABLE "search_profiles" ADD COLUMN "{column}" TEXT'))
    connection.execute(text('UPDATE "search_profiles" SET "hero_prompt" = :body WHERE "hero_prompt" IS NULL'), {"body": prompts.default_body("hero_image") or None})
    connection.execute(text('UPDATE "search_profiles" SET "hero_workflow" = :name WHERE "hero_workflow" IS NULL'), {"name": avatars.hero_default_workflow() or None})


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: tables from services.models", _v1_baseline),
    Migration(2, "drop avatars table; the tenant avatar folder is the source of truth", _v2_drop_avatars_table),
    Migration(3, "prompts stored per profile, seeded from the tenant rows; drop prompts table", _v3_prompts_on_profiles),
    Migration(4, "hero_prompt and hero_workflow on search_profiles, filled from the defaults", _v4_hero_fields_on_profiles),
]

LATEST = MIGRATIONS[-1].version

for _index, _migration in enumerate(MIGRATIONS, start=1):
    assert _migration.version == _index, f"migration versions must be consecutive; got {_migration.version} at position {_index}"


def _ensure_version_table(connection: Connection) -> None:
    connection.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"))


def current_version(engine: Engine) -> int:
    with engine.begin() as connection:
        _ensure_version_table(connection)
        row = connection.execute(text("SELECT MAX(version) FROM schema_version")).fetchone()
    return int(row[0] or 0) if row else 0


def _record(connection: Connection, migration: Migration) -> None:
    connection.execute(
        text("INSERT INTO schema_version (version, name, applied_at) VALUES (:version, :name, :applied_at)"),
        {"version": migration.version, "name": migration.name, "applied_at": datetime.now(timezone.utc).isoformat()},
    )


def stamp_latest(engine: Engine) -> list[str]:
    """A fresh database already matches the models; record every migration as applied without running them."""
    with engine.begin() as connection:
        _ensure_version_table(connection)
        for migration in MIGRATIONS:
            _record(connection, migration)
    return [f"{m.version} {m.name}" for m in MIGRATIONS]


def apply_pending(engine: Engine) -> list[str]:
    """Run every migration above the recorded version, each in its own transaction, recording it on success."""
    applied: list[str] = []
    start = current_version(engine)
    for migration in MIGRATIONS:
        if migration.version <= start:
            continue
        with engine.begin() as connection:
            migration.apply(connection)
            _record(connection, migration)
        applied.append(f"{migration.version} {migration.name}")
    return applied


def status(engine: Engine) -> dict:
    current = current_version(engine)
    return {"current": current, "latest": LATEST, "pending": [f"{m.version} {m.name}" for m in MIGRATIONS if m.version > current]}
