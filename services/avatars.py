"""Avatar storage and image serving service."""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Optional

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import generate_video_comfyui  # noqa: E402

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720


@dataclass
class AvatarServiceError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


_ROOT_OVERRIDE: ContextVar[Optional[Path]] = ContextVar("avatar_root_override", default=None)


@contextmanager
def storage_root(path: Path):
    """Scope every avatar path in this context to `path` (per-tenant storage)."""
    token = _ROOT_OVERRIDE.set(Path(path))
    try:
        yield
    finally:
        _ROOT_OVERRIDE.reset(token)


def avatar_storage_root() -> Path:
    override = _ROOT_OVERRIDE.get()
    if override is not None:
        root = override
    else:
        root = Path(os.environ.get("AVATAR_STORAGE_PATH", "assets/avatars")).expanduser()
        if not root.is_absolute():
            root = PROJECT_ROOT / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def avatar_public_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-._")
    if not cleaned:
        raise AvatarServiceError("avatar_name is required.")
    return cleaned


def avatar_image_path(avatar_name: str) -> Path:
    return avatar_storage_root() / f"{avatar_public_name(avatar_name)}.png"


def avatar_metadata_path(avatar_name: str) -> Path:
    return avatar_storage_root() / f"{avatar_public_name(avatar_name)}.json"


def max_dimension() -> int:
    return int(os.environ.get("AVATAR_MAX_DIMENSION", "1920"))


def default_width() -> int:
    return configured_dimension("AVATAR_DEFAULT_WIDTH", DEFAULT_WIDTH)


def default_height() -> int:
    return configured_dimension("AVATAR_DEFAULT_HEIGHT", DEFAULT_HEIGHT)


def avatar_resize_url(name: str) -> str:
    return f"/avatars/{name}?width={default_width()}&height={default_height()}"


def validate_dimensions(width: Optional[int], height: Optional[int]) -> tuple[Optional[int], Optional[int]]:
    limit = max_dimension()
    if width is not None and width <= 0:
        raise AvatarServiceError("width must be greater than zero.")
    if height is not None and height <= 0:
        raise AvatarServiceError("height must be greater than zero.")
    if width is not None and width > limit:
        raise AvatarServiceError(f"width exceeds maximum of {limit}px.")
    if height is not None and height > limit:
        raise AvatarServiceError(f"height exceeds maximum of {limit}px.")
    # Avatars are always 16:9 so the video stage renders widescreen without
    # cropping. A single dimension derives the other; both must agree.
    if width is None and height is None:
        width, height = default_width(), default_height()
    elif width is not None and height is None:
        height = round(width * 9 / 16)
    elif height is not None and width is None:
        width = round(height * 16 / 9)
    if width * 9 != height * 16:
        raise AvatarServiceError(f"Avatar dimensions must be 16:9, got {width}x{height}. Use 1280x720 or 1920x1080.")
    return width, height


def normalize_image(image: Image.Image) -> Image.Image:
    if image.mode not in ("RGB", "RGBA"):
        return image.convert("RGBA")
    return image


def write_metadata(avatar_name: str, metadata: dict) -> None:
    metadata_path = avatar_metadata_path(avatar_name)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def avatar_metadata(name: str, source: str, image: Image.Image, prompt: Optional[str] = None, workflow: Optional[str] = None) -> dict:
    metadata = {
        "name": name,
        "source": source,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "width": image.width,
        "height": image.height,
        "url": f"/avatars/{name}",
        "resize_url": avatar_resize_url(name),
    }
    if prompt:
        metadata["prompt"] = prompt
    if workflow:
        metadata["workflow"] = workflow
    return metadata


def read_metadata(avatar_name: str) -> dict:
    metadata_path = avatar_metadata_path(avatar_name)
    if not metadata_path.is_file():
        return {}
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def list_avatars() -> dict:
    avatars = []
    for path in sorted(avatar_storage_root().glob("*.png")):
        name = path.stem
        avatars.append(
            {
                "name": name,
                "filename": path.name,
                "size_bytes": path.stat().st_size,
                "url": f"/avatars/{name}",
                "resize_url": avatar_resize_url(name),
                "metadata": read_metadata(name),
            }
        )
    return {"count": len(avatars), "avatars": avatars}


