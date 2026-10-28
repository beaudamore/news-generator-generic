# News Generator Generic MCP

Generic news-video generation API for turning either search terms or a pre-made script into narrated, segmented ComfyUI talking-head video: SearXNG search, crawl4ai article extraction, an OpenAI-compatible LLM for the script, an OpenAI-compatible TTS server (Qwen3-TTS) for the narration, ComfyUI for per-segment hero images and the video, and ffmpeg to assemble it.

The FastAPI API is the source of truth for normal use. The CLI scripts are low-level helpers for testing individual stages.

For first-time setup and an admin verification sequence, see [docs/admin-setup.md](docs/admin-setup.md).

Shell examples in this README use POSIX paths (`.venv/bin/python`). On Windows, substitute `.venv\Scripts\python.exe` and `.venv\Scripts\uvicorn.exe`.

## Current Flow

1. `POST /generate` with multipart form data.
2. Provide an anchor image with `anchor_image_file`, or use a stored/generated avatar with `avatar_name`.
3. Provide a `voice` from the TTS server's library; optionally upload `voice_sample_file` to create or replace that named voice.
4. Provide either `search_text` or `script_text`.
5. If `search_text` is provided, the app searches SearXNG, optionally reranks evidence, and generates a script.
6. If `script_text` is provided, the app skips search/rerank/script generation and uses that script directly for audio.
7. The script is split into chunks (see [Segmented Rendering](#segmented-rendering)). Each chunk is narrated, rendered through ComfyUI, and, when the anchor image carries a green panel, gets a hero image composited into it (see [Hero Images](#hero-images)). The clips are appended with ffmpeg and the run artifacts are returned.

Minimum first request:

```bash
curl -X POST http://localhost:8000/generate \
  -F 'voice=reporter-voice' \
  -F 'voice_sample_file=@/path/to/voice-sample.wav' \
  -F 'anchor_image_file=@/path/to/anchor-image.png' \
  -F 'search_text=local ai privacy and safety advantages as of this week'
```

To skip search, rerank, and script generation, send `script_text` instead of `search_text`:

```bash
curl -X POST http://localhost:8000/generate \
  -F 'voice=reporter-voice' \
  -F 'voice_sample_file=@/path/to/voice-sample.wav' \
  -F 'anchor_image_file=@/path/to/anchor-image.png' \
  -F 'script_text=Tonight, we look at how local AI changes privacy and safety for everyday users.'
```

## Project Layout

```text
news-generator-generic/
├── README.md
├── Dockerfile                  multi-stage: Node builds web/dist, Python image adds ffmpeg + the app
├── docker-compose.yml          app + searxng (+ optional MCP-only instance)
├── requirements.txt
├── .env.example                the configuration template; copy to .env (gitignored) and fill in the keys
├── api/main.py                 FastAPI HTTP API: legacy /generate and /stages/*, /api/v1/*, /health, /admin, /mcp
├── mcp_server/                 MCP server (stdio via `python -m mcp_server`, streamable HTTP mounted at /mcp)
├── services/                   shared logic; api/, mcp_server/ and the worker all call into here
│   ├── pipeline.py             stages: search, script, images (heroes), audio, video; the legacy /generate flow
│   ├── segments.py             chunking, per-chunk render loop, green-panel hero compositing, ffmpeg assembly
│   ├── evidence.py             sentence evidence, reranking backends, story and evidence dedupe
│   ├── extract.py              crawl4ai article extraction
│   ├── llm.py                  LLM backend detection and context-window sizing
│   ├── tts.py                  the one TTS client (OpenAI-compatible /v1/audio/speech, Qwen3-TTS)
│   ├── avatars.py, tenant_avatars.py, tenant_voices.py
│   ├── prompts.py, prompting.py
│   ├── preflight.py            dependency checks before every run and for GET /health/deep
│   ├── comfyui_models.py       which model files a template needs, whether a ComfyUI host has them, and downloading the rest
│   ├── runs.py, worker.py, scheduler.py, schedules.py, profiles.py, search_preview.py
│   ├── tenancy.py, tenants.py, audit.py, db.py, models.py, migrations.py
│   └── logging_setup.py, system.py, errors.py
├── scripts/                    standalone CLI helpers for one stage at a time
│   ├── search_searxng.py
│   ├── generate_script.py
│   ├── generate_audio.py
│   ├── generate_video_comfyui.py
│   ├── ensure_comfyui_models.py  audit a ComfyUI host's model files against a template and download what is missing
│   └── init_db.py              create the database or apply migrations by hand
├── prompts/                    default prompt files: generic-news-reporter.md (summarize), video-anchor-scene.md,
│                               video-sound.md, hero-image.md
├── comfyui-helper-node/        ComfyUI custom node package adding model-download routes; install on every ComfyUI host
├── comfyui-workflows/
│   ├── models.json                            download URL, folder and checksum for every model file the templates name
│   ├── video_ltx2_3_ia2v-working.json         official LTX-2.3 IA2V template, unmodified apart from placeholders
│   ├── video_ltx2_3_ia2v-extend.json          working + LTXVContextWindows (long clips without transitions)
│   ├── video_ltx2_3_ia2v-extend-mac.json      extend with bf16 files for Apple Silicon
│   ├── video_ltx2_3_ia2v-extend-mac-q8.json   extend with Q8 GGUF loaders for Apple Silicon (docs/mac/)
│   ├── video_wan2_2_14B_s2v.json              Wan2.2 S2V alternative (parked, see Segmented Rendering)
│   └── avatar/                                image templates for avatars and heroes
│       ├── avatar_image_z_image_turbo_1080p.json
│       ├── image_z_image_turbo_int8.json
│       ├── image_qwen_image_2_1.json
│       └── image_qwen_Image_2512.json
├── web/                        React admin UI (Vite); built to web/dist and served at /admin
├── docs/
│   ├── admin-setup.md          first-time setup and verification sequence
│   ├── status.md               what is proven, what is built but unproven, current .env, next steps
│   ├── migrations.md           schema history and the production update procedure
│   ├── expansions.md           planned expansions
│   ├── ui-dev1.md              the operation catalog (MCP tool contract) and UI design
│   └── mac/README.md           LTX-2.3 on Apple Silicon findings
├── assets/avatars/<tenant>/    stored avatar PNGs + metadata (runtime-created, gitignored)
├── assets/voices/<tenant>/     uploaded voice samples (runtime-created, gitignored)
├── data/news.db                SQLite (runtime-created, gitignored)
├── logs/app.jsonl              JSON log (runtime-created, gitignored)
└── output/<tenant>/<run_id>/   run artifacts (runtime-created, gitignored)
```

## Environment

[.env.example](.env.example) is the configuration template and the authoritative list of keys. Copy it to `.env` (gitignored) and fill in the keys; `.env` is loaded once at process start by python-dotenv and by `docker compose` through `env_file`, so restart after editing. Its header shows the lines that differ between an Apple Silicon ComfyUI host and a CUDA host.

The keys fall into these groups (details in the sections linked):

| Group | Keys | See |
| --- | --- | --- |
| Avatar images | `AVATAR_API_KEY`, `AVATAR_COMFYUI_BASE_URL`, `AVATAR_COMFYUI_TIMEOUT_SECONDS`, `AVATAR_COMFYUI_WORKFLOW`, `AVATAR_DEFAULT_WIDTH`, `AVATAR_DEFAULT_HEIGHT`, `AVATAR_NEGATIVE_PROMPT`, `AVATAR_STORAGE_PATH` | [Avatar Workflow](#avatar-workflow) |
| TTS | `TTS_OPENAI_BASE_URL`, `TTS_OPENAI_MODEL`, `TTS_OPENAI_API_KEY`, `TTS_OPENAI_TIMEOUT_SECONDS` | [Generate Audio](#3-generate-audio) |
| Search | `SEARXNG_BASE_URL`, `SEARXNG_DEFAULT_*` | [Search Sources](#1-search-sources) |
| Ranking and evidence | `NEWS_SOURCE_RANK_*`, `NEWS_CONTEXT_*` | [Source Ranking](#source-ranking), [Sentence Evidence Context](#sentence-evidence-context) |
| Article extraction | `CRAWL4AI_API_URL`, `CRAWL4AI_API_TOKEN`, `ARTICLE_EXTRACTION_ENABLED`, `ARTICLE_EXTRACT_*` | [Article Extraction](#article-extraction-crawl4ai) |
| Script LLM | `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY`, `OPENAI_DEFAULT_TIMEOUT_SECONDS`, `OPENAI_REASONING_EFFORT`, `LLM_CONTEXT_*`, `LLM_RESERVE_TOKENS` | [Script LLM](#script-llm), [LLM Context Window](#llm-context-window) |
| Prompts and timing | `NEWS_SUMMARIZE_PROMPT[_FILE]`, `NEWS_VIDEO_PROMPT[_FILE]`, `NEWS_VIDEO_SOUND_PROMPT[_FILE]`, `NEWS_HERO_PROMPT[_FILE]`, `NEWS_DEFAULT_WPM`, `NEWS_INTENDED_VIDEO_MAX_SECONDS`, `NEWS_OUTPUT_DIRECTORY` | [Generate Script](#2-generate-script) |
| Video | `VIDEO_COMFYUI_BASE_URL`, `VIDEO_COMFYUI_WORKFLOW`, `VIDEO_COMFYUI_WIDTH`, `VIDEO_COMFYUI_HEIGHT`, `VIDEO_COMFYUI_FPS`, `VIDEO_COMFYUI_OUTPUT_PREFIX`, `VIDEO_COMFYUI_TIMEOUT_SECONDS`, `VIDEO_COMFYUI_PROMPT_ENHANCE`, `VIDEO_COMFYUI_FREE_BEFORE_RUN`, `VIDEO_LTX_TAIL_PAD_SECONDS`, `VIDEO_WAN_S2V_WIDTH`, `VIDEO_WAN_S2V_HEIGHT`, `VIDEO_WAN_S2V_FPS` | [Video Workflow](#video-workflow), [Output Size](#output-size) |
| Segments and heroes | `SEGMENT_ENABLED`, `SEGMENT_MAX_WORDS`, `SEGMENT_CONTINUE_FROM_LAST_FRAME`, `HERO_COMFYUI_WORKFLOW`, `HERO_COMPOSITE_MODE`, `HERO_COMPOSITE_BLEED_PX` | [Segmented Rendering](#segmented-rendering), [Hero Images](#hero-images) |
| Tenancy, storage, logging | `ADMIN_API_KEY`, `ADMIN_TENANT_NAME`, `ADMIN_TENANT_SLUG`, `DATA_DIRECTORY`, `SEQ_LOGGING_BASE_URL`, `SEQ_LOGGING_API_KEY` | [Tenancy and Database](#tenancy-and-database), [Logging and Seq](#logging-and-seq) |

Keys the code reads that are not in `.env.example` because their defaults are normally right:

```text
AVATAR_MAX_DIMENSION=1920              largest avatar side accepted on upload or generation
DATABASE_URL=                          overrides DATA_DIRECTORY/news.db entirely
LOG_DIR=logs
LOG_LEVEL=INFO
NEWS_API_KEY=                          the MCP stdio server's tenant key (falls back to the admin key)
NEWS_CONTEXT_DEDUPE_ENABLED=true       near-duplicate evidence merge; NEWS_CONTEXT_DEDUPE_THRESHOLD=0.6
NEWS_STORY_DEDUPE_ENABLED=true         same-story article dedupe; NEWS_STORY_DEDUPE_THRESHOLD=0.35
NEWS_CONTEXT_RERANK_BASE_URL=          only with NEWS_CONTEXT_RERANK_BACKEND=ollama-embed
NEWS_WORKER_ENABLED=true               false: this process only enqueues runs
NEWS_SCHEDULER_ENABLED=true            false: no scheduler thread
PREFLIGHT_TIMEOUT_SECONDS=5
VOICE_STORAGE_PATH=assets/voices
```

The standalone `scripts/generate_video_comfyui.py` reads its own `COMFYUI_BASE_URL`, `COMFYUI_DEFAULT_WORKFLOW`, `COMFYUI_DEFAULT_FPS`, `COMFYUI_DEFAULT_OUTPUT_PREFIX` and `COMFYUI_DEFAULT_TIMEOUT_SECONDS`; the pipeline does not use those.

`VIDEO_COMFYUI_BASE_URL` and `AVATAR_COMFYUI_BASE_URL` are intentionally separate. Video generation can target the heavier DGX ComfyUI instance, while avatar generation can target a lighter local ComfyUI instance that has the turbo image workflow available.

`NEWS_OUTPUT_DIRECTORY` may be absolute or project-relative. Relative values resolve from the repository root.

`NEWS_SUMMARIZE_PROMPT` or `NEWS_SUMMARIZE_PROMPT_FILE` is required unless overridden by a CLI prompt parameter or prompt file. There is no hardcoded prompt fallback in the script. The default repo prompt profile is `prompts/generic-news-reporter.md`. That profile attributes each story to its outlet by name, derived from the source domain, with varied phrasing ("According to DigiTimes, ...", "CNN is reporting ...", "From Yahoo Finance today, ..."), and never reads a URL aloud.

`NEWS_INTENDED_VIDEO_MAX_SECONDS` is appended to the summarize prompt as timing guidance. With `NEWS_DEFAULT_WPM`, the script computes the maximum spoken word count and tells the LLM to treat that word count as a hard cap.

`NEWS_SOURCE_RANK_ENABLED`, `NEWS_SOURCE_RANK_CANDIDATES`, and `NEWS_SOURCE_RANK_QUERY` control article-level ranking. When enabled, search keeps up to `NEWS_SOURCE_RANK_CANDIDATES` results that pass the age rule, scores each one's title and snippet against `NEWS_SOURCE_RANK_QUERY` through the `NEWS_CONTEXT_RERANK_*` endpoint, and keeps the top `max_articles` in score order. See [Source Ranking](#source-ranking).

`VIDEO_COMFYUI_PROMPT_ENHANCE` sets the default for the workflow's prompt-enhancement switch. It fills the `{{PROMPT_ENHANCE}}` placeholder, so the template must carry that placeholder on the boolean node that drives the switch; the API `prompt_enhance` field and the CLI `--prompt-enhance` / `--no-prompt-enhance` flags override it per run.

`VIDEO_COMFYUI_WIDTH` and `VIDEO_COMFYUI_HEIGHT` fix the final video size (code default 1280x720; `.env.example` sets 1280x704). 1280x704 is the workflow's native near-720p size; 1280x720 works but is produced by a re-encode. `AVATAR_DEFAULT_WIDTH` and `AVATAR_DEFAULT_HEIGHT` must also be 16:9; avatar generation rejects any other ratio. See [Output Size](#output-size).

## Service Topology

Every backend is reached over HTTP through its own base URL, so each one can live
on a different machine and be moved without code changes. Nothing in the pipeline
assumes two backends share a host, a GPU, or a network.

| Backend | Variable | Used by |
| --- | --- | --- |
| SearXNG | `SEARXNG_BASE_URL` | source search |
| Script LLM | `OPENAI_BASE_URL` | script generation |
| Reranker | `NEWS_CONTEXT_RERANK_ENDPOINT` | evidence ordering (optional) |
| TTS server | `TTS_OPENAI_BASE_URL` | narration audio, voice library |
| crawl4ai | `CRAWL4AI_API_URL` | article body extraction (optional) |
| Avatar ComfyUI | `AVATAR_COMFYUI_BASE_URL` | avatar images and per-segment hero images |
| Video ComfyUI | `VIDEO_COMFYUI_BASE_URL` | talking-head video |

Avatar and video ComfyUI are deliberately separate variables. Point them at one
server to share it, or at two servers to keep image generation off the machine
doing video, which is by far the largest consumer of memory.

Where backends do share hardware, `VIDEO_COMFYUI_FREE_BEFORE_RUN=true` asks the
video ComfyUI server to unload its models before a render. The code default is off,
because co-location is a deployment choice rather than something the pipeline
should assume; `.env.example` turns it on for a single-box setup.

Video memory scales with clip length: the workflow renders `duration * fps + 1`
frames and holds them through upscale and decode. Clip length is the main lever
when a render approaches the memory ceiling.

## Backend Choices

The app is intentionally endpoint-driven. The code does not require one vendor or runtime, but the selected runtime must expose the expected HTTP shape.

### Script LLM

The script generator calls an OpenAI-compatible chat completions endpoint configured by `OPENAI_BASE_URL` and `OPENAI_MODEL`.

`.env.example` points at a llama.cpp `llama-server` (`OPENAI_BASE_URL=http://<mac-host>:8180/v1`) serving a dense chat model. Any server with the OpenAI chat-completions shape works: llama.cpp, vLLM, SGLang, or Ollama. `services/llm.py` detects which one it is talking to and sizes the context accordingly (see [LLM Context Window](#llm-context-window)). Set `OPENAI_MODEL` to the served model name and `OPENAI_REASONING_EFFORT` (for example `none`) if the model takes it.

Do not point the script LLM at the BGE reranker model. The reranker is for scoring query/document pairs, not generating scripts.

### Evidence Reranker

The evidence reranker calls a rerank-shaped endpoint configured by `NEWS_CONTEXT_RERANK_ENDPOINT`, `NEWS_CONTEXT_RERANK_MODEL`, and `NEWS_CONTEXT_RERANK_ENABLED`.

The `NEWS_CONTEXT_RERANK_BACKEND` variable selects how ranking happens:

- `http` (default) posts to `NEWS_CONTEXT_RERANK_ENDPOINT`, which must speak the
  standard rerank shape. Use this for a true cross-encoder served by llama.cpp,
  TEI, Infinity, or vLLM.
- `lexical` needs nothing at all. It ranks in process with BM25 over the
  candidate evidence, using only the standard library. Weakest on paraphrase
  since it matches words rather than meaning, but deterministic, instant, and
  always available. This is also the automatic fallback: if a configured backend
  is unreachable, ranking degrades to `lexical` and the run records
  `degraded_from` plus the error, so evidence is never left in raw search order.
- `ollama-embed` needs no extra service. It embeds the query and each evidence
  unit through Ollama's `/api/embed`, ranks by cosine similarity, and normalizes
  scores to 0..1. Set `NEWS_CONTEXT_RERANK_MODEL` to an embedding model such as
  `nomic-embed-text:latest`, and `NEWS_CONTEXT_RERANK_BASE_URL` to the Ollama
  server (it falls back to `OPENAI_BASE_URL` with the `/v1` suffix stripped).

A bi-encoder ranks query and document separately, so `ollama-embed` is weaker
than a cross-encoder that scores the pair jointly. It is the right tradeoff when
avoiding another service matters more than the last increment of ranking quality.

Do not point either backend at a cross-encoder through Ollama. Ollama exposes no
route to a cross-encoder's classification head; with `bge-reranker-v2-m3` the
rerank routes 404 and both `/api/embed` and `/api/generate` terminate the
llama-server process outright.

Current verified true reranker choice is a vLLM worker serving `BAAI/bge-reranker-v2-m3`:

```text
NEWS_CONTEXT_RERANK_BACKEND=http
NEWS_CONTEXT_RERANK_ENDPOINT=http://reranker-host:8007/v1/rerank
NEWS_CONTEXT_RERANK_MODEL=bge-reranker-v2-m3
```

The served model id is whatever vLLM reports from `GET /v1/models`; the request and response shapes match the standard rerank shape below with no adapter. The same endpoint is used for article-level source ranking.

A llama.cpp alternative:

```text
NEWS_CONTEXT_RERANK_ENDPOINT=http://reranker-host:8033/v1/rerank
NEWS_CONTEXT_RERANK_MODEL=xitao/bge-reranker-v2-m3:latest
```

This endpoint is served by llama.cpp using the Ollama-downloaded GGUF and the `--embedding --pooling rank --reranking` flags. Ollama can pull and load the model file, but exposes no route to a cross-encoder's classification head, so it cannot rerank with it. Verified against Ollama 0.33.1: `/api/rerank`, `/v1/rerank`, and `/rerank` all return 404; `/api/embed` on the reranker returns `This server does not support embeddings`; `/api/generate` returns `the current context does not logits computation`. Native support is tracked upstream in ollama/ollama#3368, still open.

vLLM is also a supported reranker runtime. vLLM's scoring APIs expose `POST /rerank`, `POST /v1/rerank`, and `POST /v2/rerank` for scoring models, including cross-encoder rerankers such as `BAAI/bge-reranker-v2-m3`. For a vLLM reranker worker, serve the model with vLLM and point this app at that worker:

```text
NEWS_CONTEXT_RERANK_ENDPOINT=http://your-vllm-host:8000/v1/rerank
NEWS_CONTEXT_RERANK_MODEL=BAAI/bge-reranker-v2-m3
NEWS_CONTEXT_RERANK_ENABLED=true
```

vLLM can also serve the script LLM through its OpenAI-compatible chat/completions endpoints when the served model is a generation/chat model. Use a separate vLLM worker or separate served model for chat generation; do not reuse the reranker model as the script LLM.

Alternative: `openwebui-ollama-reranker-bridge` exposes the same `/v1/rerank` shape, but its default `embeddings` mode is embedding-similarity ranking through Ollama, not true BGE cross-encoder reranking. It works with embedding models such as `nomic-embed-text:latest`; with `xitao/bge-reranker-v2-m3:latest` it falls back to original order.

### Practical Combinations

```text
Simple local setup:
  Ollama for script LLM
  llama.cpp for true BGE rerank

Single llama.cpp stack:
  llama.cpp chat server for script LLM
  separate llama.cpp rerank server for BGE rerank

vLLM GPU workers:
  vLLM chat server for script LLM
  separate vLLM scoring server for BGE rerank

Embedding-similarity fallback:
  Ollama for script LLM
  openwebui-ollama-reranker-bridge with nomic-embed-text for approximate ranking
```

## Container Deployment

The stack is two containers plus whatever GPU services you point it at: the app (API, worker, scheduler, admin UI, MCP over HTTP, with ffmpeg and the built React bundle inside the image) and SearXNG. ComfyUI stays native on its GPU box; the TTS server, the reranker, crawl4ai, and the LLM run wherever they already run and are reached by URL. The image is a multi-stage build: Node builds `web/dist`, then a Python image installs ffmpeg and the app.

```bash
docker compose build
docker compose up -d                 # app on :8000 (admin UI at /admin, MCP at /mcp) + searxng on :8888
docker compose --profile mcp up -d   # also an MCP-only instance on :8001 with no worker or scheduler
```

Configuration is `.env`, passed through wholesale with `env_file`, so the container and a native run read the same file. Service URLs in `.env` must be reachable from inside the container: a service on the Docker host is `http://host.docker.internal:<port>`, anything on the LAN or VPN by its normal address. The compose file overrides only the paths that live in the container and `SEARXNG_BASE_URL`, which points at the bundled SearXNG. Persistent state: the SQLite database and logs in named volumes (`app-data`, `app-logs`), and `output/`, `assets/avatars/`, `assets/voices/` bind-mounted from the checkout so artifacts stay on the host.

The `mcp` profile runs a second container of the same image with `NEWS_WORKER_ENABLED=false` and `NEWS_SCHEDULER_ENABLED=false`, sharing the database and volumes, so agent traffic never shares a process with renders. Put Caddy in front of both when exposing them (see docs/ui-dev1.md).

A management UI for dependency settings is planned; today service URLs are `.env` only.

## ComfyUI Admin Setup

ComfyUI is intentionally not bundled into this Compose stack. Install ComfyUI on the machine that has the right GPU for your workflows: local workstation, DGX, NVIDIA server, cloud GPU, or another dedicated media node. Then point the app at it with:

```text
VIDEO_COMFYUI_BASE_URL=http://your-comfyui-host:8188
AVATAR_COMFYUI_BASE_URL=http://your-comfyui-host:8188
```

The app only sends API workflow JSON, uploads the input image/audio, waits for ComfyUI history, and downloads the output. The ComfyUI server must already have the custom nodes and models required by the selected workflow.

### Avatar Workflow

Workflow file:

```text
comfyui-workflows/avatar/image_z_image_turbo_int8.json
```

Configured by:

```text
AVATAR_COMFYUI_WORKFLOW=comfyui-workflows/avatar/image_z_image_turbo_int8.json
```

This is a ComfyUI API-format export of the Z-Image Turbo (int8) text-to-image graph with the pipeline placeholders substituted for the literal values: `{{POSITIVE_PROMPT}}` on the CLIP text encode, `{{WIDTH}}` and `{{HEIGHT}}` on the empty latent, `{{SEED}}` on the sampler, `avatars/{{AVATAR_NAME}}` as the save prefix, and `{{NEGATIVE_PROMPT}}` on a negative CLIP text encode wired to the sampler (filled from `AVATAR_NEGATIVE_PROMPT` or the request's `negative_prompt`). The graph runs 8 steps at CFG 1; at CFG 1 samplers skip the negative pass, so the negative prompt only takes effect if you raise `cfg` on the KSampler node in the template (try 1.5 to 2.5; turbo models degrade above that). Turbo models have low seed diversity: describe the person you want positively and specifically (age, hair, skin, eyes, clothing) rather than by negation, which the text encoder tends to invert. Every template in `comfyui-workflows/avatar/` is offered in the admin UI dropdown and accepted as the `workflow` field of the generate request; `AVATAR_COMFYUI_WORKFLOW` names the default. `image_qwen_Image_2512.json` (Qwen-Image 2512) carries the same placeholders and has a real negative prompt at CFG 4.

Required model files referenced by the workflow:

```text
qwen_3_4b_fp8_mixed.safetensors
ae.safetensors
z_image_turbo_int8_convrot.safetensors
```

Required node families/classes include:

```text
CLIPLoader
VAELoader
UNETLoader
ModelSamplingAuraFlow
KSampler
VAEDecode
```

Install the Z-Image Turbo workflow dependencies on the target ComfyUI instance and place the model files where those nodes expect them. The exact folder can vary by custom node pack, but common locations are ComfyUI `models/clip`, `models/vae`, and `models/unet`.

### Video Workflow

Workflow file:

```text
comfyui-workflows/video_ltx2_3_ia2v-working.json
```

Configured by:

```text
VIDEO_COMFYUI_WORKFLOW=comfyui-workflows/video_ltx2_3_ia2v-working.json
```

This file is the official ComfyUI **LTX-2.3: Image Audio to Video (IA2V)** template, exported in API format with the pipeline placeholders substituted for the literal values. Nothing else about it is modified. It is the proven baseline; the templates actually used today are derived from it:

| Template | Difference from `-working.json` | Use |
| --- | --- | --- |
| `video_ltx2_3_ia2v-extend.json` | adds one `LTXVContextWindows` node between the LoRA loader and both CFG guiders (145-frame windows, 40 overlap) | CUDA host; renders a whole chunk without the transitions and fades the baseline shows past about 10 s |
| `video_ltx2_3_ia2v-extend-mac.json` | extend + bf16 checkpoint and text encoder | Apple Silicon (MPS has no float8); too large to fit in 128 GB, see docs/mac/ |
| `video_ltx2_3_ia2v-extend-mac-q8.json` | extend + GGUF loaders (`UnetLoaderGGUF`, `DualCLIPLoaderGGUF`, `VAELoader`) for Q8 files | Apple Silicon, the recipe in docs/mac/ that produced a correct clip |
| `video_wan2_2_14B_s2v.json` | a different model family (Wan2.2 S2V, `WanSoundImageToVideo`) | parked: flat memory but 16 fps, looser lipsync, about 45 min per 31 s |

The pipeline picks the code path by the template's node classes (`workflow_family` in `scripts/generate_video_comfyui.py`): a `WanSoundImageToVideo` node selects the Wan stage, any `LTXV*` node the LTX stage. Keep the LTX copies in step when the template changes.

#### The narration WAV is the soundtrack

In the official IA2V template the uploaded WAV goes `LoadAudio -> TrimAudioDuration -> LTXVAudioVAEEncode -> SetLatentNoiseMask -> LTXVConcatAVLatent` into the first sampler pass. The audio latent is masked so it is never regenerated. The video's soundtrack is the narration, and the mouth is driven by it. This is what makes lip sync exact and makes the video end where the narration ends.

Do not use a template built from LTX's reference-audio variant for narrated video. That variant carries two nodes the official template does not have: `LTXVReferenceAudio`, which per the ComfyUI node documentation transfers only the speaker's voice identity to *generated* audio, and `LTXVEmptyLatentAudio`, which starts the first pass from a blank audio latent. With it the model speaks the `[SPEECH]` text of the prompt itself, at its own pace, in a voice cloned from the WAV. Symptoms: the video babbles when the script is not in the prompt, trailing sounds in the WAV do not appear in the video, and long scripts get cut off mid-sentence because the model talks slower than the TTS did. (Such a template was tried early on and has since been removed from the repository.)

The pipeline and the CLI both run `validate_video_workflow` on the template before every render. It refuses a template that is missing placeholders, contains `LTXVReferenceAudio` or `LTXVEmptyLatentAudio`, or whose `LTXVAudioVAEEncode` output does not reach a `LTXVConcatAVLatent.audio_latent` input. The error names the problem. Do not bypass it; fix the template.

#### Replacing the template

1. In ComfyUI, load the official IA2V template, adjust what you need, and run it once manually with a real image and WAV to confirm lip sync.
2. Export in **API** format (not the UI/graph export with `nodes` and `links`).
3. Substitute the placeholders listed under [Generate Video](#4-generate-video) for the literal values: `LoadImage.image`, `LoadAudio.audio`, `SaveVideo.filename_prefix`, both `RandomNoise.noise_seed`, the Width, Height, Duration, and Frame Rate primitives, the Prompt string, and the prompt-enhance boolean. Change values only; leave node ids, links, and layout alone.
4. Point `VIDEO_COMFYUI_WORKFLOW` at the file. The validator will confirm it on the next run.

Required model files referenced by the workflow:

```text
gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors
gemma_3_12B_it_fp4_mixed.safetensors
ltx-2.3-22b-dev-fp8.safetensors
ltx-2.3-spatial-upscaler-x2-1.1.safetensors
ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors
```

`comfyui-workflows/video_ltx2_3_ia2v-extend-mac.json` is the extend template for ComfyUI on Apple Silicon. MPS has no float8 type, so the fp8 checkpoint and the fp4 text encoder fail to load there; the Mac copy names the bf16 files instead (`ltx-2.3-22b-dev.safetensors` from Lightricks/LTX-2.3, 46 GB, and `gemma_3_12B_it.safetensors` from Comfy-Org/ltx-2, 24 GB). The two LoRAs and the upscaler are already bf16 and are shared. That combination loads but does not fit in 128 GB through the second pass; `video_ltx2_3_ia2v-extend-mac-q8.json` swaps the loaders for GGUF nodes and Q8 files and is the one that works. The measurements and the model files are in [docs/mac/README.md](docs/mac/README.md).

Required custom node families/classes include:

```text
ComfySwitchNode
LTXAVTextEncoderLoader
LTXVAudioVAEDecode
LTXVAudioVAEEncode
LTXVAudioVAELoader
LTXVConcatAVLatent
LTXVConditioning
LTXVCropGuides
LTXVImgToVideoInplace
LTXVLatentUpsampler
LTXVPreprocess
LTXVSeparateAVLatent
LatentUpscaleModelLoader
ResizeImageMaskNode
TextGenerateLTX2Prompt
TrimAudioDuration
```

Install the Lightricks LTX-2.3 / LTXV ComfyUI workflow dependencies on the target ComfyUI instance and place the checkpoint, text encoder, upscaler, and LoRA files where those nodes expect them. The folders are ComfyUI `models/checkpoints`, `models/text_encoders`, `models/latent_upscale_models` (the spatial upscaler; not `upscale_models`), and `models/loras`; `comfyui-workflows/models.json` records the exact folder and URL per file, and [ComfyUI Model Downloads](#comfyui-model-downloads) can fetch them for you.

### ComfyUI Validation Checklist

Before using this app against a ComfyUI server:

```text
1. Open the workflow in ComfyUI directly.
2. Confirm no nodes are red or missing.
3. Confirm every model dropdown resolves to an installed file.
4. Run the workflow once manually with test image/audio inputs.
5. Confirm ComfyUI can write outputs and the app can reach /prompt, /history/{prompt_id}, /upload/image, and /view.
```

Steps 2 and 3 are also done for you by the model audit below, and step 3's fix (download the files) can be automated.

### ComfyUI Model Downloads

The app can tell a ComfyUI host to download the model files a template names. ComfyUI's own HTTP API lists models (`GET /models/{folder}`) but has no public route to fetch one, so the repository ships two pieces:

- `comfyui-workflows/models.json`, the manifest: for every model filename the templates reference, its download URL, the `models/` subfolder it belongs in, and a sha256 where known. ComfyUI's API-format export drops the download metadata the UI export carries, which is why this lives beside the templates. `python scripts/ensure_comfyui_models.py --check-manifest` (and the test suite) fails when a template references a file the manifest does not cover.
- `comfyui-helper-node/`, a ComfyUI custom node package that registers no nodes and adds the routes `POST /news-generator/models/download`, `GET /news-generator/models/jobs`, and a few more (see its README). Copy or symlink `comfyui-helper-node/news_generator_helper` into `custom_nodes/` on every ComfyUI host and restart it. Downloads run on the host, one at a time, into the first configured path of the named folder (so `extra_model_paths.yaml` is honoured), resume after an interruption, and are sha256-verified when the manifest has a hash. Without the helper, the app falls back to ComfyUI-Manager's model queue when that is installed (`COMFYUI_MODEL_DOWNLOADER=auto`), but Manager's routes are undocumented and change between versions, so the helper is the supported path.

How a template is checked: `services/comfyui_models.py` reads every loader input in the API-format JSON (`ckpt_name`, `unet_name`, `clip_name`, `vae_name`, `lora_name`, `model_name`, ...), asks the host for that node class (`GET /object_info/{class}`), and compares the template's value with the choices the host offers. A value the host does not list is a missing file; a class the host does not know is a missing custom node pack (ComfyUI-GGUF for the Q8 Mac template, for example), which the audit reports but cannot install.

Three ways to run it:

```text
# CLI (reads .env; checks VIDEO_COMFYUI_WORKFLOW on the video host, AVATAR_/HERO_COMFYUI_WORKFLOW on the avatar host)
.venv/bin/python scripts/ensure_comfyui_models.py                          # audit only
.venv/bin/python scripts/ensure_comfyui_models.py --download --wait        # download what is missing, poll until done
.venv/bin/python scripts/ensure_comfyui_models.py --host video --workflow comfyui-workflows/video_ltx2_3_ia2v-extend.json --download

# REST (admin tenant)
GET  /api/v1/system/comfyui/models?host=video|avatar&workflow=...   audit
POST /api/v1/system/comfyui/models/ensure   json: {host?, workflow?, download: true}   202 with the jobs started
GET  /api/v1/system/comfyui/models/jobs?host=                       progress per job: status, bytes, speed, error
GET  /api/v1/system/comfyui/models/manifest                         manifest coverage

# MCP (admin tenant)
audit_comfyui_models(host?, workflow?), ensure_comfyui_models(host?, workflow?, download=true), get_comfyui_model_jobs(host?)
```

Preflight includes the audit as the `comfyui_video_models` check (and `comfyui_avatar_models` when the avatar host is preflighted): a run is refused at stage `preflight` when the template names a file the host lacks, with the filenames in the message. With `COMFYUI_AUTO_DOWNLOAD_MODELS=true`, preflight also starts the downloads on the host before refusing, so a scheduled run that fails on missing models fixes itself for the next fire; the check still fails until the files are in place, because a 30 GB checkpoint does not arrive within a preflight timeout.

```text
COMFYUI_AUTO_DOWNLOAD_MODELS=false   true: preflight queues missing manifest models for download
COMFYUI_MODEL_DOWNLOADER=auto        auto (helper, then Manager), helper, manager, or none
COMFYUI_MODELS_MANIFEST=comfyui-workflows/models.json
COMFYUI_HF_TOKEN=                    passed with each download request for gated Hugging Face repos
COMFYUI_CIVITAI_TOKEN=               same for Civitai URLs
```

Tokens can instead be set on the ComfyUI host (`HF_TOKEN`, `CIVITAI_API_TOKEN` in its environment); the request's token wins. The helper accepts URLs on `huggingface.co`, `hf.co` and `civitai.com` by default (`NEWS_GENERATOR_DOWNLOAD_ALLOWED_HOSTS` on the host widens that, for an internal mirror).

Adding a template: export it in API format, add the placeholders, then run `--check-manifest` and add a row to `models.json` for every uncovered filename. The folder names are ComfyUI's (`checkpoints`, `diffusion_models`, `text_encoders`, `vae`, `loras`, `latent_upscale_models`, `audio_encoders`); `diffusion_models` and `unet` are the same folder set. Note the LTX spatial upscaler goes in `latent_upscale_models`, not `upscale_models`.

## 1. Search Sources

Search accepts either `--subject` or the more generic `--search-text`.

```bash
.venv/bin/python scripts/search_searxng.py \
  --search-text "private ai risks and advancements" \
  --max-articles 5 \
  --max-age-days 3
```

Output:

```text
sources_path=/.../output/YYYY-MM-DD/private-ai-risks-and-advancements/sources.json
raw_result_count=...
candidate_count=...
source_rank_enabled=True query='artificial intelligence and technology news'
selected_count=...
1. [0.8721] <title> | <url>
```

The source bundle includes:

```json
{
  "subject": "private ai risks and advancements",
  "search_text": "private ai risks and advancements",
  "request_url": "...",
  "selected_sources": []
}
```

### SearXNG Categories

`SEARXNG_DEFAULT_CATEGORIES` is a comma-separated list passed directly to SearXNG's `categories` query parameter. The current default is:

```env
SEARXNG_DEFAULT_CATEGORIES=news,general
```

The available categories verified from the live local SearXNG `/config` endpoint are:

```text
general
videos
social media
images
music
packages
it
files
books
news
apps
software wikis
science
scientific publications
web
repos
other
currency
icons
weather
map
dictionaries
shopping
lyrics
cargo
movies
translate
radio
q&a
wikimedia
define
```

For news-video generation, `news,general` is the practical default: `news` keeps results current and `general` catches relevant pages that SearXNG does not classify as news.

### Age Rule

`max_age_days` (default `SEARXNG_DEFAULT_MAX_AGE_DAYS`) is applied client-side to each result's `publishedDate`. When it is greater than zero:

- results older than the limit are dropped;
- results with no publish date are dropped too. Undated hits are section hubs, index pages, or evergreen content, which cannot satisfy an age rule and would repeat across runs.

`time_range` is sent to SearXNG only when set explicitly (`--time-range` or `SEARXNG_DEFAULT_TIME_RANGE`). It is no longer derived from `max_age_days`, because SearXNG skips every engine without time-range support when `time_range` is present, which collapses results to a handful of engines that are frequently rate-limited or CAPTCHA-blocked.

### Source Ranking

With `NEWS_SOURCE_RANK_ENABLED=true`, search selects up to `NEWS_SOURCE_RANK_CANDIDATES` results that pass the age rule and de-duplication, sends each one's title and snippet to the `NEWS_CONTEXT_RERANK_ENDPOINT` cross-encoder with `NEWS_SOURCE_RANK_QUERY` as the query, and keeps the top `max_articles` in descending score order. The CLI accepts `--rank-query` and `--rank-candidates` overrides. The API has no per-request field; ranking is environment-controlled.

Runs from a profile rank by the profile's `rank_query`, or by its search terms when that is blank, and `rank_candidates` overrides the candidate count. The bundle records `candidate_count`, a `source_rank` block (endpoint, model, query, counts, and any degradation error), and a `source_rank_score` on each selected source. If the rerank endpoint fails, the run continues with the first `max_articles` candidates in search order and `source_rank.degraded` is `true`.

Selection also skips articles that cover the same story. `NEWS_STORY_DEDUPE_ENABLED` (default on) compares each ranked candidate with the ones already selected by cosine similarity on stopword-free term frequencies of title plus text; a pair above `NEWS_STORY_DEDUPE_THRESHOLD` (default 0.35) is the same story. The loop keeps walking the ranked pool until `max_articles` distinct stories are found. Same-story articles land in `duplicate_sources` with `duplicate_of`, the kept article records `also_reported_by`, their sentences still become evidence tagged with the story, and the summarize prompt is asked to tell the story once and credit the outlets together.

## 2. Generate Script

Generate the spoken news-anchor script from the source bundle:

```bash
.venv/bin/python scripts/generate_script.py \
  --sources output/YYYY-MM-DD/private-ai-risks-and-advancements/sources.json \
  --intended-video-max-seconds 30 \
  --wpm 145
```

Prompt resolution order:

1. `--summarize-prompt`
2. `--summarize-prompt-file`
3. `NEWS_SUMMARIZE_PROMPT`
4. `NEWS_SUMMARIZE_PROMPT_FILE`

If none are provided, generation fails.

Timing guidance is appended to the summarize prompt when `--intended-video-max-seconds` or `NEWS_INTENDED_VIDEO_MAX_SECONDS` is set. For example, 30 seconds at 145 WPM becomes a hard cap of 72 spoken words.

Prompt files may use template variables. The script stage replaces known variables before calling the LLM and leaves unknown variables unchanged.

Supported prompt variables:

```text
{{CATEGORY}}
{{CATEGORIES}}
{{CURRENT_DATE}}
{{CURRENT_DATETIME}}
{{CURRENT_TIME}}
{{CURRENT_TIMEZONE}}
{{CURRENT_WEEKDAY}}
{{INTENDED_VIDEO_MAX_SECONDS}}
{{LANGUAGE}}
{{MAX_AGE_DAYS}}
{{MAX_ARTICLES}}
{{TARGET_WORDS}}
{{RAW_RESULT_COUNT}}
{{SAFESEARCH}}
{{SEARCH_TEXT}}
{{SELECTED_SOURCE_COUNT}}
{{SOURCE_COUNT}}
{{SUBJECT}}
{{TIME_RANGE}}
{{WPM}}
```

Per-run override example:

```bash
.venv/bin/python scripts/generate_script.py \
  --sources output/YYYY-MM-DD/private-ai-risks-and-advancements/sources.json \
  --summarize-prompt "Write concise broadcast copy grounded only in the supplied sources. Output only the spoken script."
```

Outputs:

```text
script_path=/.../narration.txt
report_path=/.../report.md
mode=openai-compatible
word_count=...
intended_video_max_seconds=30
wpm=145
target_words=72
```

If `OPENAI_BASE_URL` or `OPENAI_MODEL` is missing, or the LLM call fails, the script currently falls back to a local source-title script builder. The summarize prompt is still required before that path is reached.

### Sentence Evidence Context

Enriched search context is sentence-first. Do not split source text by arbitrary character or token chunk boundaries for model evidence, because that can cut mid-sentence, mid-word, or mid-thought.

Current evidence flow:

```text
SearXNG selected URLs
-> selected source snippet text
-> sentence boundary detection
-> boilerplate and low-value sentence filtering
-> optional sentence or sentence-window scoring/reranking against search_text through a rerank-shaped endpoint
-> selected complete-sentence evidence units
-> script prompt context
```

Evidence units must be made only from complete sentences. If additional surrounding context is needed, build sentence windows from whole adjacent sentences, such as previous sentence + matched sentence + next sentence. Do not use overlap as a generic chunking mechanism.

Reranking should be vendor-neutral. The pipeline should care about the rerank API shape, not whether the server is Ollama, vLLM, TEI, OpenWebUI, or a custom service. The endpoint should accept a query plus complete-sentence evidence units and return ranked indexes or scores.

For a locally Ollama-downloaded BGE reranker GGUF, Ollama exposes the model but no working rerank endpoint (verified on 0.33.1). Serve the same GGUF through llama.cpp instead:

```bash
llama-server \
  -m <path-to-bge-reranker-gguf> \
  --host 127.0.0.1 \
  --port 8033 \
  --embedding \
  --pooling rank \
  --reranking
```

If the model was pulled with Ollama, the GGUF blob already exists on disk and can be passed to `llama-server` directly:

```text
macOS/Linux:  ~/.ollama/models/blobs/sha256-<digest>
Windows:      %USERPROFILE%\.ollama\models\blobs\sha256-<digest>
```

The blob for `xitao/bge-reranker-v2-m3:latest` is the ~1.2 GB file. On Windows, llama.cpp installs with `winget install ggml.llamacpp`.

Then set `NEWS_CONTEXT_RERANK_ENDPOINT=http://reranker-host:8033/v1/rerank` and enable `NEWS_CONTEXT_RERANK_ENABLED=true` when the server is running.

Expected rerank request shape:

```json
{
  "model": "xitao/bge-reranker-v2-m3:latest",
  "query": "local AI privacy and safety advantages as of this week",
  "documents": [
    "Complete sentence evidence unit one.",
    "Complete sentence evidence unit two."
  ],
  "top_n": 20
}
```

Expected rerank response shape:

```json
{
  "results": [
    {"index": 0, "score": 0.91},
    {"index": 1, "score": 0.84}
  ]
}
```

Sentence-based controls:

```text
NEWS_CONTEXT_MAX_SENTENCES
NEWS_CONTEXT_MIN_SENTENCE_CHARS
NEWS_CONTEXT_RERANK_ENABLED
NEWS_CONTEXT_RERANK_ENDPOINT
NEWS_CONTEXT_RERANK_MODEL
NEWS_CONTEXT_RERANK_TIMEOUT_SECONDS
NEWS_CONTEXT_RERANK_TOP_N
NEWS_CONTEXT_WINDOW_SENTENCES
```

`NEWS_CONTEXT_RERANK_TOP_N` is the default number of evidence units kept after reranking. API calls can override it with `rerank_top_n`, matching the existing pattern where `max_articles` overrides `SEARXNG_DEFAULT_MAX_ARTICLES` for a run.

The implementation protects common abbreviations before sentence segmentation, splits on sentence boundaries, restores abbreviation periods, then keeps only clean complete sentences. Evidence comes from the article body when [crawl4ai extraction](#article-extraction-crawl4ai) succeeds and from the SearXNG snippet otherwise; result titles are kept as metadata.

After reranking, near-duplicate evidence units are merged: with `NEWS_CONTEXT_DEDUPE_ENABLED` (default on) the reranker is asked for a pool of twice `top_n`, units whose word sets overlap above `NEWS_CONTEXT_DEDUPE_THRESHOLD` (Jaccard, default 0.6) collapse into one that carries `also_reported_by`, and the prompt renders that as "Also reported by" so the writer credits the outlets together instead of repeating the claim.

## 3. Generate Audio

Audio generation goes through `services/tts.py`, the one TTS client. The server is OpenAI-compatible: `/v1/audio/speech` for synthesis and `/v1/audio/voices` for the voice library (list, upload a sample with a consent id, delete), such as vLLM-Omni or mlx serving Qwen3-TTS. Configured by `TTS_OPENAI_BASE_URL` (ends in `/v1`), `TTS_OPENAI_MODEL`, `TTS_OPENAI_API_KEY`, `TTS_OPENAI_TIMEOUT_SECONDS`. Preflight, system info and logs name the service `tts_openai`. The Chatterbox provider was removed; a leftover `TTS_PROVIDER=chatterbox` in `.env` is refused with a message. After synthesis the pipeline trims a trailing burst: a short sound after a pause at the very end of a narration (a TTS artifact) is cut off with a stream copy.

The CLI requires an explicit `--voice`.

Existing library voice:

```bash
.venv/bin/python scripts/generate_audio.py \
  --text-file output/YYYY-MM-DD/private-ai-risks-and-advancements/narration.txt \
  --voice your-voice-name \
  --output output/YYYY-MM-DD/private-ai-risks-and-advancements/narration.wav
```

Voice rules:

1. `--voice` is required.
2. The script never implicitly uses the TTS server's default voice.
3. If no `--voice-sample` is provided, `--voice` must already exist in `GET /voices`.
4. If `--voice-sample` is provided and `--voice` exists, the script deletes that library voice and uploads the new sample under the same name.
5. If `--voice-sample` is provided and `--voice` does not exist, the script uploads the sample under that name.
6. After resolving or replacing the library voice, the script calls `POST /audio/speech` with the explicit `voice` field.

Replace or create a voice sample:

```bash
.venv/bin/python scripts/generate_audio.py \
  --text-file output/YYYY-MM-DD/private-ai-risks-and-advancements/narration.txt \
  --voice reporter-voice \
  --voice-sample /path/to/voice-sample.wav \
  --output output/YYYY-MM-DD/private-ai-risks-and-advancements/narration.wav
```

Outputs:

```text
audio_path=/.../narration.wav
voice=your-voice-name
voice_mode=library
duration_seconds=...
content_type=audio/wav
size_bytes=...
```

## 4. Generate Video

Video generation sends the image and narration WAV to ComfyUI, patches the workflow placeholders, queues the prompt, waits for history, and downloads the output media. The standalone script reads `COMFYUI_BASE_URL` and the `COMFYUI_DEFAULT_*` keys listed under [Environment](#environment), not the `VIDEO_COMFYUI_*` keys the pipeline uses.

```bash
.venv/bin/python scripts/generate_video_comfyui.py \
  --workflow comfyui-workflows/video_ltx2_3_ia2v-working.json \
  --image /path/to/anchor-image.png \
  --audio output/YYYY-MM-DD/private-ai-risks-and-advancements/narration.wav \
  --speech-text-file output/YYYY-MM-DD/private-ai-risks-and-advancements/narration.txt \
  --output output/YYYY-MM-DD/private-ai-risks-and-advancements/video.mp4 \
  --output-prefix news-generator/private-ai-risks-and-advancements
```

The script uploads both files through ComfyUI's upload endpoint and replaces these workflow placeholders:

```text
{{FIRST_FRAME_IMAGE}}
{{REFERENCE_AUDIO}}
{{PROMPT}}
{{WIDTH}}
{{HEIGHT}}
{{FPS}}
{{DURATION}}
{{SEED}}
{{OUTPUT_PREFIX}}
{{PROMPT_ENHANCE}}
```

The ComfyUI prompt is built as:

```text
[VISUAL]: <visual prompt>
[SPEECH]: <exact script text>
[SOUNDS]: <sound prompt>
```

If `--prompt` already contains `[SPEECH]:`, it is used as the complete workflow prompt.

The current duration is computed from `--speech-text-file` word count and `--wpm`; the generated WAV duration is also printed for comparison.

Outputs:

```text
prompt_id=...
image_name=...
audio_name=...
width=...
height=...
workflow_duration_seconds=...
audio_duration_seconds=...
video_path=/.../video.mp4
video_size_bytes=...
```

### Output Size

The pipeline (API and `services/pipeline.py`) always renders at `VIDEO_COMFYUI_WIDTH` x `VIDEO_COMFYUI_HEIGHT`, default 1280x720. LTX-2 works in 32-pixel blocks and this workflow renders the first pass at half size, so the native output is a multiple of 64 on each axis: 1280x704 is the native size closest to 720p and needs no re-encode, while 1280x720 is produced by re-encoding the 704 output. A non-16:9 setting is logged as a warning, not rejected. Before upload, the anchor image is center-cropped to 16:9 and resized to that size; the prepared copy is saved next to the original in the run directory as `<anchor>-<width>x<height>.png`. The workflow copies those dimensions into its latent size.

After download, the result is checked with `ffprobe`. LTX rounds latent dimensions to a multiple of 32, so ComfyUI can return, for example, 1280x704. If the size differs from the target, the ComfyUI file is kept as `video-raw.mp4` and `ffmpeg` re-encodes it to the exact target as `video.mp4` (H.264, CRF 18, AAC audio). `ffmpeg` and `ffprobe` must be on `PATH` for this guarantee; without them the size is logged as unverified.

The standalone `scripts/generate_video_comfyui.py` still copies the input image's dimensions unchanged.

### Run Log

Every pipeline run appends timestamped lines to `run.log` in the run directory, tagged by stage (`run`, `preflight`, `search`, `extract`, `evidence`, `script`, `images`, `audio`, `video`): the SearXNG request URL and counts, each selected source with its rank score, evidence rerank details, the LLM model and word counts, the resolved voice and narration duration, the ComfyUI prompt id, upload names, output file, final size, and any stage error. The same lines go to the `news_pipeline` Python logger, so they appear on the API server console.

## Full Example

```bash
.venv/bin/python scripts/search_searxng.py \
  --search-text "private ai risks and advancements" \
  --max-articles 5 \
  --max-age-days 3 \
  --output output/private-ai/sources.json

.venv/bin/python scripts/generate_script.py \
  --sources output/private-ai/sources.json \
  --output output/private-ai/narration.txt

.venv/bin/python scripts/generate_audio.py \
  --text-file output/private-ai/narration.txt \
  --voice reporter-voice \
  --voice-sample /path/to/voice-sample.wav \
  --output output/private-ai/narration.wav

.venv/bin/python scripts/generate_video_comfyui.py \
  --workflow comfyui-workflows/video_ltx2_3_ia2v-working.json \
  --image /path/to/anchor-image.png \
  --audio output/private-ai/narration.wav \
  --speech-text-file output/private-ai/narration.txt \
  --output output/private-ai/video.mp4 \
  --output-prefix news-generator/private-ai
```

## FastAPI API and MCP Direction

This project is API-first. The production integration surface is the FastAPI HTTP API because the main job is a service call: search, summarize, generate audio, generate video, and return downloadable artifacts.

MCP is a secondary agent surface. It should be a thin wrapper over the same shared pipeline code so an MCP client or agent can discover tools, list voices and avatars, and request a video from natural-language workflow context. MCP should not replace the HTTP API for app-to-app, webhook, batch, or upload/download integrations.

The intended implementation layers are:

```text
services/       shared pipeline, evidence, voices, and avatar logic
api/            FastAPI production HTTP API
mcp_server/     MCP tools that call the shared service layer
scripts/        CLI entrypoints and reusable low-level pipeline helpers
```

The CLI scripts remain reusable low-level pipeline helpers. FastAPI and MCP should not duplicate the generation logic.

## FastAPI API

The public API lives under `api/` and exposes the approved string/upload contract.

Run locally:

```bash
.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Primary endpoint:

```text
POST /generate
```

The request is `multipart/form-data`.

Required fields:

```text
voice: string
```

One script source is required:

```text
search_text: run SearXNG search and LLM summarization
script_text: use this script directly and skip search/summarization
```

One anchor image source is required:

```text
anchor_image_file: file upload
avatar_name: stored avatar name
avatar_name + avatar_prompt: generate or replace a stored avatar, then use it
```

If `anchor_image_file` is provided, it is used for that run. Otherwise, `avatar_name` must refer to a stored avatar. If `avatar_prompt` is supplied with `avatar_name`, the API generates or replaces the stored avatar first, then uses it as the run anchor image.

`/generate` runs synchronously in the request, acts on the admin tenant, and uses the same segmented narration and rendering as queued runs, but skips the hero-image stage. For queued, tenant-scoped runs with heroes use `POST /api/v1/runs` below.

Optional upload:

```text
voice_sample_file: file upload
```

Optional search fields:

```text
categories: comma-separated SearXNG categories; default SEARXNG_DEFAULT_CATEGORIES
language: default SEARXNG_DEFAULT_LANGUAGE
safesearch: default SEARXNG_DEFAULT_SAFESEARCH
max_articles: default SEARXNG_DEFAULT_MAX_ARTICLES
max_age_days: default SEARXNG_DEFAULT_MAX_AGE_DAYS
time_range: day | month | year | empty; default SEARXNG_DEFAULT_TIME_RANGE
search_timeout_seconds: default SEARXNG_DEFAULT_TIMEOUT_SECONDS
rerank_top_n: evidence rerank result count; default NEWS_CONTEXT_RERANK_TOP_N
```

Optional script fields:

```text
script_text: direct narration script; skips search and summarization
summarize_prompt: overrides NEWS_SUMMARIZE_PROMPT
intended_video_max_seconds: default NEWS_INTENDED_VIDEO_MAX_SECONDS
wpm: default NEWS_DEFAULT_WPM
```

Optional video fields:

```text
avatar_height: used only with avatar_prompt; default AVATAR_DEFAULT_HEIGHT; must be 16:9 with avatar_width
avatar_width: used only with avatar_prompt; default AVATAR_DEFAULT_WIDTH; must be 16:9 with avatar_height
replace_avatar: used only with avatar_prompt; default false
video_prompt: default NEWS_VIDEO_PROMPT or NEWS_VIDEO_PROMPT_FILE
video_sound_prompt: default NEWS_VIDEO_SOUND_PROMPT
prompt_enhance: default VIDEO_COMFYUI_PROMPT_ENHANCE; fills the {{PROMPT_ENHANCE}} placeholder
```

Internal/env-only values are not public API fields:

```text
SEARXNG_BASE_URL
OPENAI_BASE_URL
OPENAI_MODEL
OPENAI_API_KEY
OPENAI_DEFAULT_TIMEOUT_SECONDS
TTS_OPENAI_BASE_URL
TTS_OPENAI_MODEL
TTS_OPENAI_API_KEY
VIDEO_COMFYUI_BASE_URL
AVATAR_COMFYUI_BASE_URL
AVATAR_COMFYUI_WORKFLOW
AVATAR_COMFYUI_TIMEOUT_SECONDS
VIDEO_COMFYUI_FPS
VIDEO_COMFYUI_OUTPUT_PREFIX
VIDEO_COMFYUI_TIMEOUT_SECONDS
VIDEO_COMFYUI_WORKFLOW
local file paths
workflow JSON
```

Successful response:

```json
{
  "status": "success",
  "run_id": "string",
  "run_directory": "string",
  "video_url": "/runs/{run_id}/video",
  "audio_url": "/runs/{run_id}/audio",
  "script_url": "/runs/{run_id}/script",
  "report_url": "/runs/{run_id}/report",
  "sources_url": "/runs/{run_id}/sources",
  "script": "string",
  "word_count": 72,
  "target_words": 72,
  "audio_duration_seconds": 29.4,
  "video_duration_seconds": 29.2,
  "selected_sources": []
}
```

Error response:

```json
{
  "status": "error",
  "run_id": "string or null",
  "stage": "request | preflight | search | script | images | audio | video",
  "message": "string"
}
```

File download endpoints:

```text
GET /runs/{run_id}/video
GET /runs/{run_id}/audio
GET /runs/{run_id}/script
GET /runs/{run_id}/report
GET /runs/{run_id}/sources
```

Discovery endpoints:

```text
GET /voices
GET /avatars
GET /avatars/{avatar_name}
GET /avatars/{avatar_name}?width=1920&height=1080
POST /avatars/{avatar_name}/upload
POST /avatars/generate
```

`GET /voices` queries the TTS server and returns the currently available library voice names and aliases. It is a discovery endpoint only; it does not define or imply a default voice fallback.

Avatar endpoints store canonical PNG files under `AVATAR_STORAGE_PATH`. Relative paths resolve from the repository root. `POST /avatars/{avatar_name}/upload` accepts multipart field `file`, validates the image with Pillow, converts it to PNG, and writes `{avatar_name}.png` plus `{avatar_name}.json` metadata. If `AVATAR_API_KEY` is set, upload requires either `Authorization: Bearer <key>` or `X-API-Key: <key>`.

`POST /avatars/generate` accepts `avatar_name`, `avatar_prompt`, optional `replace_avatar`, and optional `width`/`height`. The default generation size is 1920x1080 for YouTube-friendly 1080p output. It loads `AVATAR_COMFYUI_WORKFLOW`, replaces the profile-image placeholders, queues ComfyUI, downloads the generated image, stores it as `{avatar_name}.png`, and writes metadata. If the avatar already exists, generation fails unless `replace_avatar=true`.

## Tenancy and Database

The app keeps tenant-scoped state in SQLite at `DATA_DIRECTORY/news.db` (default `data/`, gitignored). `DATABASE_URL` overrides the location entirely. The schema is created on startup and kept current by numbered migrations; see [Database Migrations](#database-migrations).

`.env` is system control: service endpoints, storage paths, output size, and the admin tenant. On every start the app ensures the admin tenant exists (`ADMIN_TENANT_SLUG`, `ADMIN_TENANT_NAME`) and that its bootstrap API key matches `ADMIN_API_KEY`, falling back to `AVATAR_API_KEY` so an existing installation keeps its key. Changing `ADMIN_API_KEY` revokes the previous bootstrap key on the next start. All other tenants, their keys, prompts, profiles, avatars, voices, schedules, and runs live only in the database.

Routes under `/api/v1/` require a key: `Authorization: Bearer <key>` or `X-API-Key: <key>`. Keys are stored hashed and shown once at creation. A missing, unknown, or revoked key returns 401 with the standard error shape. The pre-existing routes (`/generate`, `/stages/*`, `/runs/*`, `/voices`, `/avatars*`) are unchanged for now and act on the admin tenant.

```text
GET    /api/v1/me                       the calling tenant
GET    /api/v1/system                   version, video settings, configured services, storage (URLs for admin only)
GET    /api/v1/system/comfyui/models    admin only: model files each configured template needs and whether the ComfyUI host has them (host, workflow)
POST   /api/v1/system/comfyui/models/ensure   admin only: json host, workflow, download; starts downloads on the host for missing manifest models
GET    /api/v1/system/comfyui/models/jobs     admin only: download jobs each host reports
GET    /api/v1/system/comfyui/models/manifest admin only: manifest coverage of the templates

GET    /api/v1/voices                   list (limit, cursor)
POST   /api/v1/voices                   multipart: name, sample (wav), language, replace
GET    /api/v1/voices/{name}
GET    /api/v1/voices/{name}/sample     the stored sample wav
DELETE /api/v1/voices/{name}
GET    /api/v1/library-voices/{name}/sample   a short sample of any voice in the TTS server's library, synthesized per request, nothing stored

GET    /api/v1/avatars                  list (limit, cursor)
POST   /api/v1/avatars/generate         form: name, prompt, negative_prompt, width, height, seed, replace
POST   /api/v1/avatars/upload           multipart: name, image, replace
GET    /api/v1/avatars/options          image templates on disk, default template, default hero template, negative prompt, default size
GET    /api/v1/avatars/{name}
GET    /api/v1/avatars/{name}/image     the png
DELETE /api/v1/avatars/{name}

GET    /api/v1/prompts                  the global default prompts (summarize, video_visual, video_sound, hero_image) with body, path, updated_at
GET    /api/v1/prompts/{kind}           body, path, editable, variables
PUT    /api/v1/prompts/{kind}           form: body; rewrites the default file; admin tenant only
POST   /api/v1/prompts/{kind}/test      form: search_text, max_articles; kind must be summarize; runs search and script only, no run is queued

POST   /api/v1/runs                     json: profile_id, overrides, priority, wait; 202 with the queued run
GET    /api/v1/runs                     list newest first (status, profile_id, limit, cursor)
GET    /api/v1/runs/{id}                status, stage, timings, error, preflight, artifacts, metrics, selected_sources
POST   /api/v1/runs/{id}/cancel
DELETE /api/v1/runs/{id}              remove the run and its directory
POST   /api/v1/runs/{id}/retry          202 with a new run resuming from the failed stage
GET    /api/v1/runs/{id}/files/{artifact}       the file: video, audio, narration, sources, report, log, preflight, chunks
GET    /api/v1/runs/{id}/files/{artifact}/info  url, size, content_type; ?inline=true adds text content
GET    /api/v1/runs/{id}/chunks/{index}/hero/image   the hero png of one segment

GET    /api/v1/schedules                list
POST   /api/v1/schedules                json: profile_id, cron, enabled, name, timezone
GET    /api/v1/schedules/{id}
PUT    /api/v1/schedules/{id}            json: fields to change
DELETE /api/v1/schedules/{id}
POST   /api/v1/schedules/{id}/run        202 with the enqueued run

GET    /api/v1/tenants                  admin only: all tenants
POST   /api/v1/tenants                  admin only: json slug, name; returns the tenant and its first api_key (once)
GET    /api/v1/tenants/{slug}/keys      admin only
POST   /api/v1/tenants/{slug}/keys      admin only: json label; returns the raw key once
DELETE /api/v1/tenants/{slug}/keys/{id} admin only: revoke
PUT    /api/v1/tenants/{slug}/active    admin only: json active

GET    /api/v1/search/options           SearXNG categories and the .env defaults profile fields fall back to
POST   /api/v1/search/preview           json: search_text, categories, language, max_articles, max_age_days, rank_query, rank_candidates, extract; the articles a run would select, no LLM, no run; extract=true also fetches each through crawl4ai and reports words extracted

GET    /api/v1/profiles                 list (limit, cursor)
POST   /api/v1/profiles                 json: name, search_text, and any other profile field: categories, language, safesearch, max_articles, max_age_days,
                                        rank_query, rank_candidates, voice, avatar, intended_video_max_seconds, wpm, summarize_prompt, video_visual_prompt,
                                        video_sound_prompt, hero_prompt, hero_workflow (a new profile is filled from the default prompt files)
GET    /api/v1/profiles/{id}
PUT    /api/v1/profiles/{id}            json: fields to change
DELETE /api/v1/profiles/{id}            conflict if a schedule references it
```

Every error from these routes has one shape: `{"error": {"code", "stage", "message"}}` with codes `not_found` (404), `conflict` (409), `invalid` (400), `unauthorized` (401), `forbidden` (403, admin-only operation), `preflight_failed` (503), `pipeline_failed` (500), `service_unavailable` (503). Every write is recorded in `audit_events` with the acting key, the object, and its before and after state.

Runs and the worker: `POST /api/v1/runs` queues a run and returns immediately; a worker thread in the API process executes queued runs one at a time, oldest first by priority, through the stages `preflight`, `search`, `script`, `images` (one hero per segment), `audio`, and `video`, updating `status` and `stage` on the run as it goes. Runs use the profile's prompts (summarize, video visual, video sound, hero), the tenant's voice (falling back to a voice of the same name in the TTS server's shared library) and avatar (falling back to `assets/avatars/<name>.png`), and write under `output/<tenant>/<run_id>/`. The `overrides` object of `POST /api/v1/runs` accepts any profile field for that one run. `cancel` ends a queued run at once and asks a running one to stop between stages, interrupting ComfyUI if the video stage is active. `retry` queues a new run that resumes from the failed stage in the same directory. Runs left in progress by a restart are marked failed. `NEWS_WORKER_ENABLED=false` disables the worker (for a process that should only enqueue). The MCP server enqueues but does not execute; the API must be running.

Schedules: a five-field cron expression bound to a profile, evaluated in the schedule's timezone (default UTC). A scheduler thread in the API process checks every fifteen seconds for enabled schedules whose `next_run_at` has passed, runs the cached preflight once per pass, skips the fire with a recorded `last_error` when a hard check fails or the tenant is inactive, otherwise enqueues the profile exactly as `POST /api/v1/runs` would with actor `scheduler`, then advances `next_run_at`. `last_run_id` and `last_run_at` show what a schedule last produced. `NEWS_SCHEDULER_ENABLED=false` disables the thread.

Tenant storage: avatars under `assets/avatars/<tenant>/`, voice samples under `assets/voices/<tenant>/` (`VOICE_STORAGE_PATH` overrides the root), and TTS library names prefixed `<tenant>-`. Prompts are seeded per tenant from the repository files and `.env` values on first access; `default_body` always reflects the current file so a tenant can see and reset to it. Until the runs step, `/generate` and the other pre-existing routes still read the prompt files directly.

The MCP server resolves its tenant from `NEWS_API_KEY` in its environment, falling back to the admin bootstrap key, and exposes `get_system_info` alongside `preflight`.

## Database Migrations

The database is created on first start and kept current by numbered migrations in `services/migrations.py`; the applied versions are recorded in the `schema_version` table. `python scripts/init_db.py` creates a fresh database or applies pending migrations by hand, and `python scripts/init_db.py --status` shows the current and latest versions. Every change to `services/models.py` needs a new migration entry and a row in [docs/migrations.md](docs/migrations.md), which also holds the production update procedure.

## Admin UI

A React admin UI lives in `web/` (Vite, TypeScript, react-router, TanStack Query) and is served by the API at `/admin` when `web/dist` exists. It signs in with a tenant API key, verified against `GET /api/v1/me` and kept in the browser's local storage, and sends it as `X-API-Key` on every call. The key is passed as an `api_key` query parameter only for media the browser loads directly (video, audio, avatar images, voice samples); every other route ignores the parameter.

Build for the API to serve:

```bash
cd web
npm install
npm run build      # writes web/dist; the API picks it up on next start
```

Develop with hot reload (Vite on :5173 proxying `/api`, `/health`, `/mcp` and `/voices` to the API on :8020, so start uvicorn with `--port 8020` for dev):

```bash
cd web
npm run dev
```

Screens: dashboard (service checks, system, recent runs), runs (list, filters, run now from a profile), run detail (video, narration, a Segments panel with each chunk's hero image and status, script, sources, preflight, log, cancel, retry, delete), profiles (every profile field including the four prompts, the hero image template dropdown, and a search preview with an optional article-extraction test), schedules, avatars (template dropdown from `comfyui-workflows/avatar/`), voices (tenant voices, and the TTS server's shared library with a play button per voice), and for the admin tenant only prompts and tenants. `web/node_modules` and `web/dist` are gitignored; build on deploy.

## ffmpeg

The app shells out to `ffmpeg` and `ffprobe` for loudness normalization, appending chunk narrations and clips, verifying and correcting the final video size, and probing durations. The Docker image installs them; a native install needs them on `PATH`:

```powershell
# Windows
winget install Gyan.FFmpeg      # or: choco install ffmpeg
```

```bash
# macOS
brew install ffmpeg

# Debian / Ubuntu (including the DGX Spark)
sudo apt-get install -y ffmpeg
```

Verify with `ffmpeg -version` and `ffprobe -version` in a new shell. Preflight reports `local: warn` when either is missing; segmented rendering is on by default and cannot assemble a video without them, so treat the warning as a hard requirement.

## Segmented Rendering

Every script, whatever its length and however many articles it covers, is split at sentence boundaries into chunks of at most `SEGMENT_MAX_WORDS` (code default 100; `.env.example` sets 40, about 15 seconds of narration). Each chunk is narrated on its own (`chunks/NN/narration.wav`), rendered on its own through the video stage (`chunks/NN/video.mp4`), optionally composited with its hero image (`chunks/NN/composited.mp4`, see [Hero Images](#hero-images)), and the clips are appended with ffmpeg into the run's `video.mp4`; the narrations are appended into `narration.wav` the same way. `chunks.json` in the run directory is the manifest: text, word count, narration and clip durations, hero status and prompt, and status per chunk. It is served as the `chunks` artifact. `hero-placement.json` beside it records where the heroes are painted (see [Hero Images](#hero-images)).

This is the memory bound for the GPU: with the baseline LTX template a single 94-second render reached 112 GB on a 128 GB DGX Spark and had to be interrupted, 31 seconds peaked at 101 GB. With chunks, no render exceeds the chunk size no matter what the LLM wrote, so the word cap in the prompt is a quality target rather than a safety mechanism. Chunks whose clip already exists are reused, so a retry after a failure at chunk four renders only chunks four and up; cancel is honored between chunks. Transitions (title cards, crossfades) are not implemented; the clips are appended with a straight cut. `SEGMENT_ENABLED=false` restores single-clip rendering and disables hero images.

Knobs around the chunk loop:

- `SEGMENT_CONTINUE_FROM_LAST_FRAME` (code default false; `.env.example` sets true): on, each chunk's render starts from the last frame of the previous chunk, so the clips play as one continuous take at the cost of possible identity drift over a long run. Off, every chunk restarts from the anchor image and the presenter snaps back to the reference pose at each cut. The continuous path is a separate copy of the render loop (`segments.render_chunks_continuous`).
- `VIDEO_LTX_TAIL_PAD_SECONDS` (default 0.5): LTX fades the last frames of a clip, so each chunk is rendered this much past its narration and trimmed back to the narration length after download. `0` disables.
- The Wan2.2 S2V path: pointing `VIDEO_COMFYUI_WORKFLOW` at `comfyui-workflows/video_wan2_2_14B_s2v.json` selects `generate_video_stage_wan`, which renders 77-frame segments at `VIDEO_WAN_S2V_FPS` (16) and size `VIDEO_WAN_S2V_WIDTH` x `VIDEO_WAN_S2V_HEIGHT`, chains them to cover the narration, uses the plain visual prompt (no `[VISUAL]/[SPEECH]/[SOUNDS]` wrapper), and trims the output to the narration. Memory stays flat but the result is choppier and slower than LTX; it is parked.

## Hero Images

When the anchor image has a chroma-green panel (a studio monitor painted `#0EF50F`, at least a 5% by 5% patch of dense green), the `images` stage of a queued run generates one hero image per chunk and composites it into that panel. The LLM is given the profile's `hero_prompt` (default `prompts/hero-image.md`, prompt kind `hero_image`), the segment text and the run's source titles, and returns an image prompt; the image is rendered through the avatar ComfyUI server (`AVATAR_COMFYUI_BASE_URL`) with the template named by the profile's `hero_workflow`, else `HERO_COMFYUI_WORKFLOW`, else `AVATAR_COMFYUI_WORKFLOW`, at the panel's measured size. A chunk whose hero fails falls back to a headline card built from the matching source title. The stage is skipped, with a log line, when the anchor has no panel, segmenting is off, or there is no hero prompt. Hero images are reused on retry and shown on the run detail page.

Compositing (`HERO_COMPOSITE_MODE`):

- `fixed` (default): the panel is measured once per run and the hero is painted at that exact quad on every frame of every chunk, so it never moves or resizes. The quad comes from the green corners on the first frame of the first composited clip (the anchor as the model drew it), or from the anchor's panel box when no plausible quad is found there, and is recorded in `hero-placement.json` in the run directory. Every later chunk, and a retry, reuses the recorded quad. The hero fills the quad shape (no green key), grown by `HERO_COMPOSITE_BLEED_PX`, so anything the model draws in front of the panel is painted over, and a camera move leaves the hero where it was.
- `key`: a per-frame green key with tracking. The panel's corners are measured in every frame of the clip, the hero is perspective-warped onto them, and only the panel's window is keyed, so the hero follows the panel through pans, push-ins and off-frame corners.
- `locked`: fills the measured panel shape and holds it still when the camera does; when the measured corners jitter more than a few pixels it falls back to tracking with a dead zone, which is the small movement `fixed` was added to remove.

`HERO_COMPOSITE_BLEED_PX` (default 4) grows the hero past the measured edge in the fixed and locked modes so no green peeks out; too much paints over the bezel.

## Article Extraction (crawl4ai)

Search results only carry a one-or-two-sentence snippet per article. With `CRAWL4AI_API_URL` and `CRAWL4AI_API_TOKEN` set, the script stage fetches each selected article through a [crawl4ai](https://github.com/unclecode/crawl4ai) server (`POST /crawl` with a desktop browser user agent and the pruning content filter), cleans the markdown to prose, and uses the article body instead of the snippet as evidence. The original snippet is kept as `snippet`, the body is saved as `articles/NN.md` in the run directory, and each source records an `extraction` status: `extracted`, `skipped` (video and social domains in `ARTICLE_EXTRACT_SKIP_DOMAINS`), `blocked` (a consent or bot-wall page was returned), `short` (under `ARTICLE_EXTRACT_MIN_WORDS`), or `error` (crawl4ai failed; some sites refuse headless browsers outright). Every non-extracted source falls back to its snippet; extraction never fails a run. Bodies are cut at `ARTICLE_EXTRACT_MAX_WORDS`, fetched `ARTICLE_EXTRACT_CONCURRENCY` at a time, and the evidence sentence budget (`NEWS_CONTEXT_MAX_SENTENCES`) is shared evenly across sources so one long article cannot crowd out the rest. Preflight checks the server and token as a soft check. The search preview (`extract: true`, or the checkbox in the profile form) reports words extracted per article before a run.

```env
CRAWL4AI_API_URL=http://crawl4ai-host:11235
CRAWL4AI_API_TOKEN=
ARTICLE_EXTRACTION_ENABLED=true
ARTICLE_EXTRACT_FILTER=fit
ARTICLE_EXTRACT_TIMEOUT_SECONDS=45
ARTICLE_EXTRACT_MAX_WORDS=1500
ARTICLE_EXTRACT_MIN_WORDS=120
ARTICLE_EXTRACT_CONCURRENCY=3
ARTICLE_EXTRACT_SKIP_DOMAINS=youtube.com,youtu.be,facebook.com,instagram.com,tiktok.com,x.com,twitter.com,reddit.com,threads.net,pinterest.com
ARTICLE_EXTRACT_USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36
ARTICLE_EXTRACT_PRUNE_THRESHOLD=0.48
```

The crawl4ai container must have `CRAWL4AI_API_TOKEN` set (its JWT mode refuses every route otherwise), and its egress IP is what sites see; a VPN on that host changes it.

## LLM Context Window

The script stage refuses to let the server silently truncate its prompt. `services/llm.py` detects the backend behind `OPENAI_BASE_URL` (Ollama via `/api/version`, SGLang via `/get_server_info`, vLLM via `/version` or `max_model_len` on `/v1/models`, otherwise generic), reads the model's maximum context and the context the server will use, and logs both on every run and in preflight.

- Ollama: context is per request. The app sends `options.num_ctx` with every chat call. `LLM_CONTEXT_SIZE=auto` requests `min(model max, LLM_CONTEXT_CAP)`; a number pins it. After the call the app reads `/api/ps` and logs the context the model is actually loaded with; if the server's own `OLLAMA_CONTEXT_LENGTH` is larger, that wins and is reported.
- vLLM and SGLang: context is fixed at launch (`max_model_len`, `context_len`). The app only reads and reports it.
- Generic (anything else, such as llama.cpp): the app cannot read the context. A numeric `LLM_CONTEXT_SIZE` is used as the effective context; `auto` falls back to `LLM_CONTEXT_FALLBACK`, which is 8k and will drop most of the evidence, so pin it (llama.cpp on the Mac Studio runs 256k: `LLM_CONTEXT_SIZE=131072`).
- Any backend: before the call the prompt is estimated at about 3.5 characters per token against `effective - LLM_RESERVE_TOKENS`, and the lowest-ranked evidence sentences are dropped until it fits. The run log records the budget, the estimate, and how many were dropped.

```env
LLM_CONTEXT_SIZE=auto
LLM_CONTEXT_CAP=131072
LLM_RESERVE_TOKENS=2048
LLM_CONTEXT_FALLBACK=8192
```

## Logging and Seq

All application logging goes through the `news_pipeline` logger with three outputs: the human-readable console line, JSON lines at `LOG_DIR/app.jsonl` (default `logs/`, gitignored, rotating at 10 MB x 5), and Seq. Every record carries whatever context is set: `tenant_id`, `tenant_slug`, `run_id`, `stage`, `correlation_id`, `client_ip`, plus an `event` tag (`http`, `audit`, `run`).

- Requests: a middleware assigns a correlation id (incoming `X-Request-ID` is honored, otherwise generated), records the client IP (first `X-Forwarded-For` entry behind Caddy), returns `X-Request-ID` on the response, and logs one `http` line per request with method, path, status, and duration. `/health` is excluded from access logs.
- Audit: every state-changing action is an `audit_events` row and an `audit` log line with actor, action, object, correlation id, and IP.
- Runs: `run.log` in the run directory stays human-readable; the same lines are emitted as `run` events with `run_id` and `stage` fields. The worker binds `run_id` and tenant to every line it logs while a run executes.

Seq is enabled by `SEQ_LOGGING_BASE_URL` and `SEQ_LOGGING_API_KEY`; when either is unset the shipper is a no-op and nothing else changes. Events are batched (100 events or 2 seconds) and posted from a background thread to `/api/events/raw?clef` in CLEF format, so a slow or down Seq never blocks a request; failures are counted and reported in `GET /api/v1/system` under `logging.seq`. In Seq, filter on `app = 'news-generator'`, then by `tenant_slug`, `run_id`, or `correlation_id`. `LOG_LEVEL` (default INFO) and `LOG_DIR` are the only other knobs.

## Preflight and Deep Health

Every run begins with a preflight in `services/preflight.py` before any stage starts. It checks, in parallel with five-second timeouts: SearXNG (reachable, JSON format enabled, engines exist for the configured categories), the LLM (`OPENAI_MODEL` is served, context window read), the reranker (soft: a failure only degrades ranking to lexical), the TTS server (reachable, and the run's voice exists in its library), crawl4ai (soft; reports ok when extraction is disabled, warn when the server or token is wrong), ComfyUI video (reachable, queue depth, `VIDEO_COMFYUI_WORKFLOW` passes `validate_video_workflow`, the anchor image exists), ComfyUI video models (every model file the template names is offered by the host's loader nodes; see [ComfyUI Model Downloads](#comfyui-model-downloads)), ComfyUI avatar (a check exists behind `need_avatar`, but no caller sets it today, so the avatar server is not preflighted), and local prerequisites (ffmpeg and ffprobe on PATH, output directory writable, free disk).

Each check is logged as a `[preflight]` line in `run.log` and the full result is written to `preflight.json` in the run directory. Any hard failure ends the run at stage `preflight` with a message naming every failed service, and nothing is generated. Results are cached for thirty seconds. `PREFLIGHT_TIMEOUT_SECONDS` overrides the per-check timeout.

The same checks are served without a run:

```text
GET /health/deep
GET /health/deep?voice=<voice>&avatar_name=<avatar>
```

Returns 200 when every hard check passes and 503 otherwise, with the same JSON body either way. Use it as a service status panel and before scheduling runs.

## Tests

`tests/` holds unit tests for the ComfyUI model audit (`services/comfyui_models.py`, with HTTP faked) and the helper node's download engine (against a local HTTP server: resume, sha256, gated URLs, path safety). They need only `pytest` and `requests`, no running services:

```text
pip install pytest
python -m pytest tests/ -q
```

One test fails when a template references a model file that `comfyui-workflows/models.json` does not cover.

## Incremental Test Gates

The pipeline should be validated in small production slices before full video generation:

```text
1. request accepted: FastAPI imports, health endpoint, route registration
2. voices listed: GET /voices reaches the TTS server and returns available voice names
3. avatar generated or reused: the avatar endpoints create and retrieve a stored image
4. search made: search stage writes sources.json from SearXNG (and articles/NN.md when crawl4ai extracts)
5. summary created: script stage writes narration.txt and report.md within the word cap
6. audio created: the TTS server writes narration.wav with the explicit named voice
7. video created: ComfyUI writes video.mp4 from the selected avatar/image and narration WAV
```

Stage endpoints expose those slices directly:

```text
POST /stages/script
POST /stages/audio
POST /stages/video
```

`POST /stages/script` accepts the same script-source fields as `/generate`:

```text
search_text: run SearXNG search and LLM summarization
script_text: use this script directly and skip search/summarization
```

It creates a run directory and returns `run_id`, `script_url`, `report_url`, `sources_url`, `script`, `word_count`, `target_words`, and `selected_sources`.

`POST /stages/audio` accepts:

```text
voice: required TTS library voice name
script_text: optional direct script
search_text: optional; runs search and script first, then audio
run_id: optional existing run with narration.txt
voice_sample_file: optional WAV upload
```

It writes `narration.wav` and returns `audio_url`, `audio_duration_seconds`, `audio_size_bytes`, `voice`, and `voice_mode`. Provide `script_text`, `search_text`, or a `run_id` that already has `narration.txt`.

`POST /stages/video` accepts:

```text
run_id: optional existing run with narration.txt and narration.wav
script_text: optional direct script
audio_file: optional WAV upload
anchor_image_file: optional image upload
avatar_name: optional stored avatar name
avatar_prompt: optional prompt to generate or replace avatar_name before rendering
replace_avatar: used only with avatar_prompt; default false
avatar_width: used only with avatar_prompt; default AVATAR_DEFAULT_WIDTH; must be 16:9 with avatar_height
avatar_height: used only with avatar_prompt; default AVATAR_DEFAULT_HEIGHT; must be 16:9 with avatar_width
video_prompt: default NEWS_VIDEO_PROMPT or NEWS_VIDEO_PROMPT_FILE
video_sound_prompt: default NEWS_VIDEO_SOUND_PROMPT
prompt_enhance: default VIDEO_COMFYUI_PROMPT_ENHANCE; fills the {{PROMPT_ENHANCE}} placeholder
```

It writes `video.mp4` and returns `video_url`, `video_duration_seconds`, and `video_size_bytes`. Provide either `audio_file` or a `run_id` that already has `narration.wav`. Provide either `script_text` or a `run_id` that already has `narration.txt`. Provide either `anchor_image_file` or `avatar_name`.

## MCP Server

`mcp_server/` is the agent-facing wrapper over the same service layer FastAPI uses. It contains no pipeline logic; each tool calls one function in `services/`. The full operation catalog, which FastAPI routes and the management UI follow as well, is in `docs/ui-dev1.md` under "MCP tool catalog (the contract)".

Two transports, same tools:

- **stdio**, for a local agent: `python -m mcp_server`. Client configuration (Claude Desktop, Cursor, or any MCP client) points at that command with the repository as the working directory; the server reads `.env` from there and resolves its tenant from `NEWS_API_KEY` in its environment.
- **streamable HTTP**, for remote agents: mounted on the FastAPI app at `/mcp`, stateless with JSON responses. Every request needs the same bearer key the REST routes use (`Authorization: Bearer <key>` or `X-API-Key`); the tenant is the key's tenant. A missing or bad key returns 401 in the catalog error shape before the MCP layer is reached.

Tool bodies run in a worker thread, so a long tool call (avatar generation, a prompt test) never stalls the API.

Implemented tools:

```text
preflight(voice?, avatar_name?)   verify every dependency; same result as GET /health/deep
get_system_info()                 version, video settings, configured services, storage
audit_comfyui_models, ensure_comfyui_models, get_comfyui_model_jobs   (admin only) model files per template on each ComfyUI host
list_voices, get_voice, upload_voice(name, sample_path), delete_voice
list_avatars, get_avatar, generate_avatar, upload_avatar(name, image_path), delete_avatar
list_prompts, get_prompt, update_prompt (admin), test_prompt(kind=summarize, search_text, max_articles)
list_search_profiles, get_search_profile, upsert_search_profile, delete_search_profile
enqueue_run, get_run, list_runs, cancel_run, retry_run, get_run_file
list_schedules, upsert_schedule, delete_schedule, run_schedule_now
preview_search, get_search_options
list_tenants, create_tenant, list_api_keys, create_api_key, revoke_api_key, set_tenant_active   (admin only)
```

File-taking tools accept a path on the server host, since stdio clients run locally. Errors carry the catalog error shape serialized in the tool error message. Tool results return structured run metadata and artifact links, never embedded media. `upsert_search_profile` does not yet take the per-profile prompt or hero fields; set those through the REST route or the admin UI.

## Notes

- The pipeline should remain generic; avoid hardcoding topics, prompts, voices, or assets in scripts.
- Run artifacts live on the filesystem under `output/`; the SQLite database holds tenants, keys, profiles, schedules, run records and audit events, never media.
- YouTube publishing is not part of the current implemented flow.

## License

MIT. See [LICENSE](LICENSE).
