"""News generator MCP server (stdio).

Each tool maps to one function in `services/`. See docs/ui-dev1.md, section
"MCP tool catalog (the contract)". Tools return structured JSON, never binary
artifacts. Errors are raised with the catalog error shape serialized in the message.
"""

# No `from __future__ import annotations` here: FastMCP inspects tool signatures at import time
# and needs real annotation objects, not strings, on every supported Python version.
import functools
import json
import os
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Optional

import anyio

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from services import avatars, comfyui_models, db, logging_setup, pipeline, preflight, profiles, prompts, runs, schedules, search_preview, system, tenancy, tenant_avatars, tenant_voices, tenants  # noqa: E402
from services.errors import ApiError  # noqa: E402
from services.models import Tenant  # noqa: E402

mcp = FastMCP("news-generator")

# Set per HTTP request by the auth middleware; empty for stdio sessions.
REQUEST_API_KEY: ContextVar[str] = ContextVar("mcp_request_api_key", default="")


def _tenant() -> Tenant:
    """Tenant for this call: the HTTP request's bearer key, else NEWS_API_KEY for a stdio session (falls back to the admin bootstrap key)."""
    raw_key = REQUEST_API_KEY.get() or os.environ.get("NEWS_API_KEY", "").strip() or os.environ.get("ADMIN_API_KEY", "").strip() or os.environ.get("AVATAR_API_KEY", "").strip()
    try:
        return tenancy.resolve_tenant(raw_key)
    except tenancy.AuthError as exc:
        raise ApiError("unauthorized", str(exc)) from exc


def catalog_tool(name: str) -> Callable:
    """Register a tool that loads .env, resolves the tenant, and reports ApiError in the catalog shape."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            pipeline.load_env()
            try:
                return fn(*args, **kwargs)
            except ApiError as exc:
                raise RuntimeError(json.dumps(exc.to_dict())) from exc

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Tool bodies are synchronous and may take minutes (avatar generation, prompt tests).
            # Run them in a worker thread so the event loop that also serves FastAPI stays free.
            return await anyio.to_thread.run_sync(functools.partial(call, *args, **kwargs))

        return mcp.tool(name=name)(wrapper)

    return decorate


# ----------------------------------------------------------------------------- system


@catalog_tool("preflight")
def preflight_tool(voice: Optional[str] = None, avatar_name: Optional[str] = None) -> dict[str, Any]:
    """Verify every service the news pipeline depends on before starting a run.

    Checks SearXNG, the LLM model, the reranker (soft), the TTS server and the given
    voice, ComfyUI with template validation and the given avatar, and local
    prerequisites. Returns {"ok", "checks": [{"service", "status", "detail",
    "latency_ms"}], "checked_at", "duration_ms", "cached"}. Cached 30 seconds.
    """
    anchor_path: Optional[Path] = None
    if avatar_name:
        try:
            anchor_path = tenant_avatars.image_path(_tenant(), avatar_name)
        except ApiError:
            try:
                anchor_path = avatars.avatar_file_path(avatar_name)
            except (FileNotFoundError, avatars.AvatarServiceError):
                anchor_path = avatars.avatar_storage_root() / f"{avatar_name}.png"
    return preflight.run_preflight(voice=voice, anchor_path=anchor_path)


@catalog_tool("get_system_info")
def get_system_info_tool() -> dict[str, Any]:
    """Version, video output settings, configured services, and storage for the calling tenant. URLs only for admin."""
    return system.get_system_info(_tenant())


@catalog_tool("audit_comfyui_models")
def audit_comfyui_models_tool(host: Optional[str] = None, workflow: Optional[str] = None) -> dict[str, Any]:
    """Which model files the configured ComfyUI templates need and whether each host has them. Starts nothing. Admin only.

    host: "video", "avatar", or omitted for both. workflow: one template to check instead of the
    configured ones (a path under comfyui-workflows/ or a bare avatar template name). Returns
    {"targets": [{"host", "base_url", "workflow", "models": [...], "missing", "downloadable",
    "unknown", "missing_classes", "ok"}], "ok"}.
    """
    tenants.require_admin(_tenant())
    return comfyui_models.audit_configured(host, workflow)


@catalog_tool("ensure_comfyui_models")
def ensure_comfyui_models_tool(host: Optional[str] = None, workflow: Optional[str] = None, download: bool = True) -> dict[str, Any]:
    """Audit the templates and tell each ComfyUI host to download the missing model files it knows the URL for. Admin only.

    Downloads run on the host (through the news-generator helper node, or ComfyUI-Manager as a
    fallback) and are asynchronous: the result lists the jobs started; poll get_comfyui_model_jobs.
    A file with no manifest entry (comfyui-workflows/models.json) is reported under "unknown".
    """
    tenants.require_admin(_tenant())
    return comfyui_models.ensure_configured(host, workflow, download=download)


@catalog_tool("get_comfyui_model_jobs")
def get_comfyui_model_jobs_tool(host: Optional[str] = None) -> dict[str, Any]:
    """Model download jobs each ComfyUI host reports: status, bytes done and total, speed, error. Admin only."""
    tenants.require_admin(_tenant())
    return comfyui_models.jobs_configured(host)


# ----------------------------------------------------------------------------- voices


@catalog_tool("list_voices")
def list_voices_tool(limit: Optional[int] = None, cursor: Optional[str] = None) -> dict[str, Any]:
    """List the tenant's voices: {"items": [{"name", "language", "sample_url", "created_at"}], "next_cursor"}."""
    return tenant_voices.list_voices(_tenant(), limit, cursor)


