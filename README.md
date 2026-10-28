# News Generator Generic MCP

Generic news-video generation API for turning either search terms or a pre-made script into Chatterbox narration and a ComfyUI talking-head video.

The FastAPI API is the source of truth for normal use. The CLI scripts are low-level helpers for testing individual stages.

For first-time setup and an admin verification sequence, see [docs/admin-setup.md](docs/admin-setup.md).

Shell examples in this README use POSIX paths (`.venv/bin/python`). On Windows, substitute `.venv\Scripts\python.exe` and `.venv\Scripts\uvicorn.exe`.

## Current Flow

1. `POST /generate` with multipart form data.
2. Provide an anchor image with `anchor_image_file`, or use a stored/generated avatar with `avatar_name`.
3. Provide a Chatterbox `voice`; optionally upload `voice_sample_file` to create or replace that named voice.
4. Provide either `search_text` or `script_text`.
5. If `search_text` is provided, the app searches SearXNG, optionally reranks evidence, and generates a script.
6. If `script_text` is provided, the app skips search/rerank/script generation and uses that script directly for audio.
7. The app generates narration audio, sends the image/audio/script to ComfyUI, and returns downloadable run artifacts.

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
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env                  local only, gitignored; copy from the Environment section
├── api/                  FastAPI HTTP API (api/main.py)
├── assets/
│   └── avatars/          stored avatar PNGs + metadata (runtime-created)
├── comfyui-workflows/
│   ├── avatar_image_z_image_turbo_1080p.json
│   └── video_ltx2_3_ia2v-working.json
├── docs/                 admin setup and testing guide
├── output/               run artifacts (runtime-created)
├── prompts/
├── services/             shared pipeline, evidence, voices, avatars
└── scripts/
    ├── search_searxng.py
    ├── generate_script.py
    ├── generate_audio.py
    └── generate_video_comfyui.py
```

## Environment

The current `.env` surface is:

```env
ADMIN_API_KEY=
ADMIN_TENANT_NAME=Admin
ADMIN_TENANT_SLUG=admin
AVATAR_API_KEY=
AVATAR_COMFYUI_BASE_URL=http://avatar-comfyui-host:8188
AVATAR_COMFYUI_TIMEOUT_SECONDS=1800
AVATAR_COMFYUI_WORKFLOW=comfyui-workflows/avatar/image_z_image_turbo_int8.json
AVATAR_DEFAULT_HEIGHT=720
AVATAR_DEFAULT_WIDTH=1280
AVATAR_MAX_DIMENSION=1920
AVATAR_NEGATIVE_PROMPT=blurry, low quality, distorted, deformed, ugly, bad anatomy, watermark, text, signature
AVATAR_STORAGE_PATH=assets/avatars

CHATTERBOX_BASE_URL=http://chatterbox-host:4123
CHATTERBOX_DEFAULT_TIMEOUT_SECONDS=120

DATA_DIRECTORY=data

NEWS_DEFAULT_WPM=145
NEWS_CONTEXT_MAX_SENTENCES=80
NEWS_CONTEXT_MIN_SENTENCE_CHARS=40
NEWS_CONTEXT_RERANK_BACKEND=http
NEWS_CONTEXT_RERANK_BASE_URL=
NEWS_CONTEXT_RERANK_ENABLED=false
NEWS_CONTEXT_RERANK_ENDPOINT=http://reranker-host:8007/v1/rerank
NEWS_CONTEXT_RERANK_MODEL=bge-reranker-v2-m3
NEWS_CONTEXT_RERANK_TIMEOUT_SECONDS=60
NEWS_CONTEXT_RERANK_TOP_N=20
NEWS_SOURCE_RANK_ENABLED=true
NEWS_SOURCE_RANK_CANDIDATES=50
NEWS_SOURCE_RANK_QUERY=artificial intelligence and technology news
NEWS_CONTEXT_WINDOW_SENTENCES=0
NEWS_INTENDED_VIDEO_MAX_SECONDS=30
NEWS_OUTPUT_DIRECTORY=output
NEWS_SUMMARIZE_PROMPT=
NEWS_SUMMARIZE_PROMPT_FILE=prompts/generic-news-reporter.md
NEWS_VIDEO_PROMPT=A medium close-up video of the person from the reference image speaking directly to camera. Preserve identity, face shape, hair, clothing, and background. Natural mouth movement, subtle head motion, realistic expression, stable camera.
NEWS_VIDEO_PROMPT_FILE=
NEWS_VIDEO_SOUND_PROMPT=Clear spoken narration, moderate volume, close to the microphone.

