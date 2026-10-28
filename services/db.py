"""Database engine and session helpers.

SQLite by default at `DATA_DIRECTORY/news.db`. `DATABASE_URL` overrides the URL
entirely (Postgres later). `init_db()` creates a fresh database from the models
and stamps it at the latest schema version, or applies pending migrations from
services/migrations.py to an existing one. See docs/migrations.md.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from services import migrations, models  # noqa: F401  (models registers every table on SQLModel.metadata)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_engine = None


def data_directory() -> Path:
    root = Path(os.environ.get("DATA_DIRECTORY", "data")).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def database_url() -> str:
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    return f"sqlite:///{(data_directory() / 'news.db').as_posix()}"


def engine():
    global _engine
    if _engine is None:
        url = database_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine so the next call re-reads the environment (tests)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def init_db() -> str:
    """Fresh database: create tables and stamp the latest version. Existing: apply pending migrations. Returns the URL."""
    url = database_url()
    fresh = not inspect(engine()).get_table_names()
    SQLModel.metadata.create_all(engine())
    if fresh:
        migrations.stamp_latest(engine())
    else:
        _add_missing_columns()
        migrations.apply_pending(engine())
    if url.startswith("sqlite"):
        with engine().connect() as connection:
            connection.execute(text("PRAGMA journal_mode=WAL"))
            connection.execute(text("PRAGMA foreign_keys=ON"))
    return url


def _add_missing_columns() -> list[str]:
    """Safety net for existing databases: add columns the models have and the database lacks.

    This does not replace a migration. Every schema change still gets a numbered entry in
    services/migrations.py so drops, renames, and data transforms reach production too.
    """
    added: list[str] = []
    inspector = inspect(engine())
    with engine().begin() as connection:
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = column.type.compile(engine().dialect)
                default = ""
                if column.default is not None and getattr(column.default, "arg", None) is not None and not callable(column.default.arg):
                    value = column.default.arg
                    default = f" DEFAULT {int(value) if isinstance(value, bool) else repr(value)}"
                connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}{default}'))
                added.append(f"{table.name}.{column.name}")
    return added


@contextmanager
def session() -> Iterator[Session]:
    with Session(engine()) as db_session:
        yield db_session
