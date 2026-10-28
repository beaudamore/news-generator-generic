"""Tenant administration: admin-only operations over tenants and their API keys."""

from __future__ import annotations

import re
from typing import Any, Optional

from sqlmodel import select

from services import audit, tenancy
from services.db import session
from services.errors import ApiError, conflict, invalid, not_found
from services.models import ApiKey, Tenant, utcnow

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")


def require_admin(tenant: Tenant) -> None:
    if not tenant.is_admin:
        raise ApiError("forbidden", "This operation is limited to the admin tenant.")


def _serialize(tenant: Tenant) -> dict[str, Any]:
    return {"slug": tenant.slug, "name": tenant.name, "active": tenant.active, "is_admin": tenant.is_admin, "created_at": tenant.created_at.isoformat()}


def _serialize_key(key: ApiKey) -> dict[str, Any]:
    return {
        "id": str(key.id),
        "prefix": key.key_prefix,
        "label": key.label,
        "created_at": key.created_at.isoformat(),
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
    }


def _load(db, slug: str) -> Tenant:
    tenant = db.exec(select(Tenant).where(Tenant.slug == slug)).first()
    if tenant is None:
        raise not_found(f"Tenant {slug!r}")
    return tenant


def list_tenants(actor_tenant: Tenant) -> list[dict[str, Any]]:
    require_admin(actor_tenant)
    with session() as db:
        return [_serialize(t) for t in db.exec(select(Tenant).order_by(Tenant.slug)).all()]


def create_tenant(actor_tenant: Tenant, slug: str, name: str, actor: Optional[str] = None) -> dict[str, Any]:
    """Create a tenant, seed its prompts and storage, and return its first API key (shown once)."""
    require_admin(actor_tenant)
    slug = (slug or "").strip().lower()
    name = (name or "").strip()
    if not SLUG_PATTERN.match(slug):
        raise invalid("slug must be 2-63 characters of lowercase letters, digits, dot, dash, or underscore, starting with a letter or digit.")
    if not name:
        raise invalid("name is required.")
    if tenancy.get_tenant_by_slug(slug) is not None:
        raise conflict(f"Tenant {slug!r} already exists.")
    tenant = tenancy.create_tenant(slug, name)
    raw_key = tenancy.create_api_key(tenant.id, label="initial")
    for path in tenancy.tenant_paths(tenant).values():
        path.mkdir(parents=True, exist_ok=True)
    result = {"tenant": _serialize(tenant), "api_key": raw_key}
    audit.record(tenant_id=actor_tenant.id, actor=actor or tenancy.current_actor(), action="create_tenant", object_type="tenant", object_id=slug, after=_serialize(tenant))
    return result


def list_api_keys(actor_tenant: Tenant, slug: str) -> list[dict[str, Any]]:
    require_admin(actor_tenant)
    with session() as db:
        tenant = _load(db, slug)
        rows = db.exec(select(ApiKey).where(ApiKey.tenant_id == tenant.id).order_by(ApiKey.created_at)).all()
        return [_serialize_key(k) for k in rows]


def create_api_key(actor_tenant: Tenant, slug: str, label: str = "", actor: Optional[str] = None) -> dict[str, Any]:
    require_admin(actor_tenant)
    with session() as db:
        tenant = _load(db, slug)
        tenant_id = tenant.id
    raw_key = tenancy.create_api_key(tenant_id, label=(label or "").strip())
    with session() as db:
        key = db.exec(select(ApiKey).where(ApiKey.key_hash == tenancy.hash_api_key(raw_key))).first()
        meta = _serialize_key(key)
    audit.record(tenant_id=actor_tenant.id, actor=actor or tenancy.current_actor(), action="create_api_key", object_type="api_key", object_id=meta["id"], after={"tenant": slug, "label": meta["label"], "prefix": meta["prefix"]})
    return {"api_key": raw_key, **meta}


def revoke_api_key(actor_tenant: Tenant, slug: str, key_id: str, actor: Optional[str] = None) -> dict[str, Any]:
    require_admin(actor_tenant)
    with session() as db:
        tenant = _load(db, slug)
        try:
            key = db.get(ApiKey, int(key_id))
        except (TypeError, ValueError):
            key = None
        if key is None or key.tenant_id != tenant.id:
            raise not_found(f"API key {key_id!r} for tenant {slug!r}")
        if key.revoked_at is not None:
            raise conflict(f"API key {key_id!r} is already revoked.")
        key.revoked_at = utcnow()
        db.add(key)
        db.commit()
        meta = _serialize_key(key)
    audit.record(tenant_id=actor_tenant.id, actor=actor or tenancy.current_actor(), action="revoke_api_key", object_type="api_key", object_id=key_id, after={"tenant": slug, "prefix": meta["prefix"]})
    return {"revoked": key_id}


def set_tenant_active(actor_tenant: Tenant, slug: str, active: bool, actor: Optional[str] = None) -> dict[str, Any]:
    require_admin(actor_tenant)
    with session() as db:
        tenant = _load(db, slug)
        if tenant.is_admin and not active:
            raise invalid("The admin tenant cannot be deactivated.")
        before = _serialize(tenant)
        tenant.active = bool(active)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        result = _serialize(tenant)
    audit.record(tenant_id=actor_tenant.id, actor=actor or tenancy.current_actor(), action="set_tenant_active", object_type="tenant", object_id=slug, before=before, after=result)
    return result