OPENAI_API_KEY=
OPENAI_BASE_URL=http://llm-host:11434/v1
OPENAI_DEFAULT_TIMEOUT_SECONDS=90
OPENAI_MODEL=your-chat-model
OPENAI_REASONING_EFFORT=

SEARXNG_BASE_URL=http://searxng-host:8888
SEARXNG_DEFAULT_CATEGORIES=news,general
SEARXNG_DEFAULT_LANGUAGE=en
SEARXNG_DEFAULT_MAX_AGE_DAYS=3
SEARXNG_DEFAULT_MAX_ARTICLES=5
SEARXNG_DEFAULT_SAFESEARCH=1
SEARXNG_DEFAULT_TIME_RANGE=
SEARXNG_DEFAULT_TIMEOUT_SECONDS=20

SEQ_LOGGING_API_KEY=
SEQ_LOGGING_BASE_URL=http://seq-host:5341
LOG_DIR=logs
LOG_LEVEL=INFO

VIDEO_COMFYUI_BASE_URL=http://video-comfyui-host:8188
VIDEO_COMFYUI_FPS=24
VIDEO_COMFYUI_FREE_BEFORE_RUN=true
VIDEO_COMFYUI_HEIGHT=704
VIDEO_COMFYUI_OUTPUT_PREFIX=video/news-generator
VIDEO_COMFYUI_PROMPT_ENHANCE=false
VIDEO_COMFYUI_TIMEOUT_SECONDS=5400
VIDEO_COMFYUI_WIDTH=1280
VIDEO_COMFYUI_WORKFLOW=comfyui-workflows/video_ltx2_3_ia2v-working.json
```

`VIDEO_COMFYUI_BASE_URL` and `AVATAR_COMFYUI_BASE_URL` are intentionally separate. Video generation can target the heavier DGX ComfyUI instance, while avatar generation can target a lighter local ComfyUI instance that has the turbo image workflow available.

`NEWS_OUTPUT_DIRECTORY` may be absolute or project-relative. Relative values resolve from the repository root.

`NEWS_SUMMARIZE_PROMPT` or `NEWS_SUMMARIZE_PROMPT_FILE` is required unless overridden by a CLI prompt parameter or prompt file. There is no hardcoded prompt fallback in the script. The default repo prompt profile is `prompts/generic-news-reporter.md`. That profile attributes each story to its outlet by name, derived from the source domain, with varied phrasing ("According to DigiTimes, ...", "CNN is reporting ...", "From Yahoo Finance today, ..."), and never reads a URL aloud.

`NEWS_INTENDED_VIDEO_MAX_SECONDS` is appended to the summarize prompt as timing guidance. With `NEWS_DEFAULT_WPM`, the script computes the maximum spoken word count and tells the LLM to treat that word count as a hard cap.

`NEWS_SOURCE_RANK_ENABLED`, `NEWS_SOURCE_RANK_CANDIDATES`, and `NEWS_SOURCE_RANK_QUERY` control article-level ranking. When enabled, search keeps up to `NEWS_SOURCE_RANK_CANDIDATES` results that pass the age rule, scores each one's title and snippet against `NEWS_SOURCE_RANK_QUERY` through the `NEWS_CONTEXT_RERANK_*` endpoint, and keeps the top `max_articles` in score order. See [Source Ranking](#source-ranking).

`VIDEO_COMFYUI_PROMPT_ENHANCE` sets the default for the workflow's prompt-enhancement switch. It fills the `{{PROMPT_ENHANCE}}` placeholder, so the template must carry that placeholder on the boolean node that drives the switch; the API `prompt_enhance` field and the CLI `--prompt-enhance` / `--no-prompt-enhance` flags override it per run.

`VIDEO_COMFYUI_WIDTH` and `VIDEO_COMFYUI_HEIGHT` fix the final video size. 1280x704 is the workflow's native near-720p size; 1280x720 works but is produced by a re-encode. `AVATAR_DEFAULT_WIDTH` and `AVATAR_DEFAULT_HEIGHT` must also be 16:9; avatar generation rejects any other ratio. See [Output Size](#output-size).

## Service Topology

Every backend is reached over HTTP through its own base URL, so each one can live
on a different machine and be moved without code changes. Nothing in the pipeline
assumes two backends share a host, a GPU, or a network.

| Backend | Variable | Used by |
| --- | --- | --- |
| SearXNG | `SEARXNG_BASE_URL` | source search |
| Script LLM | `OPENAI_BASE_URL` | script generation |
| Reranker | `NEWS_CONTEXT_RERANK_ENDPOINT` | evidence ordering (optional) |
| Chatterbox | `CHATTERBOX_BASE_URL` | narration audio |
| Avatar ComfyUI | `AVATAR_COMFYUI_BASE_URL` | avatar image generation |
| Video ComfyUI | `VIDEO_COMFYUI_BASE_URL` | talking-head video |

Avatar and video ComfyUI are deliberately separate variables. Point them at one
server to share it, or at two servers to keep image generation off the machine
doing video, which is by far the largest consumer of memory.

Where backends do share hardware, `VIDEO_COMFYUI_FREE_BEFORE_RUN=true` asks the
video ComfyUI server to unload its models before a render. It is off by default,
because co-location is a deployment choice rather than something the pipeline
should assume.

Video memory scales with clip length: the workflow renders `duration * fps + 1`
frames and holds them through upscale and decode. Clip length is the main lever
when a render approaches the memory ceiling.

## Backend Choices

The app is intentionally endpoint-driven. The code does not require one vendor or runtime, but the selected runtime must expose the expected HTTP shape.

### Script LLM

The script generator calls an OpenAI-compatible chat completions endpoint configured by `OPENAI_BASE_URL` and `OPENAI_MODEL`.

Current local default:

```text
OPENAI_BASE_URL=http://llm-host:11434/v1
OPENAI_MODEL=your-chat-model
```

That points at Ollama's OpenAI-compatible `/v1/chat/completions` API.

Alternative: llama.cpp can also serve OpenAI-compatible chat endpoints for supported chat/generation models. If using llama.cpp for the script LLM, point `OPENAI_BASE_URL` at that llama.cpp server's `/v1` base URL and set `OPENAI_MODEL` to the served model name.

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

The stack is two containers plus whatever GPU services you point it at: the app (API, worker, scheduler, admin UI, MCP over HTTP, with ffmpeg and the built React bundle inside the image) and SearXNG. ComfyUI stays native on its GPU box; Chatterbox, the reranker, crawl4ai, and the LLM run wherever they already run and are reached by URL. The image is a multi-stage build: Node builds `web/dist`, then a Python image installs ffmpeg and the app.

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

This file is the official ComfyUI **LTX-2.3: Image Audio to Video (IA2V)** template, exported in API format with the pipeline placeholders substituted for the literal values. Nothing else about it is modified.

#### The narration WAV is the soundtrack

In the official IA2V template the uploaded WAV goes `LoadAudio -> TrimAudioDuration -> LTXVAudioVAEEncode -> SetLatentNoiseMask -> LTXVConcatAVLatent` into the first sampler pass. The audio latent is masked so it is never regenerated. The video's soundtrack is the narration, and the mouth is driven by it. This is what makes lip sync exact and makes the video end where the narration ends.

`comfyui-workflows/video_ltx2_3_ia2v-good-test.json` is **not** that template and must not be used for narrated video. It carries two nodes the official template does not have: `LTXVReferenceAudio`, which per the ComfyUI node documentation transfers only the speaker's voice identity to *generated* audio, and `LTXVEmptyLatentAudio`, which starts the first pass from a blank audio latent. In that variant the model speaks the `[SPEECH]` text of the prompt itself, at its own pace, in a voice cloned from the WAV. Symptoms: the video babbles when the script is not in the prompt, trailing sounds in the WAV do not appear in the video, and long scripts get cut off mid-sentence because the model talks slower than Chatterbox did.

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

`comfyui-workflows/video_ltx2_3_ia2v-extend-mac.json` is the same template for ComfyUI on Apple Silicon. MPS has no float8 type, so the fp8 checkpoint and the fp4 text encoder fail to load there; the Mac copy names the bf16 files instead (`ltx-2.3-22b-dev.safetensors` from Lightricks/LTX-2.3, 46 GB, and `gemma_3_12B_it.safetensors` from Comfy-Org/ltx-2, 24 GB). The two LoRAs and the upscaler are already bf16 and are shared. Nothing else differs; keep the two files in step when the template changes.

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

Install the Lightricks LTX-2.3 / LTXV ComfyUI workflow dependencies on the target ComfyUI instance and place the checkpoint, text encoder, upscaler, and LoRA files where those nodes expect them. Common locations are ComfyUI `models/checkpoints`, `models/text_encoders`, `models/upscale_models`, and `models/loras`.

### ComfyUI Validation Checklist

Before using this app against a ComfyUI server:

```text
1. Open the workflow in ComfyUI directly.
2. Confirm no nodes are red or missing.
3. Confirm every model dropdown resolves to an installed file.
4. Run the workflow once manually with test image/audio inputs.
5. Confirm ComfyUI can write outputs and the app can reach /prompt, /history/{prompt_id}, /upload/image, and /view.
```

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

Runs from a profile rank by the profile's `rank_query`, or by its search terms when that is blank. The bundle records `candidate_count`, a `source_rank` block (endpoint, model, query, counts, and any degradation error), and a `source_rank_score` on each selected source. If the rerank endpoint fails, the run continues with the first `max_articles` candidates in search order and `source_rank.degraded` is `true`.

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

The implementation protects common abbreviations before sentence segmentation, splits on sentence boundaries, restores abbreviation periods, then keeps only clean complete sentences. Page fetching remains a future enhancement; the current implementation builds evidence from selected SearXNG result snippets and keeps result titles as metadata.

## 3. Generate Audio

Audio generation uses Chatterbox and requires an explicit `--voice`.

Existing library voice:

```bash
.venv/bin/python scripts/generate_audio.py \
  --text-file output/YYYY-MM-DD/private-ai-risks-and-advancements/narration.txt \
  --voice your-voice-name \
  --output output/YYYY-MM-DD/private-ai-risks-and-advancements/narration.wav