def save_avatar_image(avatar_name: str, image_file: BinaryIO, source: str = "upload") -> dict:
    name = avatar_public_name(avatar_name)
    try:
        image = Image.open(image_file)
        image.verify()
    except Exception as exc:
        raise AvatarServiceError(f"Invalid image file: {exc}") from exc

    image_file.seek(0)
    image = normalize_image(Image.open(image_file))
    output_path = avatar_image_path(name)
    image.save(output_path, format="PNG", optimize=True)
    metadata = avatar_metadata(name, source, image)
    write_metadata(name, metadata)
    return {
        "status": "success",
        "avatar": {
            "name": name,
            "filename": output_path.name,
            "size_bytes": output_path.stat().st_size,
            "url": f"/avatars/{name}",
            "resize_url": avatar_resize_url(name),
            "metadata": metadata,
        },
    }


def avatar_file_path(avatar_name: str) -> Path:
    path = avatar_image_path(avatar_name)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def resized_avatar_bytes(avatar_name: str, width: Optional[int], height: Optional[int]) -> bytes:
    width, height = validate_dimensions(width, height)
    path = avatar_file_path(avatar_name)
    image = Image.open(path)

    if width is None and height is None:
        width = default_width()
        height = default_height()
    elif width is not None and height is None:
        height = int(width * (image.height / image.width))
    elif height is not None and width is None:
        width = int(height * (image.width / image.height))

    resized = image.resize((width or default_width(), height or default_height()), Image.Resampling.LANCZOS)
    output = BytesIO()
    resized.save(output, format="PNG", optimize=True)
    return output.getvalue()


WORKFLOW_DIR = PROJECT_ROOT / "comfyui-workflows" / "avatar"
SEED_MAX = 2**64 - 1  # ComfyUI samplers take an unsigned 64-bit seed; the avatars table stores it as text


def default_negative_prompt() -> str:
    return os.environ.get("AVATAR_NEGATIVE_PROMPT", "").strip()


def workflow_seed_max(workflow: dict) -> int:
    """Largest seed the template's sampler accepts. ComfyUI samplers take a 64-bit seed."""
    return SEED_MAX


def list_workflows() -> list[dict]:
    """Every avatar template in comfyui-workflows/avatar, with its seed range and whether it is the configured default."""
    default = os.environ.get("AVATAR_COMFYUI_WORKFLOW", "").strip()
    default_name = Path(default).name if default else ""
    items = []
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        try:
            workflow = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items.append({"name": path.name, "seed_max": str(workflow_seed_max(workflow)), "default": path.name == default_name})
    return items


