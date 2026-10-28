"""Tenant avatars over the existing avatar module, stored under `assets/avatars/<tenant>/`.

The folder is the source of truth: an avatar is a `<name>.png` plus its `<name>.json` metadata,
both written by services.avatars. Nothing about avatars is kept in the database.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional

from services import audit, avatars, tenancy
from services.errors import conflict, invalid, not_found
from services.models import Tenant


def avatar_root(tenant: Tenant) -> Path:
    return tenancy.tenant_paths(tenant)["avatars"]


@contextmanager
def _scoped(tenant: Tenant) -> Iterator[None]:
    with avatars.storage_root(avatar_root(tenant)):
        yield


def _clean_name(name: str) -> str:
    try:
        return avatars.avatar_public_name(name)
    except avatars.AvatarServiceError as exc:
        raise invalid(exc.message) from exc


def _image(tenant: Tenant, name: str) -> Path:
    return avatar_root(tenant) / f"{name}.png"


def _read(tenant: Tenant, name: str) -> dict[str, Any]:
    """Metadata for one avatar from its .json; a PNG without a .json still lists with what the file says."""
    image = _image(tenant, name)
    if not image.is_file():
        raise not_found(f"Avatar {name!r}")
    metadata_path = image.with_suffix(".json")
    metadata: dict[str, Any] = {}
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}
    if "width" not in metadata or "height" not in metadata:
        with _scoped(tenant):
            size = avatars.Image.open(image).size
        metadata.setdefault("width", size[0])
        metadata.setdefault("height", size[1])
    metadata["name"] = name
    return metadata


def _serialize(name: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "image_url": f"/api/v1/avatars/{name}/image",
        "prompt": metadata.get("prompt"),
        "negative_prompt": metadata.get("negative_prompt"),
        "seed": None if metadata.get("seed") is None else str(metadata["seed"]),
        "width": metadata["width"],
        "height": metadata["height"],
        "workflow": metadata.get("workflow"),
        "source": metadata.get("source", "upload"),
        "created_at": metadata.get("created_at"),
    }


def _summary(name: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "image_url": f"/api/v1/avatars/{name}/image", "width": metadata["width"], "height": metadata["height"], "source": metadata.get("source", "upload"), "created_at": metadata.get("created_at")}


def list_avatars(tenant: Tenant, limit: Optional[int] = None, cursor: Optional[str] = None) -> dict[str, Any]:
    size = min(max(int(limit or 50), 1), 200)
    offset = int(cursor) if cursor else 0
    root = avatar_root(tenant)
    names = sorted(p.stem for p in root.glob("*.png")) if root.is_dir() else []
    page = names[offset : offset + size + 1]
    return {"items": [_summary(n, _read(tenant, n)) for n in page[:size]], "next_cursor": str(offset + size) if len(page) > size else None}


def get_avatar(tenant: Tenant, name: str) -> dict[str, Any]:
    name = _clean_name(name)
    return _serialize(name, _read(tenant, name))


def image_path(tenant: Tenant, name: str) -> Path:
    name = _clean_name(name)
    path = _image(tenant, name)
    if not path.is_file():
        raise not_found(f"Image for avatar {name!r}")
    return path


def _existing(tenant: Tenant, name: str, replace: bool) -> Optional[dict[str, Any]]:
    if not _image(tenant, name).is_file():
        return None
    if not replace:
        raise conflict(f"Avatar {name!r} already exists. Set replace=true to overwrite it.")
    return _serialize(name, _read(tenant, name))


def avatar_options() -> dict[str, Any]:
    """What the generate form needs: the templates on disk, the default one, the default negative prompt, and the default size."""
    workflows = avatars.list_workflows()
    default = next((w["name"] for w in workflows if w["default"]), workflows[0]["name"] if workflows else None)
    return {"workflows": workflows, "default_workflow": default, "hero_workflow": avatars.hero_default_workflow() or default, "negative_prompt": avatars.default_negative_prompt(), "width": avatars.default_width(), "height": avatars.default_height()}


def generate_avatar(
    tenant: Tenant,
    name: str,
    prompt: str,
    negative_prompt: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    seed: Optional[int] = None,
    replace: bool = False,
    actor: Optional[str] = None,
    workflow: Optional[str] = None,
) -> dict[str, Any]:
    name = _clean_name(name)
    if not prompt or not prompt.strip():
        raise invalid("prompt is required.")
    before = _existing(tenant, name, replace)
    try:
        with _scoped(tenant):
            avatars.generate_avatar(name, prompt, replace, width, height, negative_prompt=negative_prompt, seed=seed, workflow=workflow)
    except avatars.AvatarServiceError as exc:
        raise invalid(exc.message) from exc
    after = _serialize(name, _read(tenant, name))
    audit.record(tenant_id=tenant.id, actor=actor or tenancy.current_actor(), action="generate_avatar", object_type="avatar", object_id=name, before=before, after=after)
    return after


def upload_avatar(tenant: Tenant, name: str, image: BinaryIO, replace: bool = False, actor: Optional[str] = None) -> dict[str, Any]:
    name = _clean_name(name)
    before = _existing(tenant, name, replace)
    try:
        with _scoped(tenant):
            avatars.save_avatar_image(name, image, "upload")
    except avatars.AvatarServiceError as exc:
        raise invalid(exc.message) from exc
    after = _serialize(name, _read(tenant, name))
    audit.record(tenant_id=tenant.id, actor=actor or tenancy.current_actor(), action="upload_avatar", object_type="avatar", object_id=name, before=before, after=after)
    return after


def delete_avatar(tenant: Tenant, name: str, actor: Optional[str] = None) -> dict[str, Any]:
    name = _clean_name(name)
    before = _serialize(name, _read(tenant, name))
    image = _image(tenant, name)
    image.unlink(missing_ok=True)
    image.with_suffix(".json").unlink(missing_ok=True)
    audit.record(tenant_id=tenant.id, actor=actor or tenancy.current_actor(), action="delete_avatar", object_type="avatar", object_id=name, before=before)
    return {"deleted": name}


def resolve_image_path(tenant: Tenant, name: str) -> Path:
    """Path to a tenant avatar image (used by the runs step)."""
    return image_path(tenant, name)
