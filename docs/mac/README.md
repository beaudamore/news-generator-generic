# LTX-2.3 video on Apple Silicon with ComfyUI: findings

Status: written during the work on 2026-10-14. The recipe in section 4 produced a correct clip end to end; the multi-clip run was stopped by choice once that was proven.

Machine: Mac Studio M1 Ultra, 128 GB unified memory, macOS 26, ComfyUI Desktop (ComfyUI 0.40.0, Python 3.12, torch 2.12, MPS). Models on an external SSD. Reference point for comparison: the same pipeline on an NVIDIA DGX Spark running the stock fp8 template.

## 1. The stock templates cannot run on a Mac

ComfyUI's official LTX-2.3 "Image Audio to Video" template names these model files:

| File | Format | Loads on MPS? |
|---|---|---|
| ltx-2.3-22b-dev-fp8.safetensors (27 GB) | float8 e4m3fn | No |
| gemma_3_12B_it_fp4_mixed.safetensors (8.8 GB) | fp4 mixed | No |
| gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors | bf16 | Yes |
| ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors | bf16 | Yes |
| ltx-2.3-spatial-upscaler-x2-1.1.safetensors | bf16 | Yes |

PyTorch's MPS backend has no float8 storage type, so the fp8 checkpoint fails at load with an error about `Float8_e4m3fn`. The fp4 text encoder fails the same way. ComfyUI Desktop still offers both in its Missing Models panel with one-click download buttons, on a Mac, where they can never load. Do not use "Download all".

## 2. Route A: bf16 drop-in (works, but does not fit)

Same graph, two files swapped:

| Replace | With | Source | Size |
|---|---|---|---|
| ltx-2.3-22b-dev-fp8.safetensors | ltx-2.3-22b-dev.safetensors | Lightricks/LTX-2.3 | 46.1 GB |
| gemma_3_12B_it_fp4_mixed.safetensors | gemma_3_12B_it.safetensors | Comfy-Org/ltx-2 (split_files/text_encoders) | 24.4 GB |

Only the two loader fields change (`CheckpointLoaderSimple.ckpt_name` and `LTXAVTextEncoderLoader.text_encoder`). Everything loads and pass 1 (half resolution, 8 steps) renders.

Measured on a 27 s clip at 1280x704, 24 fps (655 frames, 82 latent frames):

| Phase | Result |
|---|---|
| Model load + text encode | 8 min (Gemma runs on CPU in fp16, 25 GB) |
| Pass 1 step time | 2.6 min per step (6 to 7 context windows at 23 s each) |
| ComfyUI footprint during pass 1 | 110 GB |
| Pass 2 (full resolution, 3 steps) | Process SIGKILLed by macOS about 60 s into step 1. Swap exhausted. |

The 110 GB is 40 GB transformer + 25 GB text encoder + activations. Nothing survives a SIGKILL; ComfyUI Desktop shows "ComfyUI exited unexpectedly".

## 3. Route B: Q8 GGUF (fits the weights)

Needs the [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) node pack (Gemma 3 text encoders supported since January 2026) and these files:

| File | Folder | Source | Size |
|---|---|---|---|
| ltx-2.3-22b-dev-Q8_0.gguf | models/unet | unsloth/LTX-2.3-GGUF | 22.8 GB |
| ltx-2.3-22b-dev_video_vae.safetensors | models/vae | unsloth/LTX-2.3-GGUF (vae/) | 1.5 GB |
| ltx-2.3-22b-dev_audio_vae.safetensors | models/vae | same | 0.4 GB |
| ltx-2.3-22b-dev_embeddings_connectors.safetensors | models/text_encoders | unsloth/LTX-2.3-GGUF (text_encoders/) | 2.3 GB |
| gemma-3-12b-it-Q8_0.gguf | models/text_encoders | unsloth/gemma-3-12b-it-GGUF | 12.5 GB |

The mmproj file is not needed. The two LoRAs and the upscaler are shared with the stock template.

Node changes versus the stock template (the checkpoint bundled the transformer, both VAEs and the text projections; GGUF splits them):