def resolve_workflow_path(workflow: Optional[str] = None) -> Path:
    """The template to run: a file name from comfyui-workflows/avatar, or AVATAR_COMFYUI_WORKFLOW when none is given."""
    if workflow:
        name = workflow.strip()
        if Path(name).name != name or not name.endswith(".json"):
            raise AvatarServiceError(f"workflow must be a file name from {WORKFLOW_DIR.name}/: {workflow!r}")
        path = WORKFLOW_DIR / name
        if not path.is_file():
            raise AvatarServiceError(f"Avatar workflow does not exist: {path}")
        return path
    workflow_value = os.environ.get("AVATAR_COMFYUI_WORKFLOW", "").strip()
    if not workflow_value:
        raise AvatarServiceError("Set AVATAR_COMFYUI_WORKFLOW.")
    path = Path(workflow_value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        raise AvatarServiceError(f"Avatar workflow does not exist: {path}")
    return path


def configured_dimension(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise AvatarServiceError(f"{name} must be greater than zero.")
    if value > max_dimension():
        raise AvatarServiceError(f"{name} exceeds AVATAR_MAX_DIMENSION.")
    return value


def patch_generation_values(value: Any, width: int, height: int, seed: int) -> Any:
    if isinstance(value, dict):
        patched = {}
        for key, item in value.items():
            if key == "width" and isinstance(item, int):
                patched[key] = width
            elif key == "height" and isinstance(item, int):
                patched[key] = height
            elif key == "seed" and isinstance(item, int):
                patched[key] = seed
            else:
                patched[key] = patch_generation_values(item, width, height, seed)
        return patched
    if isinstance(value, list):
        return [patch_generation_values(item, width, height, seed) for item in value]
    return value


def hero_default_workflow() -> str:
    """Template file name (in comfyui-workflows/avatar) used for segment hero images unless the profile picks one."""
    value = os.environ.get("HERO_COMFYUI_WORKFLOW", "").strip() or os.environ.get("AVATAR_COMFYUI_WORKFLOW", "").strip()
    return Path(value).name if value else ""


def render_image(
    prompt: str,
    output_path: Path,
    *,
    width: int,
    height: int,
    name: str = "image",
    negative_prompt: Optional[str] = None,
    seed: Optional[int] = None,
    workflow: Optional[str] = None,
) -> dict:
    """Run one image template on ComfyUI with the placeholders filled and save the result as PNG at output_path.

    Shared by avatar generation and segment hero images. Returns prompt_id, seed, workflow path, and size.
    """
    prompt = prompt.strip()
    if not prompt:
        raise AvatarServiceError("prompt is required.")
    comfyui_url = os.environ.get("AVATAR_COMFYUI_BASE_URL", "").strip()
    if not comfyui_url:
        raise AvatarServiceError("Set AVATAR_COMFYUI_BASE_URL.")
    timeout = int(os.environ.get("AVATAR_COMFYUI_TIMEOUT_SECONDS", "1800"))
    workflow_path = resolve_workflow_path(workflow)
    graph = json.loads(workflow_path.read_text(encoding="utf-8"))
    negative_prompt = (negative_prompt if negative_prompt is not None else os.environ.get("AVATAR_NEGATIVE_PROMPT", "")).strip()
    seed = seed if seed is not None else int(datetime.now(timezone.utc).timestamp())

    graph = generate_video_comfyui.replace_placeholders(
        graph,
        {
            "{{POSITIVE_PROMPT}}": prompt,
            "{{AVATAR_NAME}}": name,
            "{{NEGATIVE_PROMPT}}": negative_prompt,
            "{{WIDTH}}": width,
            "{{HEIGHT}}": height,
            "{{SEED}}": seed,
        },
    )
    graph = patch_generation_values(graph, width, height, seed)
    prompt_id = generate_video_comfyui.queue_prompt(comfyui_url, graph, min(timeout, 120))
    history = generate_video_comfyui.wait_for_history(comfyui_url, prompt_id, timeout)
    media = generate_video_comfyui.find_media_output(history)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    generate_video_comfyui.download_media(comfyui_url, media, output_path, min(timeout, 300))
    with output_path.open("rb") as image_file:
        image = normalize_image(Image.open(image_file))
        image.save(output_path, format="PNG", optimize=True)
    return {"prompt_id": prompt_id, "seed": seed, "workflow": str(workflow_path), "width": image.width, "height": image.height, "negative_prompt": negative_prompt}


def generate_avatar(
    avatar_name: str,
    avatar_prompt: str,
    replace_avatar: bool = False,
    width: Optional[int] = None,
    height: Optional[int] = None,
    negative_prompt: Optional[str] = None,
    seed: Optional[int] = None,
    workflow: Optional[str] = None,
) -> dict:
    name = avatar_public_name(avatar_name)
    prompt = avatar_prompt.strip()
    if not prompt:
        raise AvatarServiceError("avatar_prompt is required.")

    output_path = avatar_image_path(name)
    if output_path.exists() and not replace_avatar:
        raise AvatarServiceError(f"Avatar {name!r} already exists. Set replace_avatar=true to overwrite it.")

    width, height = validate_dimensions(width, height)
    width = width if width is not None else default_width()
    height = height if height is not None else default_height()
    rendered = render_image(prompt, output_path, width=width, height=height, name=name, negative_prompt=negative_prompt, seed=seed, workflow=workflow)
    with output_path.open("rb") as image_file:
        image = Image.open(image_file)
        image.load()

    metadata = avatar_metadata(name, "generated", image, prompt=prompt, workflow=rendered["workflow"])
    metadata["prompt_id"] = rendered["prompt_id"]
    metadata["seed"] = rendered["seed"]
    if rendered["negative_prompt"]:
        metadata["negative_prompt"] = rendered["negative_prompt"]
    write_metadata(name, metadata)
    return {
        "status": "success",
        "avatar": {
            "name": name,
            "filename": output_path.name,
            "size_bytes": output_path.stat().st_size,
            "url": f"/avatars/{name}",
            "resize_url": avatar_resize_url(name),
            "metadata": metadata,
        },
    }