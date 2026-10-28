#!/usr/bin/env python3
"""FastAPI wrapper for the news generator pipeline."""
# pylint: disable=broad-exception-caught

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server import server as mcp_server  # noqa: E402
from services import avatars, comfyui_models, db, logging_setup, pipeline, preflight, profiles, prompts, runs, scheduler, schedules, search_preview, system, tenancy, tenant_avatars, tenant_voices, tenants, tts, worker  # noqa: E402
from services.errors import ApiError, not_found  # noqa: E402
from services.models import Tenant  # noqa: E402


@asynccontextmanager
async def lifespan(_: FastAPI):
    pipeline.load_env()
    logging_setup.configure_logging()
    db.init_db()
    tenancy.bootstrap_admin_tenant()
    worker.start_worker()
    scheduler.start_scheduler()
    async with mcp_server.mcp.session_manager.run():
        yield
    scheduler.stop_scheduler()
    worker.stop_worker()


mcp_http_app = mcp_server.http_app()
app = FastAPI(title="News Generator API", lifespan=lifespan)
app.mount("/mcp", mcp_http_app)


class RequestContextMiddleware:
    """Correlation id (X-Request-ID in or generated), client IP, and one access log line per request."""

    def __init__(self, asgi_app) -> None:
        self.app = asgi_app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        correlation_id = headers.get("x-request-id") or uuid.uuid4().hex[:16]
        forwarded = headers.get("x-forwarded-for", "").split(",")[0].strip()
        client_ip = forwarded or (scope.get("client") or ("", 0))[0]
        tokens = (logging_setup.correlation_id_var.set(correlation_id), logging_setup.client_ip_var.set(client_ip),
                  logging_setup.tenant_id_var.set(None), logging_setup.tenant_slug_var.set(None))
        status_holder = {"status": 0}
        started = time.perf_counter()

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", correlation_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            path = scope.get("path", "")
            if path not in ("/health",):
                pipeline.LOGGER.info("%s %s -> %s in %d ms", scope.get("method"), path, status_holder["status"], duration_ms,
                                     extra={"event": "http", "method": scope.get("method"), "path": path, "status": status_holder["status"], "duration_ms": duration_ms})
            for var, token in zip((logging_setup.correlation_id_var, logging_setup.client_ip_var, logging_setup.tenant_id_var, logging_setup.tenant_slug_var), tokens):
                var.reset(token)


app.add_middleware(RequestContextMiddleware)


ADMIN_DIST = PROJECT_ROOT / "web" / "dist"
if (ADMIN_DIST / "index.html").is_file():
    app.mount("/admin/assets", StaticFiles(directory=ADMIN_DIST / "assets"), name="admin-assets")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{path:path}", include_in_schema=False)
    def admin_ui(path: str = "") -> FileResponse:
        """The React admin UI. Any path under /admin serves index.html (client-side routing); real files under dist are served as-is."""
        candidate = (ADMIN_DIST / path).resolve() if path else None
        if candidate is not None and candidate.is_file() and ADMIN_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(ADMIN_DIST / "index.html")


MEDIA_ROUTE_MARKERS = ("/files/", "/image", "/sample")


async def get_tenant(request: Request, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)) -> Tenant:
    """Resolve the calling tenant from Authorization: Bearer <key> or X-API-Key.

    Async on purpose: a sync dependency runs in a worker thread with a copy of the request
    context, so the tenant and actor context variables it sets would never reach the endpoint.
    """
    raw_key = (authorization or "").replace("Bearer ", "", 1).strip() or (x_api_key or "").strip()
    if not raw_key and any(marker in request.url.path for marker in MEDIA_ROUTE_MARKERS):
        # Media elements cannot send headers; the admin UI passes the key as a query parameter on these routes only.
        raw_key = (request.query_params.get("api_key") or "").strip()
    try:
        return tenancy.resolve_tenant(raw_key)
    except tenancy.AuthError as exc:
        raise ApiError("unauthorized", str(exc)) from exc


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=exc.status_code)


def safe_upload_name(name: str, fallback: str) -> str:
    source = Path(name or fallback).name
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", source).strip("-._")
    return cleaned or fallback


def save_upload(upload: UploadFile, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output_file:
        shutil.copyfileobj(upload.file, output_file)
    return destination


def error_response(run_id: Optional[str], stage: str, message: str, status_code: int = 500) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "error",
            "run_id": run_id,
            "stage": stage,
            "message": message,
        },
    )


def require_avatar_api_key(authorization: Optional[str], x_api_key: Optional[str]) -> None:
    expected_key = os.environ.get("AVATAR_API_KEY", "").strip()
    if not expected_key:
        return
    provided_key = (authorization or "").replace("Bearer ", "", 1).strip() or (x_api_key or "").strip()
    if provided_key != expected_key:
        raise HTTPException(status_code=401, detail="Invalid or missing avatar API key.")


def stage_run_directory(run_id: Optional[str], subject: str) -> tuple[str, Path]:
    if run_id:
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", run_id):
            raise ValueError("Invalid run_id.")
        run_dir = pipeline.output_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_id, run_dir
    return pipeline.create_run_directory(subject)


def resolve_anchor_image(
    *,
    run_dir: Path,
    anchor_image_file: Optional[UploadFile],
    avatar_name: Optional[str],
    avatar_prompt: Optional[str],
    replace_avatar: bool,
    avatar_width: Optional[int],
    avatar_height: Optional[int],
    authorization: Optional[str],
    x_api_key: Optional[str],
) -> Path:
    if anchor_image_file is not None:
        anchor_name = safe_upload_name(anchor_image_file.filename or "anchor.png", "anchor.png")
        return save_upload(anchor_image_file, run_dir / anchor_name)
    if avatar_name and avatar_name.strip():
        clean_avatar_name = avatars.avatar_public_name(avatar_name)
        if avatar_prompt and avatar_prompt.strip():
            require_avatar_api_key(authorization, x_api_key)
            avatars.generate_avatar(clean_avatar_name, avatar_prompt, replace_avatar, avatar_width, avatar_height)
        source_avatar_path = avatars.avatar_file_path(clean_avatar_name)
        anchor_path = run_dir / f"{clean_avatar_name}.png"
        shutil.copyfile(source_avatar_path, anchor_path)
        return anchor_path
    raise ValueError("Provide anchor_image_file or avatar_name.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/me")