| Stock node | Replacement |
|---|---|
| CheckpointLoaderSimple (MODEL out) | UnetLoaderGGUF, unet_name = the Q8 gguf |
| CheckpointLoaderSimple (VAE out, 4 consumers) | VAELoader, vae_name = video VAE file |
| LTXVAudioVAELoader (ckpt_name) | VAELoader, vae_name = audio VAE file |
| LTXAVTextEncoderLoader (text_encoder + ckpt_name) | DualCLIPLoaderGGUF, clip_name1 = Gemma gguf, clip_name2 = connectors, type = ltxv |

The stock VAELoader detects both split VAEs correctly when the safetensors metadata is present (it is). unsloth's own reference workflow, embedded in their sample MP4, uses KJNodes' VAELoaderKJ instead; that is not required.

Install notes: the ComfyUI Desktop venv has no pip; use `uv pip install --python <venv>/bin/python gguf sentencepiece protobuf`. Restart ComfyUI after cloning the node pack.

Measured on an 8.3 s clip (first of three 40-word chunks), same resolution:

| Phase | bf16 (27 s clip) | Q8 (8 s clip) |
|---|---|---|
| Queue to first sampler step | 8 min | 95 s |
| Text encoding | ~5 min | 35 s |
| Per context window | 23 s | 32 s (dequantize on the fly) |
| Pass 1 step | 2.6 min | 65 to 90 s (2 to 3 windows) |
| Footprint, pass 1 | 110 GB | 40 GB |
| Footprint, pass 2 step 1 | killed | 95 GB, then 133 GB |

Pass 2 still blew past physical memory even with Q8 and a clip a third the length. The weights were not the problem.

## 4. The actual problem: attention on MPS (three failures, one recipe)

ComfyUI picks an attention implementation at startup. On CUDA that choice is cosmetic because flash attention handles everything. On MPS each option fails differently with LTX-2.3's long sequences, and only one combination works.

**Sequence sizes involved.** One context window of the extend template is 19 latent frames. LTX-2.3's VAE compresses 32x spatially and 8x temporally, so a 1280x704 frame is 40x22 = 880 tokens and a window is about 16,700 tokens at full resolution, about 4,200 at the half-resolution first pass. Attention memory and matmul size scale with the square of that.

| Startup flag | Pass 1 (half res) | Pass 2 (full res) | What happens |
|---|---|---|---|
| `--use-pytorch-cross-attention` (ComfyUI Desktop default on Mac) | 40 GB, fine | 95 to 133 GB, swapping | Falls back to the plain math path for these shapes, which materializes the full score matrix. bf16 was killed by macOS at this point; Q8 only survived because we stopped it. |
| `--use-split-cross-attention` | 37 GB, fine | SIGSEGV | ComfyUI sizes the slices from free memory, and with 120 GB free it chose a single slice. That batched matmul's output exceeds 2^32 elements, which sends PyTorch through its tiled bmm fallback for MPS ([pytorch#133430](https://github.com/pytorch/pytorch/pull/133430)), and that path crashed inside MPSCore (`tiled_bmm_out_mps_impl` in the crash report). |
| `--use-quad-cross-attention` | 37 GB, fine | 50 GB, fine | Sub-quadratic attention caps query chunks at 4096 rows regardless of free memory, so every matmul stays under the 32-bit limit and about 4 GB. Pass 2 adds 13 GB instead of 90. |

Visible symptom of the first row: system memory oscillating between ~45% and ~95% every few seconds during pass 2, as each window's score matrix is allocated and freed. The DGX Spark on CUDA sits flat at 75 to 80% through the same template.

**Then the black video.** With quad attention the first full render completed in 31 minutes, memory flat, and produced an all-black MP4 with an audio encode error (`avcodec_send_frame() returned 22`). Every frame had luma 16 (video black) and the AAC encoder returns that error for NaN samples. Cause: [ComfyUI #15804](https://github.com/Comfy-Org/ComfyUI/issues/15804), open and unfixed upstream as of 2026-10-13. The sub-quadratic code computes scores with `torch.baddbmm(torch.empty(1,1,1,...), q, k, beta=0)`. CUDA ignores the input when beta is 0, as documented; MPS does not, so a NaN bit pattern in the uninitialized buffer poisons the run. It is random per seed, which is why Mac users report "sometimes black".

Two local patches, from that issue, fixed it on the next run:

1. `comfy/ldm/modules/sub_quadratic_attention.py`: the four `torch.empty(1, 1, 1, ...)` buffers become `torch.zeros(1, 1, 1, ...)`.
2. `comfy/model_management.py`, `force_upcast_attention_dtype()`: return `{torch.float16: torch.float32, torch.bfloat16: torch.float32}` instead of fp16 only. (The macOS 14.5+ black-image workaround already existed for fp16; bf16 bypassed it.)

Cost of the patches: pass 1 steps went from 61 s to 69 s, pass 2 footprint from 50 GB to 59 GB. ComfyUI Desktop's Update tab overwrites both files, so keep the sed commands or a backup and re-apply after updates.

**The working recipe on Apple Silicon**

1. Q8 GGUF weights through ComfyUI-GGUF (section 3).
2. Start ComfyUI with `--use-quad-cross-attention`.
3. Apply the two patches above.

Result on the first proven clip: 8.3 s at 1280x704 and 24 fps rendered in 36 minutes, peak footprint 59 GB on a 128 GB machine with nothing else running, correct picture and 48 kHz stereo audio.

Per-step timings on that run: pass 1 about 70 s per step (8 steps), pass 2 about 4 to 6 minutes per step (3 steps), then VAE decode and save.

**Throughput, and the verdict.** About 4 minutes of render per second of finished 1280x704 video, so a 30 second piece is close to two hours and a two minute short is about eight. The same template on a DGX Spark with fp8 and flash attention is roughly ten times faster. On this hardware the setup is a working fallback for short pieces when the CUDA box is unavailable, not a production renderer. A 24 GB Apple Silicon machine cannot run the 22B model at all: the transformer alone is 23 GB at Q8 and 14 GB at Q4, before the text encoder and the attention working set.

## 5. Other Mac-specific settings that mattered

Environment variables on the ComfyUI instance:

- `PYTORCH_ENABLE_MPS_FALLBACK=1`: ops MPS lacks run on CPU instead of erroring.
- `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0`: removes the MPS allocation cap so the full unified memory is usable.

ComfyUI Desktop's Launch Mode can be set to "Console only" for headless use; the web UI stays reachable on the configured port.

## 6. What else holds memory on a 128 GB Mac

Everything below is wired or compressed memory that a render cannot reclaim. Measured with `footprint` and `vm_stat`.

| Process | Cost | Why |
|---|---|---|
| llama.cpp chat server (27B Q8, 256k context, q8 KV cache) | ~45 GB | The mmapped 27 GB weights become Metal buffers and get wired; the KV cache is 10 GB; the rest is Metal scratch. RSS under-reports it as 37 GB. f16 KV cache was 20 GB; `-ctk q8_0 -ctv q8_0` halved it. |
| Docker Desktop VM | its full memory limit (12 GB here) | The Linux guest never returns touched pages; containers inside used under 3 GB. |
| mlx-audio servers (Qwen3-TTS, Whisper), llama.cpp embed and rerank | 2 to 5 GB each | Load once, never unload. Paged out when idle, which adds swap churn. |

Practical rule: stop the chat server and quit Docker before a video render, start them again after. A watcher that stops the chat server when the pipeline enters its video stage worked well.

## 7. Future work

- ComfyUI ships a native Mac app but its templates and Missing Models panel are CUDA-shaped. A backend-aware template set (bf16 or GGUF file names when the backend is MPS, split attention default) is a small change in the upstream templates. Candidate branch: Comfy-Org/ComfyUI, `blueprints/` folder.
- Upstream candidates from this work: the two #15804 patches; and a cap in `attention_split` so the batched matmul output never exceeds 2^31 elements on MPS, which would make split attention usable there too.
- A progress indicator in any pipeline that drives ComfyUI: the server's websocket emits per-step progress; the HTTP history endpoint does not, and it can stall for over 30 s under load, so a poll with a short timeout will falsely fail a run that is still rendering.
