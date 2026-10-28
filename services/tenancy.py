"""Tenants, API keys, and per-tenant storage paths.

`.env` is system control. It defines the admin tenant (`ADMIN_TENANT_SLUG`,
`ADMIN_TENANT_NAME`) and its bootstrap API key (`ADMIN_API_KEY`, falling back to
`AVATAR_API_KEY` so an existing installation keeps working). Every other tenant
lives only in the database. Keys are stored as SHA-256 hashes; the raw key is
shown once at creation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from sqlmodel import select

from services.db import PROJECT_ROOT, session
from services.models import ApiKey, Tenant, utcnow

LOGGER = logging.getLogger("news_pipeline")
_ACTOR: ContextVar[str] = ContextVar("current_actor", default="system")


def current_actor() -> str:
    """Who is acting in this request or session, for the audit log (key:<prefix> once resolved)."""
    return _ACTOR.get()


KEY_PREFIX_LENGTH = 10
BOOTSTRAP_LABEL = "bootstrap"


class AuthError(Exception):
    """Raised when an API key is missing, unknown, revoked, or its tenant is inactive."""


def generate_api_key() -> str:
    return "ng_" + secrets.token_urlsafe(32)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def key_prefix(raw_key: str) -> str:
    return raw_key[:KEY_PREFIX_LENGTH]


def resolve_tenant(raw_key: Optional[str]) -> Tenant:
    """Map an API key to its tenant. Returns a detached Tenant instance."""
    raw_key = (raw_key or "").strip()
    if not raw_key:
        raise AuthError("API key missing. Send Authorization: Bearer <key> or X-API-Key.")
    digest = hash_api_key(raw_key)
    with session() as db:
        api_key = db.exec(select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.revoked_at.is_(None))).first()
        if api_key is None:
            raise AuthError("API key not recognized or revoked.")
        tenant = db.get(Tenant, api_key.tenant_id)
        if tenant is None or not tenant.active:
            raise AuthError("Tenant is inactive.")
        db.expunge(tenant)
        _ACTOR.set(f"key:{api_key.key_prefix}")
        from services import logging_setup

        logging_setup.tenant_id_var.set(tenant.id)
        logging_setup.tenant_slug_var.set(tenant.slug)
        return tenant


def create_api_key(tenant_id: int, label: str = "", raw_key: Optional[str] = None) -> str:
    """Store a key for a tenant and return the raw key (the only time it is available)."""
    raw = raw_key or generate_api_key()
    with session() as db:
        db.add(ApiKey(tenant_id=tenant_id, key_hash=hash_api_key(raw), key_prefix=key_prefix(raw), label=label))
        db.commit()
    return raw


def revoke_api_key(key_id: int) -> bool:
    with session() as db:
        api_key = db.get(ApiKey, key_id)
        if api_key is None or api_key.revoked_at is not None:
            return False
        api_key.revoked_at = utcnow()
        db.add(api_key)
        db.commit()
        return True


def get_tenant_by_slug(slug: str) -> Optional[Tenant]:
    with session() as db:
        tenant = db.exec(select(Tenant).where(Tenant.slug == slug)).first()
        if tenant is not None:
            db.expunge(tenant)
        return tenant


def create_tenant(slug: str, name: str, is_admin: bool = False) -> Tenant:
    with session() as db:
        tenant = Tenant(slug=slug, name=name, is_admin=is_admin)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        db.expunge(tenant)
        return tenant


def bootstrap_admin_tenant() -> dict:
    """Ensure the admin tenant from `.env` exists and its bootstrap key matches `.env`.

    Rotating ADMIN_API_KEY in `.env` revokes the previous bootstrap key on the next
    start. Keys created through the API (other labels) are never touched here.
    """
    slug = os.environ.get("ADMIN_TENANT_SLUG", "admin").strip() or "admin"
    name = os.environ.get("ADMIN_TENANT_NAME", "Admin").strip() or "Admin"
    raw_key = os.environ.get("ADMIN_API_KEY", "").strip() or os.environ.get("AVATAR_API_KEY", "").strip()

    tenant = get_tenant_by_slug(slug)
    created = False
    if tenant is None:
        tenant = create_tenant(slug, name, is_admin=True)
        created = True

    key_state = "none"
    if raw_key:
        digest = hash_api_key(raw_key)
        with session() as db:
            bootstrap_keys = db.exec(
                select(ApiKey).where(ApiKey.tenant_id == tenant.id, ApiKey.label == BOOTSTRAP_LABEL, ApiKey.revoked_at.is_(None))
            ).all()
            current = [k for k in bootstrap_keys if k.key_hash == digest]
            for stale in (k for k in bootstrap_keys if k.key_hash != digest):
                stale.revoked_at = utcnow()
                db.add(stale)
            if current:
                key_state = "unchanged"
            else:
                db.add(ApiKey(tenant_id=tenant.id, key_hash=digest, key_prefix=key_prefix(raw_key), label=BOOTSTRAP_LABEL))
                key_state = "created" if not bootstrap_keys else "rotated"
            db.commit()
    else:
        LOGGER.warning("No ADMIN_API_KEY or AVATAR_API_KEY in the environment; the admin tenant has no API key and /api/v1 routes will reject every request.")

    LOGGER.info("[tenancy] admin tenant %r %s; bootstrap key %s", slug, "created" if created else "present", key_state)
    return {"slug": slug, "created": created, "key_state": key_state}


def output_root() -> Path:
    root = Path(os.environ.get("NEWS_OUTPUT_DIRECTORY", "output")).expanduser()
    return root if root.is_absolute() else PROJECT_ROOT / root


def avatar_root() -> Path:
    root = Path(os.environ.get("AVATAR_STORAGE_PATH", "assets/avatars")).expanduser()
    return root if root.is_absolute() else PROJECT_ROOT / root


def tenant_paths(tenant: Tenant) -> dict[str, Path]:
    """Per-tenant storage locations. Not yet used by the pipeline; the runs step adopts them."""
    return {
        "output": output_root() / tenant.slug,
        "avatars": avatar_root() / tenant.slug,
    }
