"""Tenant voices over the single TTS voice library (Chatterbox or the OpenAI-compatible server, see services/tts.py).

The TTS server has one flat voice library. Each tenant voice is stored there under
`<tenant slug>-<name>`; the tenant only ever sees `<name>`. The sample WAV is kept
under `assets/voices/<tenant>/` so a voice can be re-uploaded if the library is
rebuilt.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, BinaryIO, Optional, Union

from sqlmodel import select

from services import audit, tts
from services.db import PROJECT_ROOT, session
from services.errors import conflict, invalid, not_found
from services.models import Tenant, Voice

NAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


def sample_root(tenant: Tenant) -> Path:
    root = Path(os.environ.get("VOICE_STORAGE_PATH", "assets/voices")).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root / tenant.slug


def library_name(tenant: Tenant, name: str) -> str:
    return f"{tenant.slug}-{name}"


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not NAME_PATTERN.match(name):
        raise invalid("Voice name must match [a-zA-Z0-9._-]+.")
    return name


def _serialize(voice: Voice) -> dict[str, Any]:
    return {
        "name": voice.name,
        "language": voice.language,
        "library_name": voice.library_name,
        "sample_url": f"/api/v1/voices/{voice.name}/sample" if voice.sample_path else None,
        "created_at": voice.created_at.isoformat(),
    }


def list_voices(tenant: Tenant, limit: Optional[int] = None, cursor: Optional[str] = None) -> dict[str, Any]:
    size = min(max(int(limit or 50), 1), 200)
    offset = int(cursor) if cursor else 0
    with session() as db:
        rows = db.exec(select(Voice).where(Voice.tenant_id == tenant.id).order_by(Voice.name).offset(offset).limit(size + 1)).all()
    return {"items": [_serialize(v) for v in rows[:size]], "next_cursor": str(offset + size) if len(rows) > size else None}


def _load(db, tenant: Tenant, name: str) -> Voice:
    voice = db.exec(select(Voice).where(Voice.tenant_id == tenant.id, Voice.name == name)).first()
    if voice is None:
        raise not_found(f"Voice {name!r}")
    return voice


def get_voice(tenant: Tenant, name: str) -> dict[str, Any]:
    with session() as db:
        return _serialize(_load(db, tenant, name))


def sample_path(tenant: Tenant, name: str) -> Path:
    with session() as db:
        voice = _load(db, tenant, name)
        if not voice.sample_path or not Path(voice.sample_path).is_file():
            raise not_found(f"Sample for voice {name!r}")
        return Path(voice.sample_path)


def upload_voice(
    tenant: Tenant,
    name: str,
    sample: Union[Path, BinaryIO],
    language: str = "en",
    replace: bool = False,
    actor: str = "system",
) -> dict[str, Any]:
    """Store the sample, upload it to the TTS library as `<tenant>-<name>`, and record the voice."""
    name = _check_name(name)
    language = (language or "en").strip() or "en"
    tts.base_url()  # fail before touching the database or disk when no TTS server is configured

    with session() as db:
        existing = db.exec(select(Voice).where(Voice.tenant_id == tenant.id, Voice.name == name)).first()
        if existing is not None and not replace:
            raise conflict(f"Voice {name!r} already exists. Set replace=true to overwrite it.")
        before = _serialize(existing) if existing else None

    target_dir = sample_root(tenant)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{name}.wav"
    if isinstance(sample, Path):
        if not sample.is_file():
            raise invalid(f"Sample file does not exist: {sample}")
        shutil.copyfile(sample, target)
    else:
        with target.open("wb") as handle:
            shutil.copyfileobj(sample, handle)
    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise invalid("Sample file is empty.")

    lib = library_name(tenant, name)
    timeout = min(tts.default_timeout(), 60)
    if lib in tts.voice_names(timeout):
        tts.delete_voice(lib, timeout)
    tts.upload_voice(lib, target, timeout)

    with session() as db:
        voice = db.exec(select(Voice).where(Voice.tenant_id == tenant.id, Voice.name == name)).first()
        if voice is None:
            voice = Voice(tenant_id=tenant.id, name=name, library_name=lib)
        voice.language = language
        voice.sample_path = str(target)
        voice.library_name = lib
        db.add(voice)
        db.commit()
        db.refresh(voice)
        result = _serialize(voice)
    audit.record(tenant_id=tenant.id, actor=actor, action="upload_voice", object_type="voice", object_id=name, before=before, after=result)
    return result


def delete_voice(tenant: Tenant, name: str, actor: str = "system") -> dict[str, Any]:
    tts.base_url()  # fail before touching the database when no TTS server is configured
    with session() as db:
        voice = _load(db, tenant, name)
        before = _serialize(voice)
        lib = voice.library_name
        path = Path(voice.sample_path) if voice.sample_path else None
        db.delete(voice)
        db.commit()
    timeout = min(tts.default_timeout(), 60)
    if lib in tts.voice_names(timeout):
        tts.delete_voice(lib, timeout)
    if path is not None:
        path.unlink(missing_ok=True)
    audit.record(tenant_id=tenant.id, actor=actor, action="delete_voice", object_type="voice", object_id=name, before=before)
    return {"deleted": name}


def resolve_library_name(tenant: Tenant, name: str) -> str:
    """The TTS library name for a tenant voice (used by the runs step)."""
    with session() as db:
        return _load(db, tenant, name).library_name