def api_me(tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """The calling tenant, resolved from the API key."""
    return JSONResponse({"tenant": {"slug": tenant.slug, "name": tenant.name, "active": tenant.active, "is_admin": tenant.is_admin, "created_at": tenant.created_at.isoformat()}})


@app.get("/api/v1/system")
def api_system(tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """Version, video settings, configured services, and storage (catalog: get_system_info)."""
    return JSONResponse(system.get_system_info(tenant))


# ----------------------------------------------------------------------------- /api/v1 system: ComfyUI models (admin only)


@app.get("/api/v1/system/comfyui/models")
def api_comfyui_models_audit(host: Optional[str] = None, workflow: Optional[str] = None, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """Which model files the configured templates need and whether each ComfyUI host has them. No downloads (catalog: audit_comfyui_models)."""
    tenants.require_admin(tenant)
    return JSONResponse(comfyui_models.audit_configured(host, workflow))


@app.post("/api/v1/system/comfyui/models/ensure")
async def api_comfyui_models_ensure(request: Request, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """Audit and start downloads on the host for every missing model the manifest knows. 202; poll the jobs route (catalog: ensure_comfyui_models)."""
    tenants.require_admin(tenant)
    raw = await request.body()
    body = json.loads(raw) if raw.strip() else {}
    if not isinstance(body, dict):
        raise ApiError("invalid", "Body must be a JSON object.")
    # The audit talks to the ComfyUI hosts over HTTP; keep the event loop free while it does.
    result = await run_in_threadpool(comfyui_models.ensure_configured, body.get("host"), body.get("workflow"), bool(body.get("download", True)))
    return JSONResponse(result, status_code=202 if any(t.get("started") for t in result["targets"]) else 200)


@app.get("/api/v1/system/comfyui/models/jobs")
def api_comfyui_models_jobs(host: Optional[str] = None, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """Download jobs each ComfyUI host reports: status, bytes, speed, error (catalog: get_comfyui_model_jobs)."""
    tenants.require_admin(tenant)
    return JSONResponse(comfyui_models.jobs_configured(host))


@app.get("/api/v1/system/comfyui/models/manifest")
def api_comfyui_models_manifest(tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """Manifest coverage: every template model reference with and without a manifest entry."""
    tenants.require_admin(tenant)
    return JSONResponse(comfyui_models.check_manifest())


# ----------------------------------------------------------------------------- /api/v1 voices


@app.get("/api/v1/voices")
def api_list_voices(limit: Optional[int] = None, cursor: Optional[str] = None, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(tenant_voices.list_voices(tenant, limit, cursor))


@app.post("/api/v1/voices", status_code=201)
def api_upload_voice(
    name: str = Form(...),
    sample: UploadFile = File(...),
    language: str = Form("en"),
    replace: bool = Form(False),
    tenant: Tenant = Depends(get_tenant),
) -> JSONResponse:
    return JSONResponse(tenant_voices.upload_voice(tenant, name, sample.file, language, replace), status_code=201)


@app.get("/api/v1/voices/{name}")
def api_get_voice(name: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(tenant_voices.get_voice(tenant, name))


@app.get("/api/v1/voices/{name}/sample")
def api_voice_sample(name: str, tenant: Tenant = Depends(get_tenant)):
    path = tenant_voices.sample_path(tenant, name)
    return FileResponse(path, media_type="audio/wav", filename=path.name)


@app.delete("/api/v1/voices/{name}")
def api_delete_voice(name: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(tenant_voices.delete_voice(tenant, name))


@app.get("/api/v1/library-voices/{name}/sample")
def api_library_voice_sample(name: str, tenant: Tenant = Depends(get_tenant)) -> Response:
    """A short sample of any voice in the TTS library, synthesized per request and passed straight through; nothing is stored."""
    try:
        audio = tts.preview(name, min(tts.default_timeout(), 60))
    except tts.VoiceResolutionError as exc:
        raise not_found(f"Voice {name!r} in the {tts.label()} library") from exc
    except Exception as exc:
        raise ApiError("service_unavailable", f"{tts.label()} could not synthesize a sample of voice {name!r}: {exc}") from exc
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})


# ----------------------------------------------------------------------------- /api/v1 avatars


@app.get("/api/v1/avatars")
def api_list_avatars(limit: Optional[int] = None, cursor: Optional[str] = None, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(tenant_avatars.list_avatars(tenant, limit, cursor))


@app.post("/api/v1/avatars/generate", status_code=201)
def api_generate_avatar(
    name: str = Form(...),
    prompt: str = Form(...),
    negative_prompt: Optional[str] = Form(None),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    seed: Optional[int] = Form(None),
    replace: bool = Form(False),
    workflow: Optional[str] = Form(None),
    tenant: Tenant = Depends(get_tenant),
) -> JSONResponse:
    return JSONResponse(tenant_avatars.generate_avatar(tenant, name, prompt, negative_prompt, width, height, seed, replace, workflow=workflow), status_code=201)


@app.post("/api/v1/avatars/upload", status_code=201)
def api_upload_avatar(
    name: str = Form(...),
    image: UploadFile = File(...),
    replace: bool = Form(False),
    tenant: Tenant = Depends(get_tenant),
) -> JSONResponse:
    return JSONResponse(tenant_avatars.upload_avatar(tenant, name, image.file, replace), status_code=201)


@app.get("/api/v1/avatars/options")
def api_avatar_options(tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(tenant_avatars.avatar_options())


@app.get("/api/v1/avatars/{name}")
def api_get_avatar(name: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(tenant_avatars.get_avatar(tenant, name))


@app.get("/api/v1/avatars/{name}/image")
def api_avatar_image(name: str, tenant: Tenant = Depends(get_tenant)):
    path = tenant_avatars.image_path(tenant, name)
    return FileResponse(path, media_type="image/png", filename=path.name)


@app.delete("/api/v1/avatars/{name}")
def api_delete_avatar(name: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(tenant_avatars.delete_avatar(tenant, name))


# ----------------------------------------------------------------------------- /api/v1 prompts


@app.get("/api/v1/prompts")
def api_list_prompts(tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """The global default prompts with bodies. Any tenant may read them; they fill new profiles."""
    return JSONResponse(prompts.list_prompts())


@app.get("/api/v1/prompts/{kind}")
def api_get_prompt(kind: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(prompts.get_prompt(kind))


@app.put("/api/v1/prompts/{kind}")
def api_update_prompt(kind: str, body: str = Form(...), tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """Rewrite a default prompt file. Admin tenant only."""
    return JSONResponse(prompts.update_prompt(tenant, kind, body))


@app.post("/api/v1/prompts/{kind}/test")
def api_test_prompt(
    kind: str,
    search_text: Optional[str] = Form(None),
    max_articles: Optional[int] = Form(None),
    tenant: Tenant = Depends(get_tenant),
) -> JSONResponse:
    try:
        return JSONResponse(prompts.test_prompt(tenant, kind, search_text, max_articles))
    except pipeline.PipelineError as exc:
        raise ApiError("pipeline_failed", exc.message, stage=exc.stage) from exc


# ----------------------------------------------------------------------------- /api/v1 search profiles


@app.get("/api/v1/profiles")
def api_list_profiles(limit: Optional[int] = None, cursor: Optional[str] = None, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(profiles.list_profiles(tenant, limit, cursor))


@app.post("/api/v1/profiles", status_code=201)
async def api_create_profile(request: Request, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    fields = await request.json()
    if not isinstance(fields, dict):
        raise ApiError("invalid", "Body must be a JSON object of profile fields.")
    return JSONResponse(profiles.upsert_profile(tenant, fields), status_code=201)


@app.get("/api/v1/profiles/{profile_id}")
def api_get_profile(profile_id: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(profiles.get_profile(tenant, profile_id))


@app.put("/api/v1/profiles/{profile_id}")
async def api_update_profile(profile_id: str, request: Request, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    fields = await request.json()
    if not isinstance(fields, dict):
        raise ApiError("invalid", "Body must be a JSON object of profile fields.")
    return JSONResponse(profiles.upsert_profile(tenant, fields, profile_id))


@app.delete("/api/v1/profiles/{profile_id}")
def api_delete_profile(profile_id: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(profiles.delete_profile(tenant, profile_id))


# ----------------------------------------------------------------------------- /api/v1 runs


@app.post("/api/v1/runs", status_code=202)
async def api_enqueue_run(request: Request, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """Queue a run: json {profile_id?, overrides?, priority?, wait?, wait_timeout_seconds?}."""
    body = await request.json()
    if not isinstance(body, dict):
        raise ApiError("invalid", "Body must be a JSON object.")
    result = runs.enqueue_run(
        tenant,
        profile_id=body.get("profile_id"),
        overrides=body.get("overrides"),
        priority=int(body.get("priority") or 0),
        wait=bool(body.get("wait", False)),
        wait_timeout_seconds=int(body.get("wait_timeout_seconds") or 3600),
    )
    return JSONResponse(result, status_code=200 if body.get("wait") else 202)


@app.get("/api/v1/runs")
def api_list_runs(
    status: Optional[str] = None,
    profile_id: Optional[str] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    tenant: Tenant = Depends(get_tenant),
) -> JSONResponse:
    return JSONResponse(runs.list_runs(tenant, status, profile_id, limit, cursor))


@app.get("/api/v1/runs/{run_id}")
def api_get_run(run_id: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(runs.get_run(tenant, run_id))


@app.post("/api/v1/runs/{run_id}/cancel")
def api_cancel_run(run_id: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(runs.cancel_run(tenant, run_id))


@app.delete("/api/v1/runs/{run_id}")
def api_delete_run(run_id: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(runs.delete_run(tenant, run_id))


@app.post("/api/v1/runs/{run_id}/retry", status_code=202)
def api_retry_run(run_id: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(runs.retry_run(tenant, run_id), status_code=202)


@app.get("/api/v1/runs/{run_id}/chunks/{index}/hero/image")
def api_run_chunk_hero(run_id: str, index: int, tenant: Tenant = Depends(get_tenant)) -> FileResponse:
    path = runs.chunk_hero_path(tenant, run_id, index)
    return FileResponse(path, media_type="image/png", filename=path.name)


@app.get("/api/v1/runs/{run_id}/files/{artifact}/info")
def api_run_file_info(run_id: str, artifact: str, inline: bool = False, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(runs.get_run_file(tenant, run_id, artifact, inline))


@app.get("/api/v1/runs/{run_id}/files/{artifact}")
def api_run_file(run_id: str, artifact: str, tenant: Tenant = Depends(get_tenant)):
    path, content_type = runs.artifact_path(tenant, run_id, artifact)
    return FileResponse(path, media_type=content_type, filename=path.name)


# ----------------------------------------------------------------------------- /api/v1 schedules


@app.get("/api/v1/schedules")
def api_list_schedules(tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(schedules.list_schedules(tenant))


async def _schedule_fields(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise ApiError("invalid", "Body must be a JSON object.")
    return {
        "profile_id": body.get("profile_id"),
        "cron": body.get("cron"),
        "enabled": body.get("enabled"),
        "name": body.get("name"),
        "timezone_name": body.get("timezone"),
    }


@app.post("/api/v1/schedules", status_code=201)
async def api_create_schedule(request: Request, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(schedules.upsert_schedule(tenant, **await _schedule_fields(request)), status_code=201)


@app.get("/api/v1/schedules/{schedule_id}")
def api_get_schedule(schedule_id: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(schedules.get_schedule(tenant, schedule_id))


@app.put("/api/v1/schedules/{schedule_id}")
async def api_update_schedule(schedule_id: str, request: Request, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(schedules.upsert_schedule(tenant, schedule_id=schedule_id, **await _schedule_fields(request)))


@app.delete("/api/v1/schedules/{schedule_id}")
def api_delete_schedule(schedule_id: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(schedules.delete_schedule(tenant, schedule_id))


@app.post("/api/v1/schedules/{schedule_id}/run", status_code=202)
def api_run_schedule_now(schedule_id: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(schedules.run_now(tenant, schedule_id), status_code=202)


# ----------------------------------------------------------------------------- /api/v1 tenants (admin only)


@app.get("/api/v1/tenants")
def api_list_tenants(tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(tenants.list_tenants(tenant))


@app.post("/api/v1/tenants", status_code=201)
async def api_create_tenant(request: Request, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    body = await request.json()
    if not isinstance(body, dict):
        raise ApiError("invalid", "Body must be a JSON object with slug and name.")
    return JSONResponse(tenants.create_tenant(tenant, body.get("slug", ""), body.get("name", "")), status_code=201)


@app.get("/api/v1/tenants/{slug}/keys")
def api_list_api_keys(slug: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(tenants.list_api_keys(tenant, slug))


@app.post("/api/v1/tenants/{slug}/keys", status_code=201)
async def api_create_api_key(slug: str, request: Request, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    body = await request.json() if int(request.headers.get("content-length") or 0) > 0 else {}
    label = body.get("label", "") if isinstance(body, dict) else ""
    return JSONResponse(tenants.create_api_key(tenant, slug, label), status_code=201)


@app.delete("/api/v1/tenants/{slug}/keys/{key_id}")
def api_revoke_api_key(slug: str, key_id: str, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    return JSONResponse(tenants.revoke_api_key(tenant, slug, key_id))


@app.put("/api/v1/tenants/{slug}/active")
async def api_set_tenant_active(slug: str, request: Request, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    body = await request.json()
    if not isinstance(body, dict) or "active" not in body:
        raise ApiError("invalid", "Body must be a JSON object with active: true|false.")
    return JSONResponse(tenants.set_tenant_active(tenant, slug, bool(body["active"])))


@app.get("/api/v1/search/options")
def api_search_options(tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """SearXNG categories and the .env defaults every profile field falls back to."""
    return JSONResponse(search_preview.options())


@app.post("/api/v1/search/preview")
async def api_search_preview(request: Request, tenant: Tenant = Depends(get_tenant)) -> JSONResponse:
    """Run search and ranking with the given profile values and return what a run would select. No LLM, no run."""
    body = await request.json()
    if not isinstance(body, dict):
        raise ApiError("invalid", "Body must be a JSON object.")
    return JSONResponse(search_preview.preview(
        tenant,
        body.get("search_text", ""),
        categories=body.get("categories"),
        language=body.get("language"),
        max_articles=body.get("max_articles"),
        max_age_days=body.get("max_age_days"),
        rank_query=body.get("rank_query"),
        rank_candidates=body.get("rank_candidates"),
        extract_articles=bool(body.get("extract", False)),
    ))


@app.get("/health/deep")
def health_deep(voice: Optional[str] = None, avatar_name: Optional[str] = None) -> JSONResponse:
    """Preflight every dependency. 200 when all hard checks pass, 503 otherwise. Cached 30 s."""
    pipeline.load_env()
    anchor_path = None
    if avatar_name:
        try:
            anchor_path = avatars.avatar_file_path(avatar_name)
        except (FileNotFoundError, avatars.AvatarServiceError):
            anchor_path = avatars.avatar_storage_root() / f"{avatar_name}.png"
    result = preflight.run_preflight(voice=voice, anchor_path=anchor_path)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


@app.get("/voices")
def get_voices() -> JSONResponse:
    pipeline.load_env()
    try:
        return JSONResponse(tts.list_voices())
    except Exception as exc:
        return error_response(None, "voices", str(exc))


@app.get("/avatars")
def list_avatars() -> JSONResponse:
    pipeline.load_env()
    try:
        return JSONResponse(avatars.list_avatars())
    except Exception as exc:
        return error_response(None, "avatars", str(exc))


@app.get("/avatars/{avatar_name}")
def get_avatar(avatar_name: str, width: Optional[int] = None, height: Optional[int] = None):
    pipeline.load_env()
    try:
        if width is None and height is None:
            path = avatars.avatar_file_path(avatar_name)
            return FileResponse(path, media_type="image/png", filename=f"{avatars.avatar_public_name(avatar_name)}.png")
        image_data = avatars.resized_avatar_bytes(avatar_name, width, height)
        return Response(content=image_data, media_type="image/png")
    except avatars.AvatarServiceError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Avatar not found.") from exc


@app.post("/avatars/{avatar_name}/upload")
def upload_avatar(
    avatar_name: str,
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> JSONResponse:
    pipeline.load_env()
    require_avatar_api_key(authorization, x_api_key)
    try:
        return JSONResponse(avatars.save_avatar_image(avatar_name, file.file, "upload"), status_code=201)
    except avatars.AvatarServiceError as exc:
        return error_response(None, "avatars", exc.message, 400)
    except Exception as exc:
        return error_response(None, "avatars", str(exc))


@app.post("/avatars/generate")
def generate_avatar(
    avatar_name: str = Form(...),
    avatar_prompt: str = Form(...),
    replace_avatar: bool = Form(False),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> JSONResponse:
    pipeline.load_env()
    require_avatar_api_key(authorization, x_api_key)
    try:
        return JSONResponse(avatars.generate_avatar(avatar_name, avatar_prompt, replace_avatar, width, height), status_code=201)
    except avatars.AvatarServiceError as exc:
        return error_response(None, "avatars", exc.message, 400)
    except Exception as exc:
        return error_response(None, "avatars", str(exc))


@app.post("/stages/script")
def stage_script(
    search_text: Optional[str] = Form(None),
    script_text: Optional[str] = Form(None),
    categories: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
    safesearch: Optional[str] = Form(None),
    max_articles: Optional[int] = Form(None),
    max_age_days: Optional[int] = Form(None),
    time_range: Optional[str] = Form(None),
    search_timeout_seconds: Optional[int] = Form(None),
    rerank_top_n: Optional[int] = Form(None),
    summarize_prompt: Optional[str] = Form(None),
    intended_video_max_seconds: Optional[float] = Form(None),
    wpm: Optional[float] = Form(None),
) -> JSONResponse:
    pipeline.load_env()
    clean_search_text = search_text.strip() if search_text else ""
    clean_script_text = script_text.strip() if script_text else ""
    if not clean_search_text and not clean_script_text:
        return error_response(None, "request", "Provide search_text or script_text.", 400)

    run_id, run_dir = pipeline.create_run_directory(clean_search_text or "provided script")
    try:
        result = pipeline.generate_script_stage(
            run_id=run_id,
            run_dir=run_dir,
            search_text=clean_search_text or "provided script",
            script_text=clean_script_text or None,
            categories=categories,
            language=language,
            safesearch=safesearch,
            max_articles=max_articles,
            max_age_days=max_age_days,
            time_range=time_range,
            search_timeout_seconds=search_timeout_seconds,
            rerank_top_n=rerank_top_n,
            summarize_prompt=summarize_prompt,
            intended_video_max_seconds=intended_video_max_seconds,
            wpm=wpm,
        )
    except pipeline.PipelineError as exc:
        return error_response(run_id, exc.stage, exc.message)
    return JSONResponse(result)


@app.post("/stages/audio")
def stage_audio(
    voice: str = Form(...),
    script_text: Optional[str] = Form(None),
    search_text: Optional[str] = Form(None),
    run_id: Optional[str] = Form(None),
    voice_sample_file: Optional[UploadFile] = File(None),
) -> JSONResponse:
    pipeline.load_env()
    if not voice.strip():
        return error_response(run_id, "request", "voice is required.", 400)
    clean_script_text = script_text.strip() if script_text else ""
    clean_search_text = search_text.strip() if search_text else ""

    try:
        resolved_run_id, run_dir = stage_run_directory(run_id, clean_search_text or "audio stage")
        script_path = run_dir / "narration.txt"
        if clean_script_text:
            script_result = pipeline.generate_script_stage(
                run_id=resolved_run_id,
                run_dir=run_dir,
                search_text=clean_search_text or "provided script",
                script_text=clean_script_text,
            )
            script = script_result["script"]
        elif script_path.is_file():
            script = script_path.read_text(encoding="utf-8").strip()
        else:
            return error_response(resolved_run_id, "request", "Provide script_text or a run_id with narration.txt.", 400)

        voice_sample_path = None
        if voice_sample_file is not None:
            sample_name = safe_upload_name(voice_sample_file.filename or "voice-sample.wav", "voice-sample.wav")
            voice_sample_path = save_upload(voice_sample_file, run_dir / sample_name)
        result = pipeline.generate_audio_stage(
            run_id=resolved_run_id,
            run_dir=run_dir,
            script=script,
            voice=voice,
            voice_sample_path=voice_sample_path,
        )
    except pipeline.PipelineError as exc:
        return error_response(run_id, exc.stage, exc.message)
    except Exception as exc:
        return error_response(run_id, "request", str(exc), 400)
    return JSONResponse(result)


@app.post("/stages/video")
def stage_video(
    run_id: Optional[str] = Form(None),
    script_text: Optional[str] = Form(None),
    audio_file: Optional[UploadFile] = File(None),
    anchor_image_file: Optional[UploadFile] = File(None),
    avatar_name: Optional[str] = Form(None),
    avatar_prompt: Optional[str] = Form(None),
    replace_avatar: bool = Form(False),
    avatar_width: Optional[int] = Form(None),
    avatar_height: Optional[int] = Form(None),
    video_prompt: Optional[str] = Form(None),
    video_sound_prompt: Optional[str] = Form(None),
    prompt_enhance: Optional[bool] = Form(None),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> JSONResponse:
    pipeline.load_env()
    clean_script_text = script_text.strip() if script_text else ""

    try:
        resolved_run_id, run_dir = stage_run_directory(run_id, "video stage")
        script_path = run_dir / "narration.txt"
        if clean_script_text:
            script_path.write_text(clean_script_text + "\n", encoding="utf-8")
            script = clean_script_text
        elif script_path.is_file():
            script = script_path.read_text(encoding="utf-8").strip()
        else:
            return error_response(resolved_run_id, "request", "Provide script_text or a run_id with narration.txt.", 400)

        if audio_file is not None:
            audio_name = safe_upload_name(audio_file.filename or "narration.wav", "narration.wav")
            narration_path = save_upload(audio_file, run_dir / audio_name)
        else:
            narration_path = run_dir / "narration.wav"
            if not narration_path.is_file():
                return error_response(resolved_run_id, "request", "Provide audio_file or a run_id with narration.wav.", 400)

        anchor_path = resolve_anchor_image(
            run_dir=run_dir,
            anchor_image_file=anchor_image_file,
            avatar_name=avatar_name,
            avatar_prompt=avatar_prompt,
            replace_avatar=replace_avatar,
            avatar_width=avatar_width,
            avatar_height=avatar_height,
            authorization=authorization,
            x_api_key=x_api_key,
        )
        result = pipeline.generate_video_stage(
            run_id=resolved_run_id,
            run_dir=run_dir,
            script=script,
            anchor_path=anchor_path,
            narration_path=narration_path,
            video_prompt=video_prompt,
            video_sound_prompt=video_sound_prompt,
            prompt_enhance=prompt_enhance,
        )
    except avatars.AvatarServiceError as exc:
        return error_response(run_id, "request", exc.message, 400)
    except FileNotFoundError:
        return error_response(run_id, "request", "Avatar not found.", 400)
    except pipeline.PipelineError as exc:
        return error_response(run_id, exc.stage, exc.message)
    except Exception as exc:
        return error_response(run_id, "request", str(exc), 400)
    return JSONResponse(result)


@app.post("/generate")
def generate_news_report(
    search_text: Optional[str] = Form(None),
    script_text: Optional[str] = Form(None),
    voice: str = Form(...),
    anchor_image_file: Optional[UploadFile] = File(None),
    voice_sample_file: Optional[UploadFile] = File(None),
    avatar_name: Optional[str] = Form(None),
    avatar_prompt: Optional[str] = Form(None),
    replace_avatar: bool = Form(False),
    avatar_width: Optional[int] = Form(None),
    avatar_height: Optional[int] = Form(None),
    categories: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
    safesearch: Optional[str] = Form(None),
    max_articles: Optional[int] = Form(None),
    max_age_days: Optional[int] = Form(None),
    time_range: Optional[str] = Form(None),
    search_timeout_seconds: Optional[int] = Form(None),
    rerank_top_n: Optional[int] = Form(None),
    summarize_prompt: Optional[str] = Form(None),
    intended_video_max_seconds: Optional[float] = Form(None),
    wpm: Optional[float] = Form(None),
    video_prompt: Optional[str] = Form(None),
    video_sound_prompt: Optional[str] = Form(None),
    prompt_enhance: Optional[bool] = Form(None),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> JSONResponse:
    pipeline.load_env()
    clean_search_text = search_text.strip() if search_text else ""
    clean_script_text = script_text.strip() if script_text else ""
    if not clean_search_text and not clean_script_text:
        return error_response(None, "request", "Provide search_text or script_text.", 400)
    if not voice.strip():
        return error_response(None, "request", "voice is required.", 400)

    run_id: Optional[str] = None
    try:
        run_subject = clean_search_text or "provided script"
        run_id, run_dir = pipeline.create_run_directory(run_subject)
        anchor_path = resolve_anchor_image(
            run_dir=run_dir,
            anchor_image_file=anchor_image_file,
            avatar_name=avatar_name,
            avatar_prompt=avatar_prompt,
            replace_avatar=replace_avatar,
            avatar_width=avatar_width,
            avatar_height=avatar_height,
            authorization=authorization,
            x_api_key=x_api_key,
        )

        voice_sample_path = None
        if voice_sample_file is not None:
            sample_name = safe_upload_name(voice_sample_file.filename or "voice-sample.wav", "voice-sample.wav")
            voice_sample_path = save_upload(voice_sample_file, run_dir / sample_name)
    except avatars.AvatarServiceError as exc:
        return error_response(run_id, "request", exc.message, 400)
    except FileNotFoundError:
        return error_response(run_id, "request", "Avatar not found.", 400)
    except Exception as exc:
        return error_response(run_id, "request", str(exc), 400)

    try:
        result = pipeline.generate_news_video(
            run_id=run_id,
            run_dir=run_dir,
            search_text=run_subject,
            voice=voice,
            anchor_path=anchor_path,
            script_text=clean_script_text or None,
            voice_sample_path=voice_sample_path,
            categories=categories,
            language=language,
            safesearch=safesearch,
            max_articles=max_articles,
            max_age_days=max_age_days,
            time_range=time_range,
            search_timeout_seconds=search_timeout_seconds,
            rerank_top_n=rerank_top_n,
            summarize_prompt=summarize_prompt,
            intended_video_max_seconds=intended_video_max_seconds,
            wpm=wpm,
            video_prompt=video_prompt,
            video_sound_prompt=video_sound_prompt,
            prompt_enhance=prompt_enhance,
        )
    except pipeline.PipelineError as exc:
        return error_response(run_id, exc.stage, exc.message)

    return JSONResponse(result)


@app.get("/runs/{run_id}/video")
def get_video(run_id: str) -> FileResponse:
    try:
        path = pipeline.artifact_path(run_id, "video")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Video not found.") from exc
    return FileResponse(path, media_type="video/mp4", filename="video.mp4")


@app.get("/runs/{run_id}/audio")
def get_audio(run_id: str) -> FileResponse:
    try:
        path = pipeline.artifact_path(run_id, "audio")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Audio not found.") from exc
    return FileResponse(path, media_type="audio/wav", filename="narration.wav")


@app.get("/runs/{run_id}/script")
def get_script(run_id: str) -> FileResponse:
    try:
        path = pipeline.artifact_path(run_id, "script")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Script not found.") from exc
    return FileResponse(path, media_type="text/plain", filename="narration.txt")


@app.get("/runs/{run_id}/report")
def get_report(run_id: str) -> FileResponse:
    try:
        path = pipeline.artifact_path(run_id, "report")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Report not found.") from exc
    return FileResponse(path, media_type="text/markdown", filename="report.md")


@app.get("/runs/{run_id}/sources")
def get_sources(run_id: str) -> FileResponse:
    try:
        path = pipeline.artifact_path(run_id, "sources")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Sources not found.") from exc
    return FileResponse(path, media_type="application/json", filename="sources.json")