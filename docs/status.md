# Status

Where the render pipeline stands, so work can continue after a disconnect. Update this whenever
something is proven, disproven, or configured. Dates are 2026.

## Proven

| What | Result | Evidence |
|---|---|---|
| LTX-2.3 IA2V, working template, one render | Clean lipsync up to ~10 s of narration; past that: transitions, fades, zooms, panel repainted | manual test 09-15, plain headshot, transition at 13 s |
| LTX-2.3 + `LTXVContextWindows` node (extend template) | One 31 s render, no transition, no fade, lipsync and green panel intact; 7.5 min; peak 101 GB | output/20260916-144437-ltx-extend-test-c4411b20 (one sample) |
| Memory ceiling per LTX render | decode + upscale of the whole clip at the end scales with length: 31 s = 101 GB, 94 s = 112 GB (interrupted) | 09-14 and 09-16 renders |
| Wan2.2 S2V | Holds a 31 s shot with no transitions, memory flat ~65 GB; but 16 fps looks choppy, lipsync looser than LTX, ~45 min per 31 s | output/20260916-013824-wan-test-fc469977 |
| Tracked green-panel composite | Hero follows the panel per frame through pans, push-ins, off-frame corners, fades | 7 of 7 chunks clean on run 20260916-030104 after the fade fix |
| Chatterbox trailing-syllable trim | Removes a burst after a pause at the end of a narration, stream copy | chunk 7 of run 20260916-030104: 9.40 s -> 8.36 s |
| LTX tail fade | Render padded 0.5 s past the narration, cut back after download | applied in the 09-16 extend test |
| Retry | Resumes from artifacts on disk, fetches renders ComfyUI already finished | 09-15 |

## Built, not yet proven