```

Voice rules:

1. `--voice` is required.
2. The script never implicitly uses Chatterbox's API/file-system default voice.
3. If no `--voice-sample` is provided, `--voice` must already exist in `GET /voices`.
4. If `--voice-sample` is provided and `--voice` exists, the script deletes that Chatterbox library voice and uploads the new sample under the same name.
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

Video generation sends the image and narration WAV to ComfyUI, patches the workflow placeholders, queues the prompt, waits for history, and downloads the output media.

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

Every pipeline run appends timestamped lines to `run.log` in the run directory, tagged by stage (`run`, `search`, `evidence`, `script`, `audio`, `video`): the SearXNG request URL and counts, each selected source with its rank score, evidence rerank details, the LLM model and word counts, the resolved voice and narration duration, the ComfyUI prompt id, upload names, output file, final size, and any stage error. The same lines go to the `news_pipeline` Python logger, so they appear on the API server console.

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
mcp_server/     future MCP tools that call the shared service layer
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
CHATTERBOX_BASE_URL
CHATTERBOX_DEFAULT_TIMEOUT_SECONDS
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
  "stage": "request | search | script | audio | video",
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

`GET /voices` queries Chatterbox using `CHATTERBOX_BASE_URL` and returns the currently available library voice names and aliases. It is a discovery endpoint only; it does not define or imply a default voice fallback.

Avatar endpoints store canonical PNG files under `AVATAR_STORAGE_PATH`. Relative paths resolve from the repository root. `POST /avatars/{avatar_name}/upload` accepts multipart field `file`, validates the image with Pillow, converts it to PNG, and writes `{avatar_name}.png` plus `{avatar_name}.json` metadata. If `AVATAR_API_KEY` is set, upload requires either `Authorization: Bearer <key>` or `X-API-Key: <key>`.

`POST /avatars/generate` accepts `avatar_name`, `avatar_prompt`, optional `replace_avatar`, and optional `width`/`height`. The default generation size is 1920x1080 for YouTube-friendly 1080p output. It loads `AVATAR_COMFYUI_WORKFLOW`, replaces the profile-image placeholders, queues ComfyUI, downloads the generated image, stores it as `{avatar_name}.png`, and writes metadata. If the avatar already exists, generation fails unless `replace_avatar=true`.

## Tenancy and Database

The app keeps tenant-scoped state in SQLite at `DATA_DIRECTORY/news.db` (default `data/`, gitignored). `DATABASE_URL` overrides the location entirely. Tables are created on startup; there is no migration tool yet.

`.env` is system control: service endpoints, storage paths, output size, and the admin tenant. On every start the app ensures the admin tenant exists (`ADMIN_TENANT_SLUG`, `ADMIN_TENANT_NAME`) and that its bootstrap API key matches `ADMIN_API_KEY`, falling back to `AVATAR_API_KEY` so an existing installation keeps its key. Changing `ADMIN_API_KEY` revokes the previous bootstrap key on the next start. All other tenants, their keys, prompts, profiles, avatars, voices, schedules, and runs live only in the database.

Routes under `/api/v1/` require a key: `Authorization: Bearer <key>` or `X-API-Key: <key>`. Keys are stored hashed and shown once at creation. A missing, unknown, or revoked key returns 401 with the standard error shape. The pre-existing routes (`/generate`, `/stages/*`, `/runs/*`, `/voices`, `/avatars*`) are unchanged for now and act on the admin tenant.

```text
GET    /api/v1/me                       the calling tenant
GET    /api/v1/system                   version, video settings, configured services, storage (URLs for admin only)

GET    /api/v1/voices                   list (limit, cursor)
POST   /api/v1/voices                   multipart: name, sample (wav), language, replace
GET    /api/v1/voices/{name}
GET    /api/v1/voices/{name}/sample     the stored sample wav
DELETE /api/v1/voices/{name}

GET    /api/v1/avatars                  list (limit, cursor)
POST   /api/v1/avatars/generate         form: name, prompt, negative_prompt, width, height, seed, replace
POST   /api/v1/avatars/upload           multipart: name, image, replace
GET    /api/v1/avatars/{name}
GET    /api/v1/avatars/{name}/image     the png
DELETE /api/v1/avatars/{name}

GET    /api/v1/prompts                  the global default prompts (summarize, video_visual, video_sound) with body, path, updated_at
GET    /api/v1/prompts/{kind}           body, path, editable, variables
PUT    /api/v1/prompts/{kind}           form: body; rewrites the default file; admin tenant only
POST   /api/v1/prompts/{kind}/test      form: search_text, max_articles; runs search and script only with the default summarize prompt

POST   /api/v1/runs                     json: profile_id, overrides, priority, wait; 202 with the queued run
GET    /api/v1/runs                     list newest first (status, profile_id, limit, cursor)
GET    /api/v1/runs/{id}                status, stage, timings, error, preflight, artifacts, metrics, selected_sources
POST   /api/v1/runs/{id}/cancel
POST   /api/v1/runs/{id}/retry          202 with a new run resuming from the failed stage
GET    /api/v1/runs/{id}/files/{artifact}       the file: video, audio, narration, sources, report, log, preflight
GET    /api/v1/runs/{id}/files/{artifact}/info  url, size, content_type; ?inline=true adds text content

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
POST   /api/v1/search/preview           json: search_text, categories, language, max_articles, max_age_days, rank_query; the articles a run would select, no LLM, no run

GET    /api/v1/profiles                 list (limit, cursor)
POST   /api/v1/profiles                 json: name, search_text, and any other profile field
GET    /api/v1/profiles/{id}
PUT    /api/v1/profiles/{id}            json: fields to change
DELETE /api/v1/profiles/{id}            conflict if a schedule references it
```

Every error from these routes has one shape: `{"error": {"code", "stage", "message"}}` with codes `not_found` (404), `conflict` (409), `invalid` (400), `unauthorized` (401), `forbidden` (403, admin-only operation), `preflight_failed` (503), `pipeline_failed` (500), `service_unavailable` (503). Every write is recorded in `audit_events` with the acting key, the object, and its before and after state.

Runs and the worker: `POST /api/v1/runs` queues a run and returns immediately; a worker thread in the API process executes queued runs one at a time, oldest first by priority, through preflight, search and script, audio, and video, updating `status` and `stage` on the run as it goes. Runs use the tenant's prompts, the tenant's voice (falling back to a pre-existing Chatterbox voice of the same name) and avatar (falling back to `assets/avatars/<name>.png`), and write under `output/<tenant>/<run_id>/`. `cancel` ends a queued run at once and asks a running one to stop between stages, interrupting ComfyUI if the video stage is active. `retry` queues a new run that resumes from the failed stage in the same directory. Runs left in progress by a restart are marked failed. `NEWS_WORKER_ENABLED=false` disables the worker (for a process that should only enqueue). The MCP server enqueues but does not execute; the API must be running.

Schedules: a five-field cron expression bound to a profile, evaluated in the schedule's timezone (default UTC). A scheduler thread in the API process checks every fifteen seconds for enabled schedules whose `next_run_at` has passed, runs the cached preflight once per pass, skips the fire with a recorded `last_error` when a hard check fails or the tenant is inactive, otherwise enqueues the profile exactly as `POST /api/v1/runs` would with actor `scheduler`, then advances `next_run_at`. `last_run_id` and `last_run_at` show what a schedule last produced. `NEWS_SCHEDULER_ENABLED=false` disables the thread.

Tenant storage: avatars under `assets/avatars/<tenant>/`, voice samples under `assets/voices/<tenant>/` (`VOICE_STORAGE_PATH` overrides the root), and Chatterbox library names prefixed `<tenant>-`. Prompts are seeded per tenant from the repository files and `.env` values on first access; `default_body` always reflects the current file so a tenant can see and reset to it. Until the runs step, `/generate` and the other pre-existing routes still read the prompt files directly.

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

Develop with hot reload (the API on :8000, Vite on :5173 proxying `/api`, `/health`, and `/mcp`):

```bash
cd web
npm run dev
```

Screens: dashboard (service checks, system, recent runs), runs (list, filters, run now from a profile), run detail (video, narration, script, sources, preflight, log, cancel, retry), profiles, schedules, prompts, avatars, voices, and tenants for the admin tenant. `web/node_modules` and `web/dist` are gitignored; build on deploy.

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

Verify with `ffmpeg -version` and `ffprobe -version` in a new shell. Preflight reports `local: warn` when either is missing; runs still complete but without size verification or chunk stitching, so treat the warning as a hard requirement once segmented rendering is on.

## Segmented Rendering

Every script, whatever its length and however many articles it covers, is split at sentence boundaries into chunks of at most `SEGMENT_MAX_WORDS` (default 100, about 40 seconds of narration). Each chunk is narrated on its own (`chunks/NN/narration.wav`), rendered on its own through the unchanged video stage (`chunks/NN/video.mp4`), and the clips are appended with ffmpeg into the run's `video.mp4`; the narrations are appended into `narration.wav` the same way. `chunks.json` in the run directory is the manifest: text, word count, narration and clip durations, and status per chunk.

This is the memory bound for the DGX: a single 94-second render reached 111.9 GB of 121.7 GB on the Spark and had to be interrupted, while renders under about 40 seconds stayed near 90 GB. With chunks, no render exceeds the chunk size no matter what the LLM wrote, so the word cap in the prompt is a quality target rather than a safety mechanism. Chunks whose clip already exists are reused, so a retry after a failure at chunk four renders only chunks four and up; cancel is honored between chunks. Transitions (title cards, crossfades) are a later step; today the clips are appended with a straight cut. `SEGMENT_ENABLED=false` restores single-clip rendering.

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

Every run begins with a preflight in `services/preflight.py` before any stage starts. It checks, in parallel with five-second timeouts: SearXNG (reachable, JSON format enabled, engines exist for the configured categories), the LLM (`OPENAI_MODEL` is served), the reranker (soft: a failure only degrades ranking to lexical), Chatterbox (model loaded, and the run's voice exists in its library), ComfyUI video (reachable, queue depth, `VIDEO_COMFYUI_WORKFLOW` passes `validate_video_workflow`, the anchor image exists), and local prerequisites (ffmpeg and ffprobe on PATH, output directory writable, free disk).

Each check is logged as a `[preflight]` line in `run.log` and the full result is written to `preflight.json` in the run directory. Any hard failure ends the run at stage `preflight` with a message naming every failed service, and nothing is generated. Results are cached for thirty seconds. `PREFLIGHT_TIMEOUT_SECONDS` overrides the per-check timeout.

The same checks are served without a run:

```text
GET /health/deep
GET /health/deep?voice=<voice>&avatar_name=<avatar>
```

Returns 200 when every hard check passes and 503 otherwise, with the same JSON body either way. Use it as a service status panel and before scheduling runs.

## Incremental Test Gates

The pipeline should be validated in small production slices before full video generation:

```text
1. request accepted: FastAPI imports, health endpoint, route registration
2. voices listed: GET /voices reaches Chatterbox and returns available voice names
3. avatar generated or reused: future avatar endpoints create and retrieve a stored image
4. search made: search stage writes sources.json from SearXNG
5. summary created: script stage writes narration.txt and report.md within the word cap
6. audio created: Chatterbox writes narration.wav with the explicit named voice
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
voice: required Chatterbox voice name
script_text: optional direct script
run_id: optional existing run with narration.txt
voice_sample_file: optional WAV upload
```

It writes `narration.wav` and returns `audio_url`, `audio_duration_seconds`, `audio_size_bytes`, `voice`, and `voice_mode`. Provide either `script_text` or a `run_id` that already has `narration.txt`.

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
list_voices, get_voice, upload_voice(name, sample_path), delete_voice
list_avatars, get_avatar, generate_avatar, upload_avatar(name, image_path), delete_avatar
list_prompts, get_prompt, update_prompt (admin), test_prompt(search_text)
list_search_profiles, get_search_profile, upsert_search_profile, delete_search_profile
enqueue_run, get_run, list_runs, cancel_run, retry_run, get_run_file
list_schedules, upsert_schedule, delete_schedule, run_schedule_now
preview_search, get_search_options
list_tenants, create_tenant, list_api_keys, create_api_key, revoke_api_key, set_tenant_active   (admin only)
```

File-taking tools accept a path on the server host, since stdio clients run locally. Errors carry the catalog error shape serialized in the tool error message.

Remaining catalog tools are added phase by phase. Tool results return structured run metadata and artifact links, never embedded media. The streamable HTTP transport, mounted on the FastAPI app with bearer-key auth, arrives with the tenancy phase.

## Notes

- The pipeline should remain generic; avoid hardcoding topics, prompts, voices, or assets in scripts.
- Use filesystem artifacts for v1. Add SQLite or Postgres only if a run index or historical search becomes necessary.
- YouTube publishing is not part of the current implemented flow.

## License

MIT. See [LICENSE](LICENSE).