@catalog_tool("get_voice")
def get_voice_tool(name: str) -> dict[str, Any]:
    """One voice by name, including its TTS library name."""
    return tenant_voices.get_voice(_tenant(), name)


@catalog_tool("upload_voice")
def upload_voice_tool(name: str, sample_path: str, language: str = "en", replace: bool = False) -> dict[str, Any]:
    """Create a voice from a WAV sample on the server host and register it in the TTS library as <tenant>-<name>.

    Fails with conflict if the name exists and replace is false.
    """
    return tenant_voices.upload_voice(_tenant(), name, Path(sample_path), language, replace)


@catalog_tool("delete_voice")
def delete_voice_tool(name: str) -> dict[str, Any]:
    """Delete a voice from the TTS library and the tenant. Returns {"deleted": name}."""
    return tenant_voices.delete_voice(_tenant(), name)


# ----------------------------------------------------------------------------- avatars


@catalog_tool("list_avatars")
def list_avatars_tool(limit: Optional[int] = None, cursor: Optional[str] = None) -> dict[str, Any]:
    """List the tenant's avatars: {"items": [{"name", "image_url", "width", "height", "source", "created_at"}], "next_cursor"}."""
    return tenant_avatars.list_avatars(_tenant(), limit, cursor)


@catalog_tool("get_avatar")
def get_avatar_tool(name: str) -> dict[str, Any]:
    """One avatar with its generation prompt, negative prompt, seed, size, workflow, and source."""
    return tenant_avatars.get_avatar(_tenant(), name)


