# UI Dev 1: Management UI, Multi-Tenancy, Run Queue, Preflight

Status: plan agreed 2026-09-14. Implemented and merged to main (PR #1, 2026-09-14): preflight and `/health/deep`, SQLite schema and tenancy with admin bootstrap, CRUD for voices, avatars, prompts, and search profiles, the run queue with worker, cancel, and retry, and schedules with the scheduler thread. On `feature/tenant-admin-mcp-http`: admin tenant tools and the MCP streamable HTTP transport. Every catalog operation now exists as a service function, a `/api/v1` route, and an MCP tool over stdio and HTTP. Not started: React UI, metering, logging streams and Seq, YouTube, Slack, segmented rendering, support images, Caddy.

## Decisions made

- Database: SQLite now, through SQLModel, one file under a configurable data directory. Postgres later by changing the connection URL only.
- Front end: React with TypeScript and Vite, built bundle served by FastAPI under `/admin`. One deployable process. The JSON API is the product surface; the UI is a client of it.
- Tenancy split: `.env` is system control. It holds service endpoints (ComfyUI video and avatar, TTS, LLM, reranker, SearXNG), storage paths, output size, and the admin tenant's bootstrap API key. Everything tenant-specific lives in the database. Tenants never see or set service URLs.
- The current single-tenant `.env` flow keeps working unchanged. On first start the app creates the admin tenant from `.env`, so nothing that works today breaks.

## What exists today

FastAPI app with routes for health, voices list, avatar list/get/upload/generate, three stage endpoints (script, audio, video), a blocking `/generate`, and artifact downloads per run. All configuration is `.env` plus files on disk: prompts under `prompts/`, avatars under `assets/avatars/`, runs under `output/`. There is no persistence layer, no queue, no auth beyond the avatar API key, and `/generate` blocks for the full render time.

## Phase 1: Persistence and tenant model

No UI in this phase. Goal: every screen in Phase 2 has data to read and write.

Tables, all with `tenant_id` except `tenants`:

| Table | Purpose |
|---|---|
| tenants | slug, display name, created_at, active flag |
| api_keys | hashed key, tenant_id, label, created_at, revoked_at |
| avatars | name, image path, prompt, negative prompt, seed, width, height, workflow used, source (generated or uploaded) |
| voices | name as shown to the tenant, TTS library name (prefixed), sample path, language |
| prompts | kind (summarize, video visual, video sound), name, body, is_default, updated_at |
| search_profiles | name, search text, categories, language, safesearch, max articles, max age days, rank query, rank candidates, voice, avatar, intended seconds, wpm |
| schedules | profile_id, cron expression, enabled, last_run_at, next_run_at |
| runs | see Run queue below |

Rules:

- Tenant resolution: API key in the `Authorization` header maps to a tenant. A `client_id` slug is fine as a display and URL element, but access is keyed on the secret, not the slug.
- Per-tenant storage: `assets/<tenant>/avatars/` and `output/<tenant>/<run_id>/`. Artifact routes check tenant ownership.
- Voices: the TTS server has one flat library with no tenancy. Library names are prefixed `<tenant>-<voice>`; the app strips the prefix in listings. Upload and delete already exist in `scripts/generate_audio.py` and only need routes.
- Prompts: the markdown files under `prompts/` become seed rows and remain the defaults for new tenants. A tenant edits its own copy. The user-message prose currently inside `services/prompting.py` moves into the same table, so no prompt text remains in code.
- Search profiles replace the dozen form fields on `/generate`; requests pass `profile_id` and optional overrides.

## Phase 2: Management UI

React app in `web/`, built to `web/dist`, served at `/admin`. Screens, one per item on the original list:

- Avatars: gallery with image, prompt, seed, size; create with custom prompt; regenerate; rename; delete.
- Voices: list with playback of the sample; upload new; delete.
- Prompts: editor per prompt kind with diff against the default and a "test with last sources" action that runs only the script stage.
- Search profiles: form editor; "run now" button that enqueues a run.
- Schedules: cron editor bound to a profile, enable/disable, next and last run.
- Runs: list with status, stage, timings, links to video, audio, narration text, sources, report, and `run.log`; retry and cancel.
- Service status panel: results of `GET /health/deep` (see Preflight).

## Phase 3: Run queue and worker

### Runs table

`id`, `tenant_id`, `profile_id`, `status`, `stage`, `priority`, `queued_at`, `started_at`, `finished_at`, `error_stage`, `error_message`, `run_directory`, `preflight` (JSON of check results).

Status values: `queued`, `preflight`, `running`, `done`, `failed`, `cancelled`. Stage values: `preflight`, `search`, `script`, `audio`, `video`.

### Behavior

- Enqueue: `/generate` and the scheduler insert a row and return `run_id` immediately. `GET /runs/{id}` returns status, stage, and artifact links. A `wait=true` flag keeps the current blocking behavior for scripts.
- Worker: one loop in the app process, oldest queued first, ordered by priority then queue time. Concurrency is per resource, not per tenant: ComfyUI slot of 1, TTS slot of 1, LLM and search may overlap. In practice the worker runs search, script, and audio for run N+1 while run N is in ComfyUI.
- Failure: a failed stage marks the run failed with stage and message, which `PipelineError` already carries. No automatic retries on the video stage.
- Retry: explicit, `POST /runs/{id}/retry`, re-queues from the failed stage using artifacts already on disk.
- Cancel: `POST /runs/{id}/cancel` flips a queued run to cancelled; for a running video stage it calls ComfyUI's interrupt endpoint and the wait loop marks the run cancelled.
- Restart safety: on app start, runs left in `running` are marked failed with "interrupted by restart".
- Schedules: cron plus profile, driven by APScheduler inside the app. The scheduler skips firing when `GET /health/deep` reports a hard failure.
- Reference integrity for voices and avatars. The UI only offers voices and avatars that exist, so a manual or scheduled run normally cannot name a missing one; the enqueue-time check (unknown voice or avatar fails at enqueue, not in the worker) is the safety net. The gap is deletion: when a voice or avatar is deleted, every search profile and enabled schedule that references it must be found and the user warned that those runs will break, with the choice to reassign or proceed. `delete_voice` and `delete_avatar` return the list of affected profiles and schedules, the UI shows it before confirming, and a deleted reference on a schedule flips the schedule to a `broken` state that shows in the schedules screen and in the Slack notification instead of failing silently at the next fire.

## Preflight checks

Run once per dequeued job before any stage starts. Each check is a fast HTTP call with a short timeout and a distinct failure message. Results are stored on the run row and written to `run.log` as a `[preflight]` block.

| Service | Check | Also verifies |
|---|---|---|
| SearXNG | `GET /config` returns 200 | JSON format enabled; at least one engine in the configured categories |
| LLM | `GET /v1/models` returns 200 | configured `OPENAI_MODEL` is in the list |
| Reranker | `GET /v1/models` or `/health` | configured rerank model present; skipped when rerank disabled |
| TTS server | `GET /v1/models` lists `TTS_OPENAI_MODEL` | the profile's voice exists in `GET /v1/audio/voices` |
| ComfyUI video | `GET /system_stats` returns 200 | queue depth from `GET /queue`; template passes `validate_video_workflow`; anchor image exists |
| ComfyUI avatar | same, only for runs that generate an avatar | |
| Local | ffmpeg and ffprobe on PATH; run directory writable; free disk above threshold | |

- Any hard failure marks the run `failed` at stage `preflight` without touching the pipeline. The message names the service and the problem, for example `tts_openai: voice 'james-son-of-alphaus' not in library`.
- Reranker down is soft: the pipeline already degrades to lexical ranking, so preflight logs a warning and continues. Everything else is hard.
- `GET /health/deep` runs the same checks with no run attached, for the UI status panel and the scheduler.
- Results are cached for thirty seconds so a burst of scheduled runs does not hammer the hosts.
- The preflight function has no dependency on the queue or the database. It can be built first as a plain function called at the top of `generate_news_video`, then reused unchanged by the worker.

## Phase 4: UI auth

API keys cover machine access. Humans logging into `/admin` need sessions: one admin login per tenant stored in the database. OAuth only if a customer asks.

## MCP tool catalog (the contract)

This catalog is the canonical list of operations. Each entry is one typed function in `services/`. The MCP server registers it as a tool, FastAPI exposes it as a route, and the React UI calls the route. No consumer gets an operation that is not in this list, and no operation exists in only one consumer.

Conventions:

- Tenant is implicit. It comes from the caller's API key (HTTP `Authorization: Bearer`, or the `NEWS_API_KEY` environment variable for a stdio MCP client). No tool takes a tenant parameter. Admin-only tools are marked; the admin tenant is the one bootstrapped from `.env`.
- Ids are strings. Names (voices, avatars, prompt kinds) are tenant-unique slugs matching `[a-zA-Z0-9._-]+`.
- Every error has the shape `{ "error": { "code": string, "stage": string | null, "message": string } }`. Codes: `not_found`, `conflict`, `invalid`, `unauthorized`, `forbidden` (admin-only operation called by a non-admin tenant), `preflight_failed`, `pipeline_failed`, `service_unavailable`.
- Large binary artifacts are never returned inline. Tools return a URL plus size and content type. Small text artifacts (narration text, sources JSON, report, run log) may be returned inline on request.
- List tools take `limit` (default 50, max 200) and `cursor`, and return `items` plus `next_cursor`.
- Timestamps are ISO 8601 UTC.

### System

| Tool | Parameters | Result |
|---|---|---|
| `preflight` | `profile_id?` | `{ ok, checks: [{ service, status: "ok" \| "warn" \| "fail", detail, latency_ms }], checked_at }`. With `profile_id`, also verifies that profile's voice, avatar, and model. Cached 30 s. |
| `preview_search` | `search_text, categories?, language?, max_articles?, max_age_days?, rank_query?` | `{ raw_count, dated_count, candidate_count, selected: [{ title, url, outlet, published_at, score, snippet }], rank, effective }`. Search plus ranking only; no LLM, no run. |
| `get_search_options` | none | `{ categories: [string], defaults: { max_articles, max_age_days, categories, language, intended_video_max_seconds, wpm } }` |
| `get_system_info` | none | `{ version, video: { width, height, fps, workflow }, services: [{ name, configured: bool }], storage: { data_dir, free_bytes } }`. Never returns service URLs to non-admin tenants. |

### Voices

| Tool | Parameters | Result |
|---|---|---|
| `list_voices` | `limit?, cursor?` | items of `{ name, language, sample_url, created_at }` |
| `get_voice` | `name` | `{ name, language, sample_url, library_name, created_at }` |
| `upload_voice` | `name, sample (file), language = "en", replace = false` | the voice. `conflict` if it exists and `replace` is false. Uploads to the TTS library as `<tenant>-<name>`. |
| `delete_voice` | `name` | `{ deleted: name }`. Removes from the TTS library and the database. |

### Avatars

| Tool | Parameters | Result |
|---|---|---|
| `list_avatars` | `limit?, cursor?` | items of `{ name, image_url, width, height, source, created_at }` |
| `get_avatar` | `name` | `{ name, image_url, prompt, negative_prompt, seed, width, height, workflow, source, created_at }` |
| `generate_avatar` | `name, prompt, negative_prompt?, width?, height?, seed?, replace = false` | the avatar. Dimensions must be 16:9; defaults from `.env`. |
| `upload_avatar` | `name, image (file), replace = false` | the avatar with `source: "uploaded"` |
| `delete_avatar` | `name` | `{ deleted: name }` |

### Prompts

Prompt kinds: `summarize`, `video_visual`, `video_sound`. Prompts are stored per profile; the kinds here are the global defaults (files) that fill a new profile, edited by the admin tenant only.

| Tool | Parameters | Result |
|---|---|---|
| `list_prompts` | none | `[{ kind, body, path, editable, variables, updated_at }]` the global defaults |
| `get_prompt` | `kind` | `{ kind, body, path, editable, variables: [string], updated_at }` |
| `update_prompt` | `kind, body` | rewrites the default file; admin tenant only; existing profiles keep their own text |
| `test_prompt` | `kind = "summarize", run_id?, search_text?` | `{ script, word_count, target_words, sources_used }`. Runs only the script stage against an existing run's sources or a fresh search. Does not create a run. |

### Search profiles

Profile fields: `id, name, search_text, categories, language, safesearch, max_articles, max_age_days, rank_query, rank_candidates, voice, avatar, intended_video_max_seconds, wpm, created_at, updated_at`.

| Tool | Parameters | Result |
|---|---|---|
| `list_search_profiles` | `limit?, cursor?` | items of profiles |
| `get_search_profile` | `id` | the profile |
| `upsert_search_profile` | profile fields, `id?` | the profile. Omitted fields take tenant defaults, then `.env` defaults. |
| `delete_search_profile` | `id` | `{ deleted: id }`. `conflict` if a schedule references it. |

### Runs

Run fields: `id, profile_id, status, stage, priority, queued_at, started_at, finished_at, error_stage, error_message, preflight, artifacts: { video, audio, narration, sources, report, log } (each { url, size, content_type } or null), metrics: { word_count, target_words, audio_seconds, video_seconds, video_width, video_height }, selected_sources: [{ title, url, source, outlet, published_at, source_rank_score }]`.

| Tool | Parameters | Result |
|---|---|---|
| `enqueue_run` | `profile_id`, `overrides?: { search_text, script_text, max_articles, max_age_days, voice, avatar, intended_video_max_seconds, wpm, prompt_enhance }`, `priority = 0`, `wait = false` | the run. With `wait = true` blocks until `done` or `failed`. |
| `get_run` | `id` | the run |
| `list_runs` | `status?, profile_id?, limit?, cursor?` | items of runs without `selected_sources` |
| `cancel_run` | `id` | the run with status `cancelled`. Interrupts ComfyUI if the video stage is active. |
| `retry_run` | `id` | a new run re-queued from the failed stage, reusing artifacts on disk |
| `get_run_file` | `id, artifact: "video" \| "audio" \| "narration" \| "sources" \| "report" \| "log", inline = false` | `{ url, size, content_type, content? }`. `content` only for text artifacts with `inline = true`. |

### Schedules

| Tool | Parameters | Result |
|---|---|---|
| `list_schedules` | none | `[{ id, profile_id, cron, enabled, last_run_id, last_run_at, next_run_at }]` |
| `upsert_schedule` | `id?, profile_id, cron, enabled = true` | the schedule. `invalid` on a bad cron expression. |
| `delete_schedule` | `id` | `{ deleted: id }` |
| `run_schedule_now` | `id` | the enqueued run |

### Tenants (admin only)

| Tool | Parameters | Result |
|---|---|---|
| `list_tenants` | none | `[{ slug, name, active, created_at }]` |
| `create_tenant` | `slug, name` | `{ tenant, api_key }`. The key is returned once and stored hashed. Seeds default prompts. |
| `list_api_keys` | `tenant_slug` | `[{ id, prefix, label, created_at, revoked_at }]` |
| `create_api_key` | `tenant_slug, label?` | `{ api_key, id, prefix, label, created_at }`, key returned once |
| `revoke_api_key` | `tenant_slug, key_id` | `{ revoked: key_id }` |
| `set_tenant_active` | `tenant_slug, active` | the tenant. Inactive tenants cannot enqueue runs; their schedules pause. |

### Transport

- MCP server in `mcp_server/` on the official Python `mcp` SDK. Stdio transport for local agents; streamable HTTP transport, mounted on the FastAPI app, for remote agents. HTTP auth is the same bearer key as the REST routes.
- FastAPI routes mirror the catalog one to one under `/api/v1/`, for example `POST /api/v1/runs` for `enqueue_run` and `GET /api/v1/runs/{id}` for `get_run`. The existing routes (`/generate`, `/stages/*`, `/runs/{id}/*`, `/voices`, `/avatars*`) stay as thin aliases for the admin tenant until the React UI ships, then are removed.
- Tool and route input validation share one Pydantic model per operation. The MCP tool description is generated from that model's docstring and field descriptions, so the agent-facing text and the API docs cannot drift.

## Product context

The product helps companies with SEO and market placement in their niche. Every generated video is a piece of niche content published under the client's brand. That framing drives several requirements below: per-tenant brand and keyword context in the prompts, publishing integrations, SEO metadata per video, and usage tracking for billing. The service is operated as a managed SaaS by the team, not self-served by tenants, but every control and every metric is still tenant-scoped so that billing, auditing, and per-client reporting work from day one.

## Deployment: Caddy, TLS, Cloudflare

- Caddy in front of the FastAPI app as the reverse proxy, terminating TLS. Caddy obtains certificates automatically; with Cloudflare in front, use the Cloudflare DNS challenge so certificates issue without exposing port 80, and set Cloudflare to Full (strict) so the origin is verified.
- Cloudflare provides DNS, DDoS protection, and optionally Access for the `/admin` UI, so the management UI is only reachable by team members authenticated through Cloudflare before the app's own login is even reached.
- Caddy passes `X-Forwarded-For` and `X-Forwarded-Proto`; the app must read them for correct client IPs in audit logs and for absolute artifact URLs.
- Large uploads (voice samples, anchor images) and long-running `wait=true` requests need Caddy timeouts raised above the defaults, or better, the queue makes every request short.
- Compose gains a `caddy` service with a `Caddyfile`; the app stops publishing its port directly. Media artifacts are served through Caddy from the output directory with tenant checks enforced by the app, not by static file serving.

## Usage metering and billing

Every billable resource is recorded per tenant, per run, per stage, in a `usage_events` table: `id, tenant_id, run_id, stage, resource, quantity, unit, started_at, finished_at, meta`.

| Resource | Unit | Source |
|---|---|---|
| LLM tokens | prompt tokens, completion tokens | the `usage` block in the OpenAI-compatible response; model name in `meta` |
| Reranker calls | documents scored | count of documents per rerank request |
| TTS | seconds of audio generated, wall-clock seconds | narration duration and request timing |
| ComfyUI video | wall-clock seconds of GPU time, output seconds, pixels | queue-to-download timing, video duration, width x height |
| ComfyUI avatar | wall-clock seconds, images | same |
| SearXNG | requests | count |
| Storage | bytes stored | size of the run directory, sampled daily |

- `get_usage(tenant?, from, to, group_by)` joins the catalog as a tool and route: totals by resource, by run, by day. Admin can query any tenant; a tenant sees only itself.
- Quotas live on the tenant row: monthly runs, monthly GPU seconds, monthly tokens. Preflight refuses a run that would exceed a hard quota and warns on a soft one.
- Billing export: a monthly CSV or JSON per tenant from `usage_events`. Pricing is applied outside the app for now; the app records quantities, not money.

## Logging, audit, and aggregation

Three log streams, all structured (one JSON object per line) with `tenant_id`, `run_id` where applicable, and a correlation id per request:

1. Run log. `run.log` per run stays, now as JSON lines, and the same events go to the process logger. Every stage start and end, every external call with URL, status, latency, and the size of what came back, every selected source with its score, every validator and preflight result, and every error with the stage. Nothing about a run should require re-running it to understand.
2. Audit log. Every UI and API action that changes state is one line: who (tenant, API key id or user), what (tool name), on which object, with the before and after for edits, the client IP, and the correlation id. Stored in an `audit_events` table and emitted to the log stream. Reads are not audited; writes always are.
3. Application log. Startup, config loaded (values masked), worker loop, scheduler firings, preflight results, and anything unexpected.

Seq stub: a `logging` handler that ships JSON lines to Seq over its HTTP ingestion endpoint, enabled by `SEQ_URL` and `SEQ_API_KEY` in `.env`, no-op when unset. Until Seq is stood up, the same JSON lines go to stdout for Docker and to `logs/app.jsonl` on disk. The handler is the only Seq-specific code; everything else just logs structured events.

## Integrations: publishing and notifications

- YouTube autopublish. Per-tenant YouTube channel connected through OAuth, refresh token stored encrypted in the database. A `publish_targets` table holds the channel, category, default privacy, and a metadata template. On run completion, if the profile has autopublish on, the worker uploads the video with generated title, description, tags, and chapters, and records the video id and URL on the run. Publishing is its own stage with its own preflight (token valid, quota available) and its own retry, after the video stage.
  - Visibility policy. Uploads are `private` by default, `unlisted` if the target says so. A video becomes `public` only when the publish target has `public_allowed = true`, set ahead of time by the team, never by the run itself. Two release modes: immediate, which uploads with the target's default visibility and stops; and scheduled, which uploads as `private` with YouTube's `publishAt` set to `completion + N days` from the target's `schedule_days`, so YouTube flips it to public at that time without anyone logging in. A `review` option holds the run before upload until a team member approves in the UI.
  - Automation. Most runs will come from schedules with nobody watching, so the default path is upload private or unlisted, then notify. A Slack message with the run, the watch link, the current visibility, and the scheduled publish time (if any) tells the end user it is ready. Making it public early is a UI action on the run, audited like every other write.
- Slack notifications. Per-tenant or team-wide incoming webhook stored on a `notification_targets` table. Events: run queued, run done with artifact links and a thumbnail, run failed with stage and message, preflight failure, publish done with the YouTube URL, quota warnings. Message templates are editable prompts of a new kind. Slack slash commands or an app can come later; webhooks first.
  - Slack settings are managed per tenant in the UI. A Slack section on the tenant's settings screen holds: one or more targets, each with a webhook URL (stored encrypted, shown masked after save), a channel label, and a per-event toggle set (queued, done, failed, preflight failure, published, quota, schedule broken); a "send test message" button that posts a sample and shows the HTTP result; and the message templates for that tenant. Team-wide targets are configured by the admin tenant and shown read-only to other tenants. The same fields exist as catalog operations (`list_notification_targets`, `upsert_notification_target`, `test_notification`) so an agent or script can configure them without the UI. Every change is audited.
- Both integrations sit behind the same catalog pattern: `list_publish_targets`, `upsert_publish_target`, `publish_run`, `list_notification_targets`, `upsert_notification_target`, `test_notification`.

## vidIQ in the app

The vidIQ tools used during development run inside Claude's connector directory, not in this app. The app never calls Claude, so that path is not available at runtime. What vidIQ does offer programmatically:

- No conventional REST API on standard plans. Their programmatic surface is their MCP server at `https://mcp.vidiq.com/mcp`, which they state works with "any MCP-compatible client", not only Claude. MCP is a protocol; an application can be an MCP client.
- Authentication is OAuth 2.0 through the vidIQ account (`app.vidiq.com`, Account Settings, MCP). Some listings also describe a bearer API key. Either way it is a one-time interactive authorization producing a token the app stores encrypted and refreshes, the same pattern as the YouTube channel connection.
- Available on all plans during their launch period. Tool calls cost credits from the account's monthly pool, most at 5 credits, video and reel analysis at 10. That is a per-account cost to track against tenants in `usage_events` like any other resource.
- Tools that matter here: keyword research, title generation and scoring, thumbnail scoring, trending and outlier discovery, competitor and channel analytics.

Integration design: `services/vidiq.py` as an MCP client on the official Python `mcp` SDK, streamable HTTP transport to their endpoint, one connected vidIQ account per tenant or one team account shared across tenants (decide at setup; usage is attributed to the tenant either way). It is used at two points, both optional and off by default: in the metadata stage to score title candidates and pull keyword volume for the include terms, and in a periodic research job that feeds trending topics in the tenant's niche into search profiles. Every call is a `usage_events` row with the credit cost. If the launch-period access ends or the credit cost is too high, the fallbacks are the YouTube Data API for raw data, and paid keyword APIs such as DataForSEO or Ahrefs. The metadata stage must work with vidIQ absent; it only gets better with it present.

Sources: [vidIQ MCP help article](https://support.vidiq.com/en/articles/15082430-vidiq-mcp), [vidIQ MCP page](https://vidiq.com/mcp/).

## Other things to think about

- SEO metadata per video. A metadata stage after the script. Inputs, in this order of weight: the narration text, the search profile's search text and rank query, the titles of the articles actually used, and a per-tenant set of global include terms edited in the UI (brand name, niche keywords, standing hashtags). Outputs: title candidates, description with the outlets credited, tags, keywords, chapters, and a caption file (SRT from the narration timing, which Chatterbox can supply per chunk). Same LLM as the script, separate prompt of kind `metadata`, editable per tenant like the others. Optional scoring through vidIQ, below.
- Brand kit per tenant. Company name, niche, target keywords, tone, calls to action, disclaimers, logo. Injected into the summarize and metadata prompts as variables so every script speaks for the client, not generically. The global include terms above live here.
- Vertical output. Deferred, circle back later. The pipeline is 16:9 only. Shorts, Reels, and TikTok need 9:16, which means either a second render at 704x1280 with a vertical avatar or a crop-and-caption post-process. Not in scope for the phases above; the metadata stage may assume 16:9 for now.
- Branding overlays. Lower thirds, logo watermark, intro and outro, burned-in captions. An ffmpeg post-stage with a per-tenant template.
- Companion article. The script plus sources is already a short article. Emitting it as HTML or markdown for the client's own site is cheap and is the most direct SEO value in the pipeline.
- Topic memory. Track covered URLs and topics per tenant so daily runs don't repeat a story. Feed recent coverage into the source ranker as a negative signal.
- Human review. A `review` status and an approve/reject action in the UI, on by default for new tenants, off for trusted ones. Managed SaaS means a human is available; use that.
- Content safety. A lightweight check on the script before audio: no medical, legal, or financial advice, no unverifiable claims, no profanity. Same LLM, separate prompt, fail closed into review.
- Retention. Output directories grow by tens of megabytes per run. A per-tenant retention policy and a cleanup job, with usage recorded before deletion.
- Performance feedback. Once YouTube is connected, pull view and watch-time stats per published run and show which topics and profiles perform. This closes the loop for the SEO goal.
- Secrets. YouTube refresh tokens, Slack webhooks, and per-tenant keys need encryption at rest. One app-level key in `.env`, Fernet-encrypted columns.
- Backups. SQLite file plus the output directory. Nightly copy, and a restore drill once.
- Health metrics beyond logs. Queue depth, stage durations, and failure rates as Prometheus-style metrics on `/metrics`, so alerts don't depend on reading logs.
- Rights. Snippets are used as evidence with attribution, which is defensible, but the terms for each outlet and for the avatar likeness (generated or a real person) should be written down before the first external client.

## Segmented rendering (separate branch)

Status: planned, agreed 2026-09-14. Built on its own branch after the MCP service-layer work, separate from the queue and UI phases.

### Problem

LTX-2 samples a clip's entire latent at once, and attention cost grows with frame count. Nothing streams to disk during sampling. The workflow's decode is temporally tiled, but the full latent has already lived on the GPU by then, and decoded frames sit in system RAM until SaveVideo. A 60-second clip at 24 fps is 1440 frames, roughly 10 to 15 GB of float frames before encoding. Anything past about a minute risks out-of-memory on the DGX, and the failure is total: the whole render is lost.

The ComfyUI-LTXVideo extend and looping samplers chunk inside the workflow with latent overlap, which would keep motion continuous. Rejected for now: the audio latent would have to be chunked to match, the workflow becomes hard to validate, and one failed chunk still loses the run. Revisit only if segment cuts prove unacceptable.

### Rule: chunk the script into 100-word segments, render each, append with ffmpeg

Decided 2026-09-14 after a 94-second single render pushed the DGX Spark to 111.9 GB of 121.7 GB and had to be interrupted (first pass held at 90 GB; the upscale pass added 22 GB). Whatever the script stage produces, for however many articles, is split into chunks of at most `SEGMENT_MAX_WORDS` (default 100, about 40 seconds of narration) at sentence boundaries. Each chunk is narrated and rendered as its own clip, then the clips are appended with ffmpeg. The chunk size is the only length control that touches the GPU, so the LLM's word cap becomes a quality target rather than a safety mechanism, and a story that runs long simply becomes two chunks. Story boundaries still matter for title cards and chapters: chunks never span two stories, so a long story yields consecutive chunks under one card.

### Design: one segment per story, chunked to 100 words

- **Script stage** writes N segments instead of one script. One story per segment; each segment is then split into chunks of at most `SEGMENT_MAX_WORDS` (default 100) at sentence boundaries, so no single render exceeds about 40 seconds regardless of what the model wrote. The per-story word target derived from `SEGMENT_MAX_SECONDS` and the WPM remains a guideline for the writer, and each opening with a spoken bridge ("In other news", "Turning to the markets", "From DigiTimes today"). The bridge is what makes a sequence of stories sound like one show and is required regardless of the visual treatment. Output: `segments/NN/narration.txt` plus a `segments.json` manifest (order, headline, outlet, source URL, word count).
- **Audio stage** produces one narration per segment. Output: `segments/NN/narration.wav`.
- **Video stage** renders each segment sequentially through today's stage unchanged: same anchor image as the first frame every time (identity never drifts), same preflight, same template validation, same logging. Output: `segments/NN/video.mp4`. Memory is bounded by the longest segment, which is a size already proven to work. Retry re-renders only the failed segment.
- **Stitch stage** (new) assembles the show: optional branded intro, then for each segment a title card followed by the segment clip, then an optional sign-off outro. All ffmpeg; no new ComfyUI work. Output: `video.mp4` plus `chapters.json` with the start time of every segment for the YouTube description.

### Transitions

Every segment starts from the anchor pose, so a hard cut would snap the anchor back to neutral. Chosen treatment:

1. **Title card between segments**, 2 to 3 seconds: story headline, outlet name, tenant logo and colors from the brand kit, optional bed music. Rendered per segment from a per-tenant template with Pillow or ffmpeg `drawtext`. This is the broadcast convention, hides the cut entirely, puts the headline on screen for SEO and retention, and the card boundaries are the chapter markers.
2. **Crossfade on card edges**, about 300 ms video (`xfade`) and audio (`acrossfade`).
3. **Spoken bridge** at the start of each segment, from the script stage.

Rejected: chaining the last frame of one segment into the next (identity and color drift accumulate and one bad segment poisons the rest); a "segment introduction" in the visual prompt (the model cannot be relied on to perform a distinct gesture, and it costs control). Crossfade-only remains the fallback if a tenant turns cards off.

### Segment support image

Each story segment carries a supporting image, the way a broadcast puts a graphic beside the anchor.

**Layout and motion.** The LTX render stays what it is: a full-frame anchor clip per segment. The split layout is a post-process in the stitch stage, not a model behavior. For each segment: open on the full-frame anchor for the spoken bridge (about 2 seconds), then animate the anchor clip down to roughly 40 percent width anchored on the right third while the support image slides in on the left two-thirds with a slow Ken Burns push, hold that for the body of the story, and return to full-frame anchor for the last sentence so the segment ends on the presenter. All of it is ffmpeg `scale`, `overlay`, `zoompan`, and `xfade` on a 1280x704 canvas, with a lower-third headline and the outlet name. The title card between segments still handles the segment-to-segment cut; the return to full-frame is what makes the card feel like a natural break rather than a jump out of a split screen. Timing of the split-in and split-out comes from the narration: the metadata stage knows where the bridge sentence ends and the last sentence begins, so the cue points are computed, not guessed.

**Where the image comes from.** Three sources, in order of preference, all producing an image plus an attribution string:

1. **Generated illustration** through the avatar ComfyUI instance (Z-Image), from a prompt the LLM derives from the story: subject, setting, mood, in the tenant's visual style from the brand kit. No rights question at all. Rules: no real people, no logos, no text in the image, and a small on-screen label "Illustration" so it is never mistaken for a photo of the event. This is the default.
2. **Openly licensed photo** via the Openverse API (Creative Commons and public domain, over 800 million items from Wikimedia Commons, Flickr, museums; no key required for basic use, returns license, commercial-use flag, and a ready-made attribution line) or Wikimedia Commons directly. The stitch stage burns the attribution into the lower third for the segment. Filter to licenses that allow commercial use without share-alike (CC0, CC BY, public domain). Good for named entities: a company HQ, a product, a public figure at a public event.
3. **Screenshot of the source article's headline** captured with a headless browser (Playwright), cropped to masthead, headline, and byline, no photo. Used as commentary on the source itself, which is the strongest fair-use posture, and it doubles as visible attribution to the outlet. Per-tenant opt-in, off by default.

**What is not done.** The article's own photo (the `og:image` or any image in the page) is not reused. News photos are usually licensed from Getty, AP, Reuters, or a staff photographer who keeps the rights; commentary is a fair-use defense, not a right, it weighs weaker when the use is commercial and the whole photo is shown as illustration rather than discussed, and Getty and AP pursue claims. YouTube's Content ID does not fingerprint stills, but rights holders file manual copyright claims and strikes against them, and three strikes end a channel. For a managed SaaS publishing under client brands, that exposure is not worth one picture. This is a product policy, not legal advice; a lawyer should review the screenshot option before it ships.

**SearXNG.** The instance's `images` category already has `openverse`, `wikicommons.images`, `flickr`, `pexels`, and `unsplash` enabled (verified 2026-09-14), so `/search?format=json&categories=images&engines=openverse,wikicommons.images` returns openly licensed candidates through the same `/search?format=json` call the pipeline already uses. It does not return license metadata per result, so it is a discovery step only; the Openverse API call on the chosen candidate supplies the license and attribution. Engines like Bing Images or Google Images must not be used for this, because their results are unlicensed.

**Pipeline shape.** A new `support_image` step per segment inside the metadata stage: derive an image prompt and search terms from the segment, try the sources in the tenant's configured order, store the image under the run's `segments/NN/` folder with a `support_image.json` (source, license, attribution, prompt or URL), and fail soft to the generated illustration if a licensed search finds nothing. Usage is metered: a ComfyUI image job or an Openverse call per segment.

Sources: [fair use for screenshots (Penn State Libraries)](https://psu.libanswers.com/faq/333836), [Getty Images copyright rules](https://www.trademarkia.com/news/business/getty-images-free-to-use), [Openverse](https://openverse.org/about), [Openverse API](https://docs.openverse.org/api/reference/made_with_ov.html).

### Intro and outro

A 5-second branded intro and a sign-off outro, rendered once per tenant (a short LTX clip of the anchor greeting or signing off, or a static card with music) and reused on every run. Stored with the brand kit; regenerated only when the avatar or branding changes.

### Configuration

| Setting | Default | Meaning |
|---|---|---|
| `SEGMENT_MAX_WORDS` | 100 | hard chunk size per render, split at sentence boundaries; the only limit that bounds GPU memory |
| `SEGMENT_MAX_SECONDS` | 60 | per-segment length cap, drives the per-segment word cap; this is the only length limit, chosen to bound LTX-2 memory per render (renders past about a minute are where the DGX has run out of memory, so pull this back to 45 if a full-length segment fails) |
| `SEGMENT_TITLE_CARD_SECONDS` | 2.5 | 0 disables cards (crossfade only) |
| `SEGMENT_CROSSFADE_MS` | 300 | fade length at each card edge |
| brand kit | per tenant | logo, colors, font, bed music, intro and outro clips |

There is no total show length limit. The total is the sum of the segments plus cards and intro/outro, reported on the run, never enforced. A profile's `intended_video_max_seconds` becomes the per-segment cap (defaulting to `SEGMENT_MAX_SECONDS`), and `max_articles` is what governs overall length: more stories, longer show.

### Interaction with the queue and metering

A run has an ordered list of segments, each with its own status, so the worker can resume a run at the failed segment and the UI can show per-segment progress. ComfyUI GPU seconds are metered per segment and summed to the run. The stitch stage is CPU-only and runs on the app host.

### Build steps

1. Script stage: segment output and manifest, per-segment caps and bridges. Verify with the 27b model on three stories.
2. Audio stage: per-segment narration.
3. Video stage: loop over segments, reuse the existing stage per segment, per-segment logging.
4. Stitch stage: title cards, crossfades, concat, chapters.json.
5. Intro and outro per tenant.
6. Docs and a full run of a five-story show as the gate.

## Suggested build order

1. Preflight function and `GET /health/deep` (no schema needed, immediate value).
2. SQLite schema, tenant resolution, admin tenant bootstrap from `.env`, per-tenant storage paths.
3. CRUD routes for avatars, voices, prompts, search profiles.
4. Runs table, worker, non-blocking `/generate`, retry and cancel.
5. Schedules and APScheduler.
6. React admin UI, screen by screen in the order above.
7. UI auth.

## Open items

- Outlet name table in `services/prompting.py` needs more entries; `money.usnews.com` currently renders as "Usnews" instead of "U.S. News".
- Script length: the 27b model respects the word cap; the 4b model did not. Keep the cap as a hard rule in the prompt regardless.
