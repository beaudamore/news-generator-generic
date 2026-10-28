# news-generator helper (ComfyUI custom node package)

Adds model-download routes to a ComfyUI server so the news-generator app can tell that
host to fetch the model files a workflow needs. It registers no nodes and has no
dependencies beyond what ComfyUI already ships (aiohttp, `folder_paths`).

ComfyUI's public API lists models but has no route to download one; ComfyUI-Manager has
one, but its routes are undocumented and change between versions. This package is the
stable contract the app talks to on every ComfyUI host (CUDA box, DGX, Apple Silicon).

## Install

```
cd <ComfyUI>/custom_nodes
cp -r <this repo>/comfyui-helper-node/news_generator_helper .    # or: ln -s
# restart ComfyUI; the log shows "[news-generator-helper] v0.1.0 routes registered"
```

ComfyUI Desktop: the custom_nodes folder is the one shown in its settings; a restart of
the app reloads it.

## Configuration (environment of the ComfyUI process)

| Variable | Default | Meaning |
|---|---|---|
| `NEWS_GENERATOR_DOWNLOAD_ALLOWED_HOSTS` | `huggingface.co,hf.co,civitai.com` | Hosts a download URL may point at (redirects to CDNs are followed) |
| `HF_TOKEN` / `HUGGINGFACE_TOKEN` | — | Used for Hugging Face URLs when the request carries no token (gated repos) |
| `CIVITAI_API_TOKEN` | — | Used for Civitai URLs when the request carries no token |

The app can also pass a token per request (`COMFYUI_HF_TOKEN` / `COMFYUI_CIVITAI_TOKEN`
in the app's `.env`), which is simpler than configuring every ComfyUI host.

## Routes

```
GET  /news-generator/helper                      name, version, known model folders, allowed hosts
GET  /news-generator/models/files/{folder}       files ComfyUI sees in that models folder
POST /news-generator/models/download             {"url","folder","filename","sha256"?,"token"?,"subfolder"?,"overwrite"?}
                                                 202 {"job"} queued or already running; 200 {"job": {"status":"exists"}} when present
GET  /news-generator/models/jobs                 every job this process has seen (bounded)
GET  /news-generator/models/jobs/{id}            one job
POST /news-generator/models/jobs/{id}/cancel
```

Job shape: `{"id","status","url","folder","filename","path","bytes_done","bytes_total",
"percent","speed_bps","error","created_at","started_at","finished_at"}`, status one of
`queued`, `downloading`, `verifying`, `done`, `exists`, `failed`, `cancelled`.

Downloads run one at a time, write `<filename>.part`, resume with a Range request if a
`.part` is left from an interrupted run, verify `sha256` when given, then rename into
place. The folder name is a ComfyUI models folder (`checkpoints`, `diffusion_models`,
`text_encoders`, `vae`, `loras`, `latent_upscale_models`, `audio_encoders`, ...); the file
lands in the first configured path for that folder, which honours `extra_model_paths.yaml`.

## Try it

```
curl http://comfy-host:8188/news-generator/helper
curl -X POST http://comfy-host:8188/news-generator/models/download \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors","folder":"vae","filename":"ae.safetensors"}'
curl http://comfy-host:8188/news-generator/models/jobs
```