@catalog_tool("generate_avatar")
def generate_avatar_tool(
    name: str,
    prompt: str,
    negative_prompt: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    seed: Optional[int] = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Generate an avatar image through the avatar ComfyUI workflow. Dimensions must be 16:9 (default from .env)."""
    return tenant_avatars.generate_avatar(_tenant(), name, prompt, negative_prompt, width, height, seed, replace)


@catalog_tool("upload_avatar")
def upload_avatar_tool(name: str, image_path: str, replace: bool = False) -> dict[str, Any]:
    """Register an existing PNG or JPEG on the server host as a tenant avatar."""
    path = Path(image_path)
    if not path.is_file():
        raise ApiError("invalid", f"Image file does not exist: {path}")
    with path.open("rb") as handle:
        return tenant_avatars.upload_avatar(_tenant(), name, handle, replace)


@catalog_tool("delete_avatar")
def delete_avatar_tool(name: str) -> dict[str, Any]:
    """Delete an avatar and its image. Returns {"deleted": name}."""
    return tenant_avatars.delete_avatar(_tenant(), name)


# ----------------------------------------------------------------------------- prompts


@catalog_tool("list_prompts")
def list_prompts_tool() -> list[dict[str, Any]]:
    """The global default prompts (summarize, video_visual, video_sound) with body, file path, and updated_at. Profiles carry their own copies."""
    return prompts.list_prompts()


@catalog_tool("get_prompt")
def get_prompt_tool(kind: str) -> dict[str, Any]:
    """One default prompt: body, file path, whether it is editable, and the {{VARIABLES}} it uses."""
    return prompts.get_prompt(kind)


@catalog_tool("update_prompt")
def update_prompt_tool(kind: str, body: str) -> dict[str, Any]:
    """Rewrite a default prompt file. Admin tenant only; existing profiles keep their own text."""
    return prompts.update_prompt(_tenant(), kind, body)


@catalog_tool("test_prompt")
def test_prompt_tool(kind: str = "summarize", search_text: Optional[str] = None, max_articles: Optional[int] = None) -> dict[str, Any]:
    """Run only search and script with the default summarize prompt. Returns the script, word counts, and sources used."""
    return prompts.test_prompt(_tenant(), kind, search_text, max_articles)


# ----------------------------------------------------------------------------- search profiles


@catalog_tool("list_search_profiles")
def list_search_profiles_tool(limit: Optional[int] = None, cursor: Optional[str] = None) -> dict[str, Any]:
    """List the tenant's search profiles: {"items": [profile], "next_cursor"}."""
    return profiles.list_profiles(_tenant(), limit, cursor)


@catalog_tool("get_search_profile")
def get_search_profile_tool(id: str) -> dict[str, Any]:
    """One search profile by id."""
    return profiles.get_profile(_tenant(), id)


@catalog_tool("upsert_search_profile")
def upsert_search_profile_tool(
    id: Optional[str] = None,
    name: Optional[str] = None,
    search_text: Optional[str] = None,
    categories: Optional[str] = None,
    language: Optional[str] = None,
    safesearch: Optional[str] = None,
    max_articles: Optional[int] = None,
    max_age_days: Optional[int] = None,
    rank_query: Optional[str] = None,
    rank_candidates: Optional[int] = None,
    voice: Optional[str] = None,
    avatar: Optional[str] = None,
    intended_video_max_seconds: Optional[float] = None,
    wpm: Optional[float] = None,
) -> dict[str, Any]:
    """Create a profile (no id; name and search_text required) or update the given fields of an existing one."""
    fields = {k: v for k, v in locals().items() if k != "id" and v is not None}
    return profiles.upsert_profile(_tenant(), fields, id)


@catalog_tool("delete_search_profile")
def delete_search_profile_tool(id: str) -> dict[str, Any]:
    """Delete a search profile. Fails with conflict if a schedule references it."""
    return profiles.delete_profile(_tenant(), id)


@catalog_tool("preview_search")
def preview_search_tool(
    search_text: str,
    categories: Optional[str] = None,
    language: Optional[str] = None,
    max_articles: Optional[int] = None,
    max_age_days: Optional[int] = None,
    rank_query: Optional[str] = None,
    extract: bool = False,
) -> dict[str, Any]:
    """Run search and ranking with these profile values and return the articles a run would select, with outlet, date, score, and snippet. extract=true also fetches each article through crawl4ai and reports words extracted. No script, no video, no run."""
    return search_preview.preview(_tenant(), search_text, categories, language, max_articles, max_age_days, rank_query, extract_articles=extract)


@catalog_tool("get_search_options")
def get_search_options_tool() -> dict[str, Any]:
    """SearXNG categories the instance supports and the .env defaults every profile field falls back to."""
    return search_preview.options()


# ----------------------------------------------------------------------------- runs


@catalog_tool("enqueue_run")
def enqueue_run_tool(
    profile_id: Optional[str] = None,
    overrides: Optional[dict[str, Any]] = None,
    priority: int = 0,
    wait: bool = False,
    wait_timeout_seconds: int = 3600,
) -> dict[str, Any]:
    """Queue a news video run from a search profile, with optional overrides.

    Overrides may include search_text, script_text, max_articles, max_age_days,
    voice, avatar, intended_video_max_seconds, wpm, prompt_enhance. A run needs a
    voice and an avatar from the profile or the overrides. With wait=true the
    call blocks until the run finishes. The API process must be running: it hosts
    the worker that executes runs. Returns the run with status, stage, artifacts.
    """
    return runs.enqueue_run(_tenant(), profile_id, overrides, priority, wait, wait_timeout_seconds)


@catalog_tool("get_run")
def get_run_tool(id: str) -> dict[str, Any]:
    """A run: status (queued, preflight, running, done, failed, cancelled), stage, timings, error, preflight, artifacts, metrics, selected sources."""
    return runs.get_run(_tenant(), id)


@catalog_tool("list_runs")
def list_runs_tool(status: Optional[str] = None, profile_id: Optional[str] = None, limit: Optional[int] = None, cursor: Optional[str] = None) -> dict[str, Any]:
    """Runs newest first, optionally filtered by status or profile: {"items": [run summary], "next_cursor"}."""
    return runs.list_runs(_tenant(), status, profile_id, limit, cursor)


@catalog_tool("cancel_run")
def cancel_run_tool(id: str) -> dict[str, Any]:
    """Cancel a queued run immediately, or request cancellation of a running one (interrupts ComfyUI during the video stage)."""
    return runs.cancel_run(_tenant(), id)


@catalog_tool("retry_run")
def retry_run_tool(id: str) -> dict[str, Any]:
    """Queue a new run that resumes a failed or cancelled run from its failed stage, reusing the artifacts already on disk."""
    return runs.retry_run(_tenant(), id)


@catalog_tool("get_run_file")
def get_run_file_tool(id: str, artifact: str, inline: bool = False) -> dict[str, Any]:
    """Artifact info for a run: video, audio, narration, sources, report, log, or preflight. inline=true returns text content for text artifacts."""
    return runs.get_run_file(_tenant(), id, artifact, inline)


# ----------------------------------------------------------------------------- schedules


@catalog_tool("list_schedules")
def list_schedules_tool() -> list[dict[str, Any]]:
    """The tenant's schedules: id, name, profile_id, cron, timezone, enabled, last_run_id, last_run_at, next_run_at, last_error."""
    return schedules.list_schedules(_tenant())


@catalog_tool("upsert_schedule")
def upsert_schedule_tool(
    id: Optional[str] = None,
    profile_id: Optional[str] = None,
    cron: Optional[str] = None,
    enabled: Optional[bool] = None,
    name: Optional[str] = None,
    timezone: Optional[str] = None,
) -> dict[str, Any]:
    """Create a schedule (profile_id and cron required; five-field cron, evaluated in timezone, default UTC) or update fields of an existing one by id."""
    return schedules.upsert_schedule(_tenant(), profile_id, cron, enabled, name, timezone, id)


@catalog_tool("delete_schedule")
def delete_schedule_tool(id: str) -> dict[str, Any]:
    """Delete a schedule. Returns {"deleted": id}."""
    return schedules.delete_schedule(_tenant(), id)


@catalog_tool("run_schedule_now")
def run_schedule_now_tool(id: str) -> dict[str, Any]:
    """Enqueue the schedule's profile immediately and return the run."""
    return schedules.run_now(_tenant(), id)


# ----------------------------------------------------------------------------- tenants (admin only)


@catalog_tool("list_tenants")
def list_tenants_tool() -> list[dict[str, Any]]:
    """All tenants: slug, name, active, is_admin, created_at. Admin only."""
    return tenants.list_tenants(_tenant())


@catalog_tool("create_tenant")
def create_tenant_tool(slug: str, name: str) -> dict[str, Any]:
    """Create a tenant with seeded prompts and storage. Returns {"tenant", "api_key"}; the key is shown only once. Admin only."""
    return tenants.create_tenant(_tenant(), slug, name)


@catalog_tool("list_api_keys")
def list_api_keys_tool(tenant_slug: str) -> list[dict[str, Any]]:
    """A tenant's API keys: id, prefix, label, created_at, revoked_at. Admin only."""
    return tenants.list_api_keys(_tenant(), tenant_slug)


@catalog_tool("create_api_key")
def create_api_key_tool(tenant_slug: str, label: Optional[str] = None) -> dict[str, Any]:
    """Issue a new API key for a tenant. Returns the raw key once plus its id and prefix. Admin only."""
    return tenants.create_api_key(_tenant(), tenant_slug, label or "")


@catalog_tool("revoke_api_key")
def revoke_api_key_tool(tenant_slug: str, key_id: str) -> dict[str, Any]:
    """Revoke a tenant's API key by id. Admin only."""
    return tenants.revoke_api_key(_tenant(), tenant_slug, key_id)


@catalog_tool("set_tenant_active")
def set_tenant_active_tool(tenant_slug: str, active: bool) -> dict[str, Any]:
    """Activate or deactivate a tenant. Inactive tenants cannot authenticate or enqueue; their schedules are skipped. Admin only."""
    return tenants.set_tenant_active(_tenant(), tenant_slug, active)


# ----------------------------------------------------------------------------- HTTP transport


class ApiKeyMiddleware:
    """ASGI middleware: require a valid tenant API key and expose it to tools for the request."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        raw_key = headers.get("authorization", "").replace("Bearer ", "", 1).strip() or headers.get("x-api-key", "").strip()
        try:
            tenancy.resolve_tenant(raw_key)
        except tenancy.AuthError as exc:
            body = json.dumps(ApiError("unauthorized", str(exc)).to_dict()).encode("utf-8")
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        token = REQUEST_API_KEY.set(raw_key)
        try:
            await self.app(scope, receive, send)
        finally:
            REQUEST_API_KEY.reset(token)


def http_app() -> Any:
    """The MCP server as an ASGI app (streamable HTTP, stateless, JSON responses) behind API-key auth.

    Mount it on FastAPI and run `mcp.session_manager.run()` inside the FastAPI lifespan.
    """
    mcp.settings.streamable_http_path = "/"
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    return ApiKeyMiddleware(mcp.streamable_http_app())


def main() -> None:
    pipeline.load_env()
    logging_setup.configure_logging()
    db.init_db()
    tenancy.bootstrap_admin_tenant()
    mcp.run()


if __name__ == "__main__":
    main()