| What | Where | How to test |
|---|---|---|
| Continuation between chunks (each chunk starts from the previous chunk's last frame) | `services/segments.render_chunks_continuous`, toggle `SEGMENT_CONTINUE_FROM_LAST_FRAME` | a real run with the toggle on; look at the cut points; watch for identity drift over many chunks |
| Wan S2V through the app | `services/pipeline.generate_video_stage_wan`, template `video_wan2_2_14B_s2v.json` | set `VIDEO_COMFYUI_WORKFLOW` to the Wan file; slow, parked |
| TTS in one file, two providers (Chatterbox, or OpenAI-compatible vLLM-Omni Qwen3-TTS) (10-03) | `services/tts.py`, toggle `TTS_PROVIDER`; pipeline, preflight, runs, tenant voices, `GET /voices`, system info and `scripts/generate_audio.py` all call it; `services/voices.py` is now unreferenced and still on disk, delete it | openai provider, 10-03: `import api.main` ok; CLI synth with voice `beau` wrote a 2.00 s WAV (duration parses); `/health/deep?voice=beau` on the running app shows `tts_openai` ok. Still to do: an audio stage through the app, whether the trailing-burst trim (still on for both) cuts real Qwen audio, a voice upload from the admin UI, and the Chatterbox provider after the refactor (not exercised at all) |
| Sample player for every shared library voice on the Voices screen (10-03) | `tts.preview`, route `GET /api/v1/library-voices/{name}/sample`, table in `web/src/pages/Voices.tsx`; synthesized per play, passed straight through, nothing stored, `Cache-Control: no-store` | UI build passes; restart uvicorn, open Voices, press play on a shared voice; never run |
| Near-duplicate evidence merge (10-10) | `services/evidence.dedupe_evidence`, toggle `NEWS_CONTEXT_DEDUPE_ENABLED` (default on), `NEWS_CONTEXT_DEDUPE_THRESHOLD` (word-set Jaccard, default 0.6); reranking takes a pool of 2x top_n, merged units carry `also_reported_by`, `prompting.evidence_lines` renders it as "Also reported by", and the attribution rule (user prompt and `prompts/generic-news-reporter.md`) asks for one statement crediting the outlets together | offline check on the 10-10 run's sources.json; never run through a profile. Watch whether the writer still repeats a story covered by two outlets, and whether 0.6 merges too much or too little |
| Story-level dedupe at article selection (10-10) | `services/evidence.same_story` (cosine on stopword-free term frequencies of title+text), toggle `NEWS_STORY_DEDUPE_ENABLED` (default on), `NEWS_STORY_DEDUPE_THRESHOLD` (default 0.35); the selection loop in `pipeline.run_search_stage` keeps walking the ranked pool until `max_articles` distinct stories are found; same-story articles land in `duplicate_sources` with `duplicate_of`, the keeper gets `also_reported_by`, their sentences still become evidence tagged `story`, `prompting.evidence_lines` renders `Story: N`, the rule says tell it once and credit the outlets together; Profiles hint says stories | offline on the 10-10 run's 5 articles: wsvn, yahoo and abc7news clustered as one story (0.60 / 0.64), the other two stayed apart (max 0.21); `import api.main` ok; never run through a profile. Watch the extract log for `same story as ranked N` lines and whether 0.35 over- or under-merges |

## Current configuration (.env)

| Key | Value | Why |
|---|---|---|
| VIDEO_COMFYUI_WORKFLOW | `comfyui-workflows/video_ltx2_3_ia2v-extend.json` | context windows; `-working.json` is the untouched original |
| SEGMENT_MAX_WORDS | 80 | ~31 s chunks, the memory ceiling per render on the extend template |
| SEGMENT_CONTINUE_FROM_LAST_FRAME | true | under test: first real run pending |
| VIDEO_LTX_TAIL_PAD_SECONDS | 0.5 | tail fade removal |
| HERO_COMFYUI_WORKFLOW | image_qwen_Image_2512.json | ~2 min per hero; the turbo model drew gibberish text into heroes |
| TTS_PROVIDER | `openai` | Qwen3-TTS on vLLM-Omni; `chatterbox` (or unset) goes back to `CHATTERBOX_*` |
| NEWS_CONTEXT_DEDUPE_ENABLED | unset (on) | near-duplicate evidence merge, 10-10 |
| NEWS_CONTEXT_DEDUPE_THRESHOLD | unset (0.6) | Jaccard on word sets |
| NEWS_STORY_DEDUPE_ENABLED | unset (on) | story-level dedupe at article selection, 10-10 |
| NEWS_STORY_DEDUPE_THRESHOLD | unset (0.35) | cosine, same-story pairs measured 0.45-0.64, different 0.06-0.21 |
| `TTS_OPENAI_BASE_URL`, `TTS_OPENAI_MODEL`, `TTS_OPENAI_API_KEY`, `TTS_OPENAI_TIMEOUT_SECONDS` | `http://<your-tts-host>:8091/v1` / `Qwen/Qwen3-TTS-12Hz-1.7B-Base` / `local` / 360 | voice `beau` is already in that server's library; tenant voices uploaded to Chatterbox are not, re-upload them |

Templates: `video_ltx2_3_ia2v-working.json` (untouched, proven), `video_ltx2_3_ia2v-extend.json` (working + one
`LTXVContextWindows` node between the LoRA loader and both CFG guiders: 145-frame windows, 40 overlap,
pyramid fuse, freenoise, retain first frame), `video_wan2_2_14B_s2v.json` (Wan, placeholders in, chain fitted at run time).

## Next

1. Switch to the extend template, 80 words, continuation on; run a profile; judge the cut points and drift.
2. If joins drift: reset to the anchor at story boundaries (new copy of the continuity loop).
3. Raise the per-render ceiling: tiled/temporal VAE decode on the extend copy; test creeping up (45 s, 60 s) with the memory guard armed. Failure mode is an OOM that strands the DGX, so never jump straight to long clips.
4. Confirm the context-windows result on more than one sample.
5. Per-family render cap and the expansion items in docs/expansions.md.

## Rules in force

One change at a time, proven with a run. New behavior as copies beside working code, behind toggles
(see `render_chunks_continuous`, `generate_video_stage_wan`, the `-extend` template). The working LTX
template is never edited. Memory guard: interrupt ComfyUI above 110 GB.
