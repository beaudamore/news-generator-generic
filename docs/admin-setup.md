# Admin Setup and Testing Guide

This guide takes an administrator from a fresh clone to a verified working pipeline. Follow the phases in order; each phase has a pass/fail gate before moving on.

The app itself is CPU-only and portable. All heavy work happens on external HTTP services you point it at:

| Dependency | Env var(s) | What it does |
|---|---|---|
| SearXNG | `SEARXNG_BASE_URL` | news/web search |
| OpenAI-compatible LLM (llama.cpp, vLLM, SGLang, Ollama) | `OPENAI_BASE_URL`, `OPENAI_MODEL` | script writing |
| Reranker (llama.cpp, vLLM, TEI) | `NEWS_CONTEXT_RERANK_*` | optional evidence reranking |
| TTS server (OpenAI-compatible, Qwen3-TTS) | `TTS_OPENAI_BASE_URL`, `TTS_OPENAI_MODEL` | narration audio and the voice library |
| ComfyUI (video) | `VIDEO_COMFYUI_BASE_URL`, `VIDEO_COMFYUI_WORKFLOW` | LTX-2.3 talking-head video; the workflow must be the official IA2V template, see README "Video Workflow" |
| ComfyUI (avatar, optional) | `AVATAR_COMFYUI_BASE_URL` | Z-Image-Turbo avatar generation |

## Phase 0 — Python environment

Python 3.11–3.13 required. Python 3.14 does not work: `Pillow==10.4.0` has no 3.14 wheel and fails to build from source. The Docker image pins 3.12.

```bash
# macOS/Linux
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

```powershell
# Windows
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Gate: this import check prints versions with no errors.

```powershell
.\.venv\Scripts\python.exe -c "import PIL, fastapi, uvicorn, requests, dotenv; print('ok')"
```

Also install ffmpeg (needed by the pipeline for stitching, loudness normalization, and size checks): `winget install Gyan.FFmpeg` on Windows, `brew install ffmpeg` on macOS, `sudo apt-get install -y ffmpeg` on Debian/Ubuntu. `ffmpeg -version` in a new shell confirms it. The Docker image includes it.

## Phase 1 — Create .env

Copy the Environment block from the README into a `.env` file at the repo root and edit the URLs for your network. `.env` is gitignored — never commit it; it typically contains private LAN/VPN addresses.

Notes:

- `OPENAI_MODEL` must be a model that actually exists on your LLM server (`ollama list` / your server's `/v1/models`).
- `NEWS_CONTEXT_RERANK_ENABLED=true` only after the reranker (Phase 3) is running.
- `AVATAR_COMFYUI_BASE_URL` may point to a different, lighter ComfyUI instance than `VIDEO_COMFYUI_BASE_URL`.

## Phase 2 — Verify external endpoints

Every URL in `.env` must answer before testing the app. Quick probe:

```powershell
# Windows PowerShell — adjust hosts to your .env values
'Ollama=http://localhost:11434/v1/models',
'SearXNG=http://localhost:8888/config',
'TTS=http://your-tts-host:8184/v1/audio/voices',
'VideoComfyUI=http://your-gpu-host:8188/system_stats',
'AvatarComfyUI=http://your-avatar-host:8188/system_stats',
'Reranker=http://127.0.0.1:8033/health' |
ForEach-Object { $n,$u = $_ -split '=',2
  try { $r = Invoke-WebRequest $u -TimeoutSec 5 -UseBasicParsing; "OK    $n [$($r.StatusCode)]" }
  catch { "FAIL  $n : $($_.Exception.Message)" } }
```

```bash
# macOS/Linux
for pair in Ollama=http://localhost:11434/v1/models SearXNG=http://localhost:8888/config; do
  name=${pair%%=*}; url=${pair#*=}
  curl -fsS -m 5 -o /dev/null "$url" && echo "OK    $name" || echo "FAIL  $name"
done
```

Gate: every service you intend to use returns OK. The reranker will fail until Phase 3 — that is expected.

## Phase 3 — Start the reranker (optional but recommended)

Verified choice: a vLLM worker serving `BAAI/bge-reranker-v2-m3`. Point the app at it with `NEWS_CONTEXT_RERANK_BACKEND=http`, `NEWS_CONTEXT_RERANK_ENDPOINT=http://<vllm-host>:<port>/v1/rerank`, and `NEWS_CONTEXT_RERANK_MODEL=<id from GET /v1/models>`. Note the worker is plain http unless you put TLS in front of it. The same endpoint also ranks candidate articles when `NEWS_SOURCE_RANK_ENABLED=true`. The llama.cpp route below is the alternative if you have no vLLM.

Ollama can pull the BGE reranker model but does not serve a working rerank endpoint for it. Serve the same GGUF blob through llama.cpp.

Install llama.cpp:

```powershell
# Windows
winget install ggml.llamacpp   # then open a new shell for PATH
```

```bash
# macOS
brew install llama.cpp
```

Find the GGUF. If you pulled `xitao/bge-reranker-v2-m3:latest` with Ollama, the blob is already on disk — it is the ~1.2 GB file:

```powershell
# Windows
Get-ChildItem "$env:USERPROFILE\.ollama\models\blobs" | Sort-Object Length -Descending | Select-Object -First 5
```

```bash
# macOS/Linux
ls -lS ~/.ollama/models/blobs | head -5
```

Start the server (long-running; keep the terminal open or wrap in a service):

```powershell
llama-server -m "$env:USERPROFILE\.ollama\models\blobs\sha256-<digest>" `
  --host 127.0.0.1 --port 8033 --embedding --pooling rank --reranking
```

Wait for `listening on http://127.0.0.1:8033`, then verify with a real rerank call:

```powershell
$body = '{"model":"xitao/bge-reranker-v2-m3:latest","query":"local AI privacy","documents":["Local AI keeps your data on your own machine.","Bananas are yellow fruit."],"top_n":2}'
Invoke-RestMethod -Uri http://127.0.0.1:8033/v1/rerank -Method Post -ContentType 'application/json' -Body $body
```

Gate: the on-topic document scores far higher than the off-topic one (e.g. `-0.3` vs `-11.0`; llama.cpp returns raw logits, higher = more relevant). The response field is `relevance_score`; the app accepts both `score` and `relevance_score`.

Now set `NEWS_CONTEXT_RERANK_ENABLED=true` in `.env`.

## Phase 4 — Incremental pipeline test sequence

Start the API:

```powershell
.\.venv\Scripts\uvicorn.exe api.main:app --host 0.0.0.0 --port 8000
```

Run the gates in order. Stop at the first failure; the `stage` field in error responses tells you which dependency to look at.

### Gate 0 — Deep health (preflight of every dependency)

```bash
curl -i http://localhost:8000/health/deep
# expect HTTP 200 and "ok": true; 503 lists which service failed and why
```

This runs the same checks every run performs before starting. Fix anything marked `fail` before continuing; `warn` on the reranker is acceptable (ranking degrades to lexical). Add `?voice=<name>&avatar_name=<name>` to also verify a specific voice and avatar.

### Gate 1 — App is alive

```bash
curl http://localhost:8000/health
# expect {"status":"ok"}
```

### Gate 2 — Voices listed (proves TTS server connectivity)

```bash
curl http://localhost:8000/voices
# expect {"voices":[...],"count":N}
```

### Gate 3 — Search + script (proves SearXNG + LLM + reranker)

```bash
curl -X POST http://localhost:8000/stages/script \
  -F 'search_text=local ai privacy news this week'
# expect run_id, script text, word_count, target_words, selected_sources
```

Inspect `output/<run_id>/report.md` to confirm evidence sentences and rerank scores look sane.

### Gate 4 — Audio (proves TTS synthesis)

Use the `run_id` from Gate 3. First run: also upload a voice sample to create the named voice.

```bash
curl -X POST http://localhost:8000/stages/audio \
  -F 'voice=reporter-voice' \
  -F 'voice_sample_file=@/path/to/voice-sample.wav' \
  -F 'run_id=<run_id-from-gate-3>'
# expect audio_url, audio_duration_seconds
```

Listen to `output/<run_id>/narration.wav`.

### Gate 5 — Avatar (optional; proves avatar ComfyUI)

```bash
curl -X POST http://localhost:8000/avatars/generate \
  -F 'avatar_name=test-anchor' \
  -F 'avatar_prompt=professional news anchor, studio background'
curl http://localhost:8000/avatars/test-anchor -o test-anchor.png
```

Skip if you will always upload `anchor_image_file` yourself.

### Gate 6 — Video (proves video ComfyUI + LTX workflow)

```bash
curl -X POST http://localhost:8000/stages/video \
  -F 'run_id=<run_id-from-gate-3>' \
  -F 'anchor_image_file=@/path/to/anchor-image.png'
# expect video_url, video_duration_seconds
```

This is the slow gate (minutes on a capable GPU). Before blaming the app, run the workflow JSON manually in the ComfyUI UI — see the ComfyUI Validation Checklist in the README.

The video stage validates the template before queuing. An error stage `video` whose message names `LTXVReferenceAudio`, `LTXVEmptyLatentAudio`, missing placeholders, or `LTXVAudioVAEEncode` means the wrong template is configured; fix `VIDEO_COMFYUI_WORKFLOW`, do not bypass the check. `output/<run_id>/run.log` records the validation result and every other stage.

Output size: LTX-2.3 renders in 32-pixel blocks and this template runs its first pass at half size, so `VIDEO_COMFYUI_WIDTH` x `VIDEO_COMFYUI_HEIGHT` should be multiples of 64. `1280x704` is native near-720p. `1280x720` also works but is produced by re-encoding the 704 output with ffmpeg.

### Gate 7 — Full end-to-end

```bash
curl -X POST http://localhost:8000/generate \
  -F 'voice=reporter-voice' \
  -F 'anchor_image_file=@/path/to/anchor-image.png' \
  -F 'search_text=local ai privacy news this week'
```

Expect `status: success` with `video_url`, then:

```bash
curl -OJ http://localhost:8000/runs/<run_id>/video
```

## Troubleshooting quick reference

| Symptom | Likely cause |
|---|---|
| error stage `preflight` | a dependency failed its check before the run started; the message names the service and the problem, and `output/<run_id>/preflight.json` has every check |
| error stage `search` | SearXNG down or `SEARXNG_BASE_URL` wrong |
| error stage `script` | LLM server down, `OPENAI_MODEL` doesn't exist on it, or word-cap exceeded |
| script gate hangs then falls back to title-based script | `OPENAI_BASE_URL` unreachable |
| error stage `audio` | TTS server down, `TTS_OPENAI_MODEL` not served, or `voice` not in `GET /voices` and no sample uploaded |
| error stage `video` | ComfyUI down, workflow nodes/models missing, or timeout too low for the GPU |
| error stage `video` naming `LTXVReferenceAudio` / `LTXVEmptyLatentAudio` / placeholders | `VIDEO_COMFYUI_WORKFLOW` points at the wrong template; use the official IA2V export (README "Video Workflow") |
| video babbles, says words not in the script, or cuts off before the narration ends | same cause: an ID-LoRA voice-clone template that generates its own speech; the validator now refuses these |
| no sources selected | every result was undated or older than `SEARXNG_DEFAULT_MAX_AGE_DAYS`; undated hits are discarded on purpose |
| rerank silently no-ops | `NEWS_CONTEXT_RERANK_ENABLED` not `true`, or the rerank endpoint is down; `sources.json` `source_rank.degraded` and `evidence_context.rerank.degraded_from` show the fallback |
| Pillow install fails | Python 3.14 — use 3.11–3.13 |
