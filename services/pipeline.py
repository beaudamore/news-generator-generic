"""Shared news video generation pipeline."""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import avatars, evidence, extract, llm, preflight, prompting, prompts, segments, tts  # noqa: E402
from scripts import generate_script, generate_video_comfyui, search_searxng  # noqa: E402


@dataclass
class PipelineError(Exception):
    stage: str
    message: str

    def __str__(self) -> str:
        return self.message


def load_env() -> None:
    search_searxng.load_env_file(PROJECT_ROOT / ".env")


def output_root() -> Path:
    root = Path(os.environ.get("NEWS_OUTPUT_DIRECTORY", "output")).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "run"


def create_run_directory(search_text: str, root: Optional[Path] = None) -> tuple[str, Path]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"{timestamp}-{slugify(search_text)[:64]}-{uuid.uuid4().hex[:8]}"
    run_dir = (root or output_root()) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_id, run_dir


def trim_video_to_audio(run_dir: Path, video_path: Path, audio_seconds: float) -> Path:
    """Cut a segment-padded clip (Wan renders whole 4.8 s segments) down to the narration length."""
    actual = read_video_duration(video_path)
    if actual is None or actual <= audio_seconds + 0.15 or not shutil.which("ffmpeg"):
        return video_path
    raw_path = video_path.with_name(video_path.stem + "-untrimmed.mp4")
    video_path.replace(raw_path)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw_path), "-t", f"{audio_seconds:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(video_path)], check=True)
    raw_path.unlink(missing_ok=True)
    log_event(run_dir, "video", f"trimmed {actual:.2f}s render to the {audio_seconds:.2f}s narration")
    return video_path


def _silences(path: Path, noise_db: int = -40, min_seconds: float = 0.3) -> list[tuple[float, float]]:
    """(start, end) of every silence in an audio file, from ffmpeg silencedetect."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", f"silencedetect=noise={noise_db}dB:d={min_seconds}", "-f", "null", "-"], capture_output=True, text=True).stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([0-9.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end: ([0-9.]+)", out)]
    return list(zip(starts, ends))


def trim_trailing_burst(run_dir: Path, path: Path, min_pause: float = 0.4, max_burst: float = 0.6) -> Optional[float]:
    """Cut a hallucinated syllable off the end of a Chatterbox narration.

    The artifact has one shape: real speech, a pause of at least `min_pause`, a burst shorter than
    `max_burst`, then the end of the file. When the file ends that way it is cut at the start of the
    pause with a stream copy (no re-encode). Returns the new duration, or None when nothing was cut.
    """
    if not shutil.which("ffmpeg"):
        return None
    duration = generate_video_comfyui.wav_duration_seconds(path)
    if not duration:
        return None
    silences = _silences(path)
    if not silences:
        return None
    # non-silent intervals
    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in silences:
        if start - cursor > 0.05:
            speech.append((cursor, start))
        cursor = end
    if duration - cursor > 0.05:
        speech.append((cursor, duration))
    if len(speech) < 2:
        return None
    last_start, last_end = speech[-1]
    pause = next(((s, e) for s, e in silences if abs(e - last_start) < 0.02), None)
    if pause is None or (last_end - last_start) >= max_burst or (pause[1] - pause[0]) < min_pause:
        return None
    cut_at = pause[0] + 0.1
    raw = path.with_name(path.stem + "-untrimmed.wav")
    path.replace(raw)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-t", f"{cut_at:.3f}", "-c", "copy", str(path)], check=True)
    raw.unlink(missing_ok=True)
    new_duration = generate_video_comfyui.wav_duration_seconds(path) or cut_at
    log_event(run_dir, "audio", f"trailing burst removed: {last_end - last_start:.2f}s sound after a {pause[1] - pause[0]:.2f}s pause; cut at {cut_at:.2f}s (was {duration:.2f}s)")
    return new_duration


def read_video_duration(path: Path) -> Optional[float]:
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            text=True,
        )
        data = json.loads(result)
        duration = data.get("format", {}).get("duration")
        return float(duration) if duration is not None else None
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None


LOGGER = logging.getLogger("news_pipeline")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def log_event(run_dir: Path, stage: str, message: str) -> None:
    """Append one line to <run_dir>/run.log and mirror it to the process logger."""
    LOGGER.info("[%s] %s", stage, message, extra={"stage": stage, "run_id": run_dir.name, "event": "run"})
    try:
        with (run_dir / "run.log").open("a", encoding="utf-8") as log_file:
            log_file.write(f"{datetime.now(timezone.utc).isoformat()} [{stage}] {message}\n")
    except OSError:
        pass


def target_video_dimensions() -> tuple[int, int]:
    width = int(os.environ.get("VIDEO_COMFYUI_WIDTH", "1280"))
    height = int(os.environ.get("VIDEO_COMFYUI_HEIGHT", "720"))
    if width <= 0 or height <= 0:
        raise RuntimeError("VIDEO_COMFYUI_WIDTH and VIDEO_COMFYUI_HEIGHT must be greater than zero.")
    if width * 9 != height * 16:
        LOGGER.warning("VIDEO_COMFYUI_WIDTH x VIDEO_COMFYUI_HEIGHT is %dx%d, not 16:9; rendering as configured", width, height)
    return width, height


def prepare_anchor_image(anchor_path: Path, run_dir: Path, width: int, height: int) -> Path:
    """Center-crop the anchor to the target aspect and resize it to width x height.

    The video workflow copies the anchor dimensions into its latent size, so the
    anchor must already be the exact size the final video should have.
    """
    with Image.open(anchor_path) as source:
        image = source.convert("RGB")
    source_width, source_height = image.size
    if (source_width, source_height) == (width, height):
        return anchor_path
    target_ratio = width / height
    source_ratio = source_width / source_height
    if source_ratio > target_ratio:
        crop_width = round(source_height * target_ratio)
        left = (source_width - crop_width) // 2
        image = image.crop((left, 0, left + crop_width, source_height))
    elif source_ratio < target_ratio:
        crop_height = round(source_width / target_ratio)
        top = (source_height - crop_height) // 2
        image = image.crop((0, top, source_width, top + crop_height))
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    prepared_path = run_dir / f"{anchor_path.stem}-{width}x{height}.png"
    image.save(prepared_path, format="PNG", optimize=True)
    return prepared_path


def probe_video_dimensions(path: Path) -> Optional[tuple[int, int]]:
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(path)],
            text=True,
        )
        stream = (json.loads(result).get("streams") or [{}])[0]
        return int(stream["width"]), int(stream["height"])
    except (subprocess.SubprocessError, ValueError, KeyError, IndexError, json.JSONDecodeError):
        return None


def normalize_video_dimensions(run_dir: Path, video_path: Path, width: int, height: int) -> Path:
    """Guarantee the final video is exactly width x height; re-encode with ffmpeg if ComfyUI rounded."""
    actual = probe_video_dimensions(video_path)
    if actual is None:
        log_event(run_dir, "video", "ffprobe unavailable; final video size not verified")
        return video_path
    if actual == (width, height):
        log_event(run_dir, "video", f"ComfyUI output already {width}x{height}; no re-encode needed")
        return video_path
    if not shutil.which("ffmpeg"):
        log_event(run_dir, "video", f"WARNING ComfyUI output is {actual[0]}x{actual[1]} and ffmpeg is unavailable to normalize it")
        return video_path
    raw_path = video_path.with_name("video-raw.mp4")
    video_path.replace(raw_path)
    log_event(run_dir, "video", f"ComfyUI output is {actual[0]}x{actual[1]}; re-encoding {raw_path.name} to {width}x{height}")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(raw_path),
            "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(video_path),
        ],
        check=True,
    )
    return video_path


def system_exit_message(exc: SystemExit) -> str:
    return str(exc.code) if exc.code else "Pipeline stage exited."


def artifact_path(run_id: str, artifact: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", run_id):
        raise ValueError("Invalid run_id.")
    files = {
        "video": "video.mp4",
        "audio": "narration.wav",
        "script": "narration.txt",
        "report": "report.md",
        "sources": "sources.json",
    }
    if artifact not in files:
        raise ValueError("Invalid artifact.")
    path = output_root() / run_id / files[artifact]
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def generate_script_stage(
    *,
    run_id: str,
    run_dir: Path,
    search_text: str,
    script_text: Optional[str] = None,
    categories: Optional[str] = None,
    language: Optional[str] = None,
    safesearch: Optional[str] = None,
    max_articles: Optional[int] = None,
    max_age_days: Optional[int] = None,
    time_range: Optional[str] = None,
    search_timeout_seconds: Optional[int] = None,
    rerank_top_n: Optional[int] = None,
    rank_query: Optional[str] = None,
    rank_candidates: Optional[int] = None,
    summarize_prompt: Optional[str] = None,
    intended_video_max_seconds: Optional[float] = None,
    wpm: Optional[float] = None,
) -> dict:
    sources_path = run_dir / "sources.json"
    script_path = run_dir / "narration.txt"

    provided_script = script_text.strip() if script_text else ""
    if provided_script:
        source_bundle = {
            "subject": search_text or "provided script",
            "search_text": search_text,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "provided-script",
            "raw_result_count": 0,
            "selected_count": 0,
            "selected_sources": [],
            "suggestions": [],
            "corrections": [],
        }
        selected_sources = []
        script = re.sub(r"\s+", " ", provided_script).strip()
        word_count = generate_script.word_count(script)
        target_words = None
        log_event(run_dir, "script", f"using provided script ({word_count} words); search skipped")
        search_searxng.write_bundle(sources_path, source_bundle)
        script_path.write_text(script + "\n", encoding="utf-8")
        generate_script.write_report(script_path, source_bundle, script, "provided-script")
    else:
        try:
            search_args = SimpleNamespace(
                categories=categories,
                language=language,
                safesearch=safesearch,
                max_articles=max_articles if max_articles is not None else int(os.environ.get("SEARXNG_DEFAULT_MAX_ARTICLES", "5")),
                max_age_days=max_age_days if max_age_days is not None else int(os.environ.get("SEARXNG_DEFAULT_MAX_AGE_DAYS", "3")),
                time_range=time_range or os.environ.get("SEARXNG_DEFAULT_TIME_RANGE") or None,
                timeout=search_timeout_seconds if search_timeout_seconds is not None else int(os.environ.get("SEARXNG_DEFAULT_TIMEOUT_SECONDS", "20")),
                rank_query=rank_query,
                rank_candidates=rank_candidates,
            )
            log_event(run_dir, "search", f"query={search_text!r} max_articles={search_args.max_articles} max_age_days={search_args.max_age_days} time_range={search_args.time_range or 'none'}")
            search_result = search_searxng.search_searxng(search_text, search_args)
            log_event(run_dir, "search", f"request_url={search_result['request_url']} status={search_result['status_code']}")
            search_data = search_result["data"]
            raw_results = search_data.get("results") or []
            ranked_sources, source_rank = search_searxng.select_and_rank_sources(raw_results, search_args, limit=False)
            wanted = max(1, int(search_args.max_articles or 1))
            log_event(run_dir, "search", f"raw_results={len(raw_results)} candidates={source_rank.get('candidate_count')} ranked={len(ranked_sources)} wanted={wanted} source_rank={json.dumps(source_rank, default=str)}")
            for position, source in enumerate(ranked_sources, 1):
                source["rank_position"] = position
                score = source.get("source_rank_score")
                score_text = "" if score is None else f" score={score:.4f}"
                log_event(run_dir, "search", f"ranked {position}:{score_text} {source.get('published_at') or 'undated'} {source.get('source')} | {source.get('title')} | {source.get('url')}")
            # The profile's article count means usable articles on distinct stories. Walk down the
            # ranked pool, extracting a batch at a time, until that many have real text or the pool
            # is spent. An article on a story already selected (evidence.same_story) is kept as a
            # duplicate: it does not count, its outlet is credited on the keeper, its text still
            # feeds the evidence. Toggle NEWS_STORY_DEDUPE_ENABLED.
            selected_sources, rejected_sources, duplicate_sources = [], [], []
            story_dedupe = evidence.story_enabled()
            story_threshold = evidence.story_threshold()
            extraction = {"enabled": extract.enabled(), "extracted": 0, "fallback": 0, "words": 0, "duplicates": 0}
            cursor = 0
            while len(selected_sources) < wanted and cursor < len(ranked_sources):
                batch = ranked_sources[cursor : cursor + (wanted - len(selected_sources))]
                cursor += len(batch)
                extract.extract_sources(batch, None, log=None)
                for source in batch:
                    record = source.get("extraction") or {}
                    status = record.get("status") or "none"
                    words = int(record.get("words") or 0)
                    usable = status == "extracted" if extraction["enabled"] else bool(source.get("snippet"))
                    match = evidence.same_story(source, selected_sources, story_threshold) if usable and story_dedupe and selected_sources else None
                    if match is not None:
                        keeper = selected_sources[match[0]]
                        keeper.setdefault("also_reported_by", []).append(
                            {"source": source.get("source") or "", "title": source.get("title") or "", "url": source.get("url") or "", "similarity": round(match[1], 3)}
                        )
                        source["duplicate_of"] = keeper.get("url") or ""
                        source["story_similarity"] = round(match[1], 3)
                        duplicate_sources.append(source)
                        extraction["duplicates"] += 1
                        log_event(run_dir, "extract", f"ranked {source['rank_position']}: {status} words={words} {record.get('elapsed_ms', '?')} ms {source.get('source')} -> same story as ranked {keeper.get('rank_position')} ({keeper.get('source')}), similarity {match[1]:.2f}; credited there, not counted ({len(selected_sources)}/{wanted})")
                        continue
                    if usable and len(selected_sources) < wanted:
                        source["_story_vector"] = evidence.story_vector(source) if story_dedupe else None
                        selected_sources.append(source)
                        if status == "extracted":
                            extraction["extracted"] += 1
                            extraction["words"] += words
                        else:
                            extraction["fallback"] += 1
                        log_event(run_dir, "extract", f"ranked {source['rank_position']}: {status} words={words} {record.get('elapsed_ms', '?')} ms {source.get('source')} -> usable ({len(selected_sources)}/{wanted})")
                    else:
                        source["rejected"] = status if status != "none" else "no text"
                        rejected_sources.append(source)
                        error = f" | {record['error']}" if record.get("error") else ""
                        log_event(run_dir, "extract", f"ranked {source['rank_position']}: {status} words={words} {record.get('elapsed_ms', '?')} ms {source.get('source')}{error} -> rejected, trying next ranked source")
            for source in selected_sources:
                source.pop("_story_vector", None)
            for position, source in enumerate(selected_sources, 1):
                if source.get("extraction", {}).get("status") == "extracted" and source.get("content"):
                    folder = run_dir / "articles"
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / f"{position:02d}.md").write_text(f"# {source.get('title') or ''}\n\n{source.get('url') or ''}\n\n{source['content']}\n", encoding="utf-8")
            log_event(run_dir, "extract", f"enabled={extraction['enabled']} stories={len(selected_sources)} of wanted={wanted} same_story_duplicates={len(duplicate_sources)} tried={cursor} of ranked={len(ranked_sources)} rejected={len(rejected_sources)} extracted={extraction['extracted']} fallback_to_snippet={extraction['fallback']} words={extraction['words']} story_dedupe={'on' if story_dedupe else 'off'} threshold={story_threshold}")
            if selected_sources and len(selected_sources) < wanted:
                log_event(run_dir, "search", f"WARNING only {len(selected_sources)} usable source(s) of the {wanted} the profile asks for; the ranked pool of {len(ranked_sources)} is exhausted")
            source_bundle = {
                "subject": search_text,
                "search_text": search_text,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "request_url": search_result["request_url"],
                "content_type": search_result["content_type"],
                "categories": search_args.categories or os.environ.get("SEARXNG_DEFAULT_CATEGORIES", ""),
                "language": search_args.language or os.environ.get("SEARXNG_DEFAULT_LANGUAGE", ""),
                "safesearch": search_args.safesearch or os.environ.get("SEARXNG_DEFAULT_SAFESEARCH", ""),
                "time_range": search_args.time_range or "",
                "max_articles": search_args.max_articles,
                "max_article_age_days": search_args.max_age_days,
                "raw_result_count": len(raw_results),
                "candidate_count": source_rank.get("candidate_count", len(selected_sources)),
                "source_rank": source_rank,
                "extraction": extraction,
                "wanted_count": wanted,
                "tried_count": cursor,
                "selected_count": len(selected_sources),
                "selected_sources": selected_sources,
                "duplicate_sources": duplicate_sources,
                "rejected_sources": rejected_sources,
                "suggestions": search_data.get("suggestions") or [],
                "corrections": search_data.get("corrections") or [],
            }
            source_bundle = evidence.enrich_source_bundle(source_bundle, rerank_top_n)
            evidence_context = source_bundle.get("evidence_context") or {}
            log_event(run_dir, "evidence", f"sentences={evidence_context.get('candidate_count')} kept={evidence_context.get('selected_count')} rerank={json.dumps(evidence_context.get('rerank'), default=str)} dedupe={json.dumps({k: v for k, v in (evidence_context.get('dedupe') or {}).items() if k != 'merged'}, default=str)} merged={json.dumps((evidence_context.get('dedupe') or {}).get('merged') or [], default=str)}")
            search_searxng.write_bundle(sources_path, source_bundle)
            log_event(run_dir, "search", f"wrote {sources_path.name}")
            # Never generate a news script with nothing to ground it. The LLM
            # will happily write confident, plausible copy from the search text
            # alone, which is fabrication rather than reporting.
            if not selected_sources and ranked_sources:
                raise PipelineError(
                    "search",
                    f"{cursor} ranked source(s) tried for {search_text!r} and none had usable article text "
                    f"(rejected: {', '.join(sorted({str(s['rejected']) for s in rejected_sources}))}). "
                    "Change the categories or widen the search; refusing to generate an ungrounded script.",
                )
            if not selected_sources:
                raise PipelineError(
                    "search",
                    f"Search returned no usable sources for {search_text!r} "
                    f"({len(raw_results)} raw results, max_age_days="
                    f"{search_args.max_age_days}, time_range="
                    f"{source_bundle.get('time_range') or 'none'}). "
                    "Widen the search window or loosen the filters; refusing to "
                    "generate an ungrounded script.",
                )
        except PipelineError as exc:
            log_event(run_dir, "search", f"ERROR {exc.message}")
            raise
        except SystemExit as exc:
            log_event(run_dir, "search", f"ERROR {system_exit_message(exc)}")
            raise PipelineError("search", system_exit_message(exc)) from exc
        except Exception as exc:
            log_event(run_dir, "search", f"ERROR {type(exc).__name__}: {exc}")
            raise PipelineError("search", str(exc)) from exc

        try:
            script_args = SimpleNamespace(
                summarize_prompt=summarize_prompt,
                summarize_prompt_file=None,
                intended_video_max_seconds=intended_video_max_seconds,
                wpm=wpm if wpm is not None else float(os.environ.get("NEWS_DEFAULT_WPM", "145")),
                timeout=int(os.environ.get("OPENAI_DEFAULT_TIMEOUT_SECONDS", "90")),
                categories=categories,
                language=language,
                safesearch=safesearch,
                time_range=time_range,
                max_articles=max_articles,
                max_age_days=max_age_days,
            )
            prompt = generate_script.resolve_summarize_prompt(script_args)
            intended_seconds = generate_script.resolve_intended_video_max_seconds(script_args)
            prompt, target_words = generate_script.append_timing_guidance(prompt, intended_seconds, script_args.wpm)
            prompt = prompting.render_prompt_variables(
                prompt,
                prompting.prompt_variables(source_bundle, script_args, intended_seconds, target_words),
            )
            system_prompt, user_prompt = prompting.build_prompt(source_bundle, prompt)
            # Never let the server truncate the prompt: trim the lowest-ranked evidence until it fits the context budget.
            context = llm.context_info()
            token_budget = llm.budget(context)
            units = (source_bundle.get("evidence_context") or {}).get("evidence_units") or []
            dropped = 0
            while llm.estimate_tokens(system_prompt + user_prompt) > token_budget and len(units) > 1:
                units.pop()
                dropped += 1
                source_bundle["evidence_context"]["evidence_units"] = units
                system_prompt, user_prompt = prompting.build_prompt(source_bundle, prompt)
            log_event(run_dir, "script", f"llm context: {llm.describe(context)} budget={token_budget} prompt_tokens~{llm.estimate_tokens(system_prompt + user_prompt)} evidence_dropped={dropped}")
            log_event(run_dir, "script", f"calling LLM model={os.environ.get('OPENAI_MODEL', '')!r} base_url={os.environ.get('OPENAI_BASE_URL', '')} target_words={target_words} intended_seconds={intended_seconds} system_chars={len(system_prompt)} user_chars={len(user_prompt)}")
            script = generate_script.call_openai_compatible(system_prompt, user_prompt, script_args.timeout)
            loaded = llm.loaded_context_after_call()
            if loaded is not None:
                log_event(run_dir, "script", f"llm loaded context after call: {loaded}" + (" (below requested; server default wins)" if context.get('requested') and loaded < context['requested'] else ""))
            if not script:
                raise RuntimeError("OpenAI-compatible script generation returned no script.")
            script = re.sub(r"\s+", " ", script).strip()
            word_count = generate_script.word_count(script)
            script_path.write_text(script + "\n", encoding="utf-8")
            generate_script.write_report(script_path, source_bundle, script, "openai-compatible")
            log_event(run_dir, "script", f"script written ({word_count} words) to {script_path.name}; report.md written")
        except SystemExit as exc:
            log_event(run_dir, "script", f"ERROR {system_exit_message(exc)}")
            raise PipelineError("script", system_exit_message(exc)) from exc
        except Exception as exc:
            log_event(run_dir, "script", f"ERROR {type(exc).__name__}: {exc}")
            raise PipelineError("script", str(exc)) from exc

    return {
        "status": "success",
        "run_id": run_id,
        "run_directory": str(run_dir),
        "script_url": f"/runs/{run_id}/script",
        "report_url": f"/runs/{run_id}/report",
        "sources_url": f"/runs/{run_id}/sources",
        "script": script,
        "word_count": word_count,
        "target_words": target_words,
        "selected_sources": selected_sources,
    }


def generate_audio_stage(
    *,
    run_id: str,
    run_dir: Path,
    script: str,
    voice: str,
    voice_sample_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> dict:
    narration_path = output_path or (run_dir / "narration.wav")
    narration_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        tts_url = tts.base_url()
        audio_timeout = tts.default_timeout()
        log_event(run_dir, "audio", f"{tts.service_name()}={tts_url} voice={voice!r} sample={voice_sample_path.name if voice_sample_path else 'none'} script_chars={len(script)}")
        if voice_sample_path is not None:
            voice_mode = tts.replace_voice_sample(voice, voice_sample_path, min(audio_timeout, 30))
            resolved_voice = voice
        else:
            resolved_voice, voice_mode = tts.resolve_voice(voice, min(audio_timeout, 10))
        log_event(run_dir, "audio", f"resolved voice={resolved_voice!r} mode={voice_mode}; generating narration")
        audio_result = tts.synthesize(script, narration_path, resolved_voice, voice_mode, audio_timeout)
        audio_duration = audio_result["duration_seconds"]
        log_event(run_dir, "audio", f"narration written to {narration_path.name} duration={audio_duration:.2f}s bytes={narration_path.stat().st_size}")
        trimmed = trim_trailing_burst(run_dir, narration_path)
        if trimmed:
            audio_duration = trimmed
    except SystemExit as exc:
        log_event(run_dir, "audio", f"ERROR {system_exit_message(exc)}")
        raise PipelineError("audio", system_exit_message(exc)) from exc
    except Exception as exc:
        log_event(run_dir, "audio", f"ERROR {type(exc).__name__}: {exc}")
        raise PipelineError("audio", str(exc)) from exc

    return {
        "status": "success",
        "run_id": run_id,
        "run_directory": str(run_dir),
        "audio_url": f"/runs/{run_id}/audio",
        "audio_duration_seconds": audio_duration,
        "audio_size_bytes": narration_path.stat().st_size,
        "voice": resolved_voice,
        "voice_mode": voice_mode,
    }


def _configured_video_family() -> str:
    """Family of the template named by VIDEO_COMFYUI_WORKFLOW, or "ltx" when it cannot be read (the LTX stage then reports why)."""
    value = os.environ.get("VIDEO_COMFYUI_WORKFLOW", "").strip()
    if not value:
        return "ltx"
    path = generate_video_comfyui.resolve_project_path(value)
    if not path.is_file():
        return "ltx"
    try:
        return generate_video_comfyui.workflow_family(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return "ltx"


def generate_video_stage_wan(
    *,
    run_id: str,
    run_dir: Path,
    script: str,
    anchor_path: Path,
    narration_path: Path,
    video_prompt: Optional[str] = None,
    output_dir: Optional[Path] = None,
    on_queued: Optional[Callable[[str], None]] = None,
) -> dict:
    """Video stage for the Wan2.2 S2V template. Separate from the LTX stage on purpose.

    Wan renders 77-frame segments at 16 fps and continues each from the last, so the Extend chain
    in the template is fitted to the narration length; the prompt is the plain visual description
    (no LTX [VISUAL]/[SPEECH]/[SOUNDS] wrapper); the output is trimmed to the narration.
    """
    output_dir = output_dir or run_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = output_dir / "video.mp4"
    try:
        comfyui_url = os.environ.get("VIDEO_COMFYUI_BASE_URL", "").strip()
        if not comfyui_url:
            raise RuntimeError("Set VIDEO_COMFYUI_BASE_URL.")
        video_timeout = int(os.environ.get("VIDEO_COMFYUI_TIMEOUT_SECONDS", "1800"))
        workflow_path = generate_video_comfyui.resolve_project_path(os.environ.get("VIDEO_COMFYUI_WORKFLOW", "").strip())
        output_prefix_root = os.environ.get("VIDEO_COMFYUI_OUTPUT_PREFIX", "").strip()
        if not output_prefix_root:
            raise RuntimeError("Set VIDEO_COMFYUI_OUTPUT_PREFIX.")
        visual_prompt = video_prompt.strip() if video_prompt else generate_video_comfyui.read_env_text("NEWS_VIDEO_PROMPT", "NEWS_VIDEO_PROMPT_FILE")
        if not visual_prompt:
            raise RuntimeError("Provide video_prompt or NEWS_VIDEO_PROMPT/NEWS_VIDEO_PROMPT_FILE.")
        settings = generate_video_comfyui.family_settings("wan_s2v")
        width, height, fps = settings["width"], settings["height"], settings["fps"]
        log_event(run_dir, "video", f"comfyui={comfyui_url} workflow={workflow_path.name} family=wan_s2v timeout={video_timeout}s prompt_chars={len(visual_prompt)}")
        if os.environ.get("VIDEO_COMFYUI_FREE_BEFORE_RUN", "").strip().lower() in ("1", "true", "yes"):
            freed = generate_video_comfyui.free_memory(comfyui_url, min(video_timeout, 60))
            log_event(run_dir, "video", f"free_memory requested: {'ok' if freed else 'failed'}")
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        workflow_check = generate_video_comfyui.validate_wan_s2v_workflow(workflow, workflow_path.name)
        log_event(run_dir, "video", f"workflow validated: {workflow_check}")
        source_width, source_height = generate_video_comfyui.image_dimensions(anchor_path)
        log_event(run_dir, "video", f"anchor {anchor_path.name} is {source_width}x{source_height}; target video size {width}x{height}")
        anchor_path = prepare_anchor_image(anchor_path, run_dir, width, height)
        video_audio_duration = generate_video_comfyui.wav_duration_seconds(narration_path)
        if video_audio_duration is None:
            raise RuntimeError(f"Could not read WAV duration from {narration_path}.")
        uploaded_image_name = generate_video_comfyui.upload_input_file(comfyui_url, anchor_path, min(video_timeout, 120))
        uploaded_audio_name = generate_video_comfyui.upload_input_file(comfyui_url, narration_path, min(video_timeout, 120))
        log_event(run_dir, "video", f"uploaded image={uploaded_image_name} audio={uploaded_audio_name} audio_duration={video_audio_duration:.2f}s")
        workflow, chain = generate_video_comfyui.fit_wan_s2v_chain(workflow, video_audio_duration, fps)
        log_event(run_dir, "video", f"wan s2v chain: {chain['segments']} segment(s) of {chain['frames_per_segment']} frames ({chain['seconds_per_segment']}s) at {fps} fps covers {chain['covers_seconds']}s for {video_audio_duration:.2f}s of narration (export had {chain['exported_segments']})")
        patched_workflow = generate_video_comfyui.apply_workflow_placeholders(
            workflow,
            image_name=uploaded_image_name,
            audio_name=uploaded_audio_name,
            prompt=visual_prompt,
            width=width,
            height=height,
            fps=fps,
            duration_seconds=video_audio_duration,
            seed=int(datetime.now(timezone.utc).timestamp()),
            output_prefix=f"{output_prefix_root.rstrip('/')}/{run_id}",
            prompt_enhance=False,
        )
        prompt_id = generate_video_comfyui.queue_prompt(comfyui_url, patched_workflow, min(video_timeout, 120))
        log_event(run_dir, "video", f"queued prompt_id={prompt_id} family=wan_s2v fps={fps} size={width}x{height}; waiting for ComfyUI")
        if on_queued:
            on_queued(prompt_id)
        history = generate_video_comfyui.wait_for_history(comfyui_url, prompt_id, video_timeout)
        media = generate_video_comfyui.find_media_output(history)
        log_event(run_dir, "video", f"ComfyUI finished; output={media.get('subfolder', '')}/{media.get('filename')}")
        generate_video_comfyui.download_media(comfyui_url, media, video_path, min(video_timeout, 300))
        log_event(run_dir, "video", f"downloaded to {video_path.name} bytes={video_path.stat().st_size}")
        video_path = normalize_video_dimensions(run_dir, video_path, width, height)
        video_path = trim_video_to_audio(run_dir, video_path, video_audio_duration)
        video_duration = read_video_duration(video_path)
        final_size = probe_video_dimensions(video_path)
        log_event(run_dir, "video", f"final {video_path.name} size={final_size[0]}x{final_size[1] if final_size else '?'} duration={video_duration} bytes={video_path.stat().st_size}" if final_size else f"final {video_path.name} duration={video_duration} bytes={video_path.stat().st_size}")
    except SystemExit as exc:
        log_event(run_dir, "video", f"ERROR {system_exit_message(exc)}")
        raise PipelineError("video", system_exit_message(exc)) from exc
    except Exception as exc:
        log_event(run_dir, "video", f"ERROR {type(exc).__name__}: {exc}")
        raise PipelineError("video", str(exc)) from exc
    return {
        "status": "success",
        "run_id": run_id,
        "run_directory": str(run_dir),
        "video_url": f"/runs/{run_id}/video",
        "video_duration_seconds": video_duration,
        "video_size_bytes": video_path.stat().st_size,
        "video_width": final_size[0] if final_size else width,
        "video_height": final_size[1] if final_size else height,
        "segments": chain["segments"],
    }


def generate_video_stage(
    *,
    run_id: str,
    run_dir: Path,
    script: str,
    anchor_path: Path,
    narration_path: Path,
    video_prompt: Optional[str] = None,
    video_sound_prompt: Optional[str] = None,
    prompt_enhance: Optional[bool] = None,
    output_dir: Optional[Path] = None,
    on_queued: Optional[Callable[[str], None]] = None,
) -> dict:
    if _configured_video_family() == "wan_s2v":
        return generate_video_stage_wan(run_id=run_id, run_dir=run_dir, script=script, anchor_path=anchor_path, narration_path=narration_path, video_prompt=video_prompt, output_dir=output_dir, on_queued=on_queued)
    output_dir = output_dir or run_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = output_dir / "video.mp4"
    if prompt_enhance is None:
        prompt_enhance = search_searxng.env_flag("VIDEO_COMFYUI_PROMPT_ENHANCE", False)

    try:
        comfyui_url = os.environ.get("VIDEO_COMFYUI_BASE_URL", "").strip()
        if not comfyui_url:
            raise RuntimeError("Set VIDEO_COMFYUI_BASE_URL.")
        video_timeout = int(os.environ.get("VIDEO_COMFYUI_TIMEOUT_SECONDS", "1800"))
        workflow_value = os.environ.get("VIDEO_COMFYUI_WORKFLOW", "").strip()
        if not workflow_value:
            raise RuntimeError("Set VIDEO_COMFYUI_WORKFLOW.")
        workflow_path = generate_video_comfyui.resolve_project_path(workflow_value)
        output_prefix_root = os.environ.get("VIDEO_COMFYUI_OUTPUT_PREFIX", "").strip()
        if not output_prefix_root:
            raise RuntimeError("Set VIDEO_COMFYUI_OUTPUT_PREFIX.")

        visual_prompt = video_prompt.strip() if video_prompt else generate_video_comfyui.read_env_text("NEWS_VIDEO_PROMPT", "NEWS_VIDEO_PROMPT_FILE")
        if not visual_prompt:
            raise RuntimeError("Provide video_prompt or NEWS_VIDEO_PROMPT/NEWS_VIDEO_PROMPT_FILE.")
        sound_prompt = video_sound_prompt.strip() if video_sound_prompt else os.environ.get("NEWS_VIDEO_SOUND_PROMPT", "").strip()
        workflow_prompt = generate_video_comfyui.build_video_prompt(visual_prompt, script, sound_prompt)

        # Optional: ask ComfyUI to drop resident models before a render whose
        # peak scales with clip length. Useful when ComfyUI shares hardware with
        # another backend; off by default since that is a deployment detail.
        log_event(run_dir, "video", f"comfyui={comfyui_url} workflow={workflow_path.name} timeout={video_timeout}s prompt_enhance={prompt_enhance} workflow_prompt_chars={len(workflow_prompt)}")
        if os.environ.get("VIDEO_COMFYUI_FREE_BEFORE_RUN", "").strip().lower() in ("1", "true", "yes"):
            freed = generate_video_comfyui.free_memory(comfyui_url, min(video_timeout, 60))
            log_event(run_dir, "video", f"free_memory requested: {'ok' if freed else 'failed'}")
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        workflow_check = generate_video_comfyui.validate_video_workflow(workflow, workflow_path.name)
        log_event(run_dir, "video", f"workflow validated: {workflow_check}")
        source_width, source_height = generate_video_comfyui.image_dimensions(anchor_path)
        width, height = target_video_dimensions()
        log_event(run_dir, "video", f"anchor {anchor_path.name} is {source_width}x{source_height}; target video size {width}x{height}")
        anchor_path = prepare_anchor_image(anchor_path, run_dir, width, height)
        log_event(run_dir, "video", f"anchor for upload: {anchor_path.name} at {width}x{height}")
        video_audio_duration = generate_video_comfyui.wav_duration_seconds(narration_path)
        if video_audio_duration is None:
            raise RuntimeError(f"Could not read WAV duration from {narration_path}.")
        uploaded_image_name = generate_video_comfyui.upload_input_file(comfyui_url, anchor_path, min(video_timeout, 120))
        # LTX fades the last frames of a clip. Render past the narration and cut the tail off after download.
        narration_duration = video_audio_duration
        render_audio_path = narration_path
        tail_pad = float(os.environ.get("VIDEO_LTX_TAIL_PAD_SECONDS", "0.5") or 0)
        if tail_pad > 0 and shutil.which("ffmpeg"):
            render_audio_path = output_dir / "narration-padded.wav"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(narration_path), "-af", f"apad=pad_dur={tail_pad}", "-c:a", "pcm_s16le", str(render_audio_path)], check=True)
            video_audio_duration = generate_video_comfyui.wav_duration_seconds(render_audio_path) or (narration_duration + tail_pad)
            log_event(run_dir, "video", f"narration padded by {tail_pad}s for the render ({video_audio_duration:.2f}s); the tail is cut back to {narration_duration:.2f}s after download")
        uploaded_audio_name = generate_video_comfyui.upload_input_file(comfyui_url, render_audio_path, min(video_timeout, 120))
        log_event(run_dir, "video", f"uploaded image={uploaded_image_name} audio={uploaded_audio_name} audio_duration={video_audio_duration:.2f}s")
        patched_workflow = generate_video_comfyui.apply_workflow_placeholders(
            workflow,
            image_name=uploaded_image_name,
            audio_name=uploaded_audio_name,
            prompt=workflow_prompt,
            width=width,
            height=height,
            fps=int(os.environ.get("VIDEO_COMFYUI_FPS", "24")),
            duration_seconds=video_audio_duration,
            seed=int(datetime.now(timezone.utc).timestamp()),
            output_prefix=f"{output_prefix_root.rstrip('/')}/{run_id}",
            prompt_enhance=prompt_enhance,
        )
        prompt_id = generate_video_comfyui.queue_prompt(comfyui_url, patched_workflow, min(video_timeout, 120))
        log_event(run_dir, "video", f"queued prompt_id={prompt_id} fps={int(os.environ.get('VIDEO_COMFYUI_FPS', '24'))} size={width}x{height}; waiting for ComfyUI")
        if on_queued:
            on_queued(prompt_id)
        history = generate_video_comfyui.wait_for_history(comfyui_url, prompt_id, video_timeout)
        media = generate_video_comfyui.find_media_output(history)
        log_event(run_dir, "video", f"ComfyUI finished; output={media.get('subfolder', '')}/{media.get('filename')}")
        generate_video_comfyui.download_media(comfyui_url, media, video_path, min(video_timeout, 300))
        log_event(run_dir, "video", f"downloaded to {video_path.name} bytes={video_path.stat().st_size}")
        video_path = normalize_video_dimensions(run_dir, video_path, width, height)
        if render_audio_path is not narration_path:
            video_path = trim_video_to_audio(run_dir, video_path, narration_duration)
            render_audio_path.unlink(missing_ok=True)
        video_duration = read_video_duration(video_path)
        final_size = probe_video_dimensions(video_path)
        log_event(run_dir, "video", f"final {video_path.name} size={final_size[0]}x{final_size[1] if final_size else '?'} duration={video_duration} bytes={video_path.stat().st_size}" if final_size else f"final {video_path.name} duration={video_duration} bytes={video_path.stat().st_size}")
    except SystemExit as exc:
        log_event(run_dir, "video", f"ERROR {system_exit_message(exc)}")
        raise PipelineError("video", system_exit_message(exc)) from exc
    except Exception as exc:
        log_event(run_dir, "video", f"ERROR {type(exc).__name__}: {exc}")
        raise PipelineError("video", str(exc)) from exc

    return {
        "status": "success",
        "run_id": run_id,
        "run_directory": str(run_dir),
        "video_url": f"/runs/{run_id}/video",
        "video_duration_seconds": video_duration,
        "video_size_bytes": video_path.stat().st_size,
        "video_width": final_size[0] if final_size else None,
        "video_height": final_size[1] if final_size else None,
    }


def fetch_finished_render(run_dir: Path, prompt_id: str, video_path: Path) -> Optional[float]:
    """Download a render ComfyUI already finished for prompt_id, if it has one. Returns the clip duration, else None."""
    comfyui_url = os.environ.get("VIDEO_COMFYUI_BASE_URL", "").strip()
    if not comfyui_url:
        return None
    try:
        requests = importlib.import_module("requests")
        response = requests.get(f"{comfyui_url.rstrip('/')}/history/{prompt_id}", timeout=30)
        response.raise_for_status()
        history = response.json().get(prompt_id)
        if not history:
            log_event(run_dir, "video", f"prompt_id={prompt_id} not in ComfyUI history; rendering")
            return None
        media = generate_video_comfyui.find_media_output(history)
        video_path.parent.mkdir(parents=True, exist_ok=True)
        generate_video_comfyui.download_media(comfyui_url, media, video_path, 300)
        width, height = target_video_dimensions()
        video_path = normalize_video_dimensions(run_dir, video_path, width, height)
        duration = read_video_duration(video_path)
        log_event(run_dir, "video", f"prompt_id={prompt_id} already finished in ComfyUI; downloaded {media.get('filename')} to {video_path.name} duration={duration}")
        return duration
    except Exception as exc:  # noqa: BLE001 - a failed lookup just means we render
        log_event(run_dir, "video", f"prompt_id={prompt_id} lookup failed ({type(exc).__name__}: {exc}); rendering")
        return None


def generate_heroes_stage(
    *,
    run_id: str,
    run_dir: Path,
    script: str,
    anchor_path: Path,
    hero_prompt: Optional[str] = None,
    hero_workflow: Optional[str] = None,
    should_cancel=None,
) -> dict:
    """Images stage: one hero image per chunk, sized to the anchor's green panel, from an LLM-written prompt.

    Skipped, with a log line, when the anchor has no green panel or segmenting is off.
    """
    if not segments.enabled():
        log_event(run_dir, "images", "segmenting is off; no hero images")
        return {"status": "skipped", "reason": "segmenting off", "chunks": 0}
    size = segments.hero_size(anchor_path)
    if size is None:
        log_event(run_dir, "images", f"no green panel in {anchor_path.name}; no hero images for this run")
        return {"status": "skipped", "reason": "no panel", "chunks": 0}
    manifest = segments.build_manifest(run_dir, script)
    instruction = (hero_prompt or "").strip() or prompts.default_body("hero_image")
    if not instruction:
        log_event(run_dir, "images", "no hero prompt (profile or default); no hero images")
        return {"status": "skipped", "reason": "no hero prompt", "chunks": len(manifest["chunks"])}
    sources_path = run_dir / "sources.json"
    source_lines: list[str] = []
    selected: list[dict] = []
    subject = ""
    if sources_path.is_file():
        try:
            bundle = json.loads(sources_path.read_text(encoding="utf-8"))
            selected = bundle.get("selected_sources") or []
            subject = bundle.get("subject") or bundle.get("search_text") or ""
            source_lines = [f"- {s.get('title') or ''} ({s.get('source') or ''})" for s in selected]
        except json.JSONDecodeError:
            source_lines = []
    timeout = int(os.environ.get("OPENAI_DEFAULT_TIMEOUT_SECONDS", "90"))
    log_event(run_dir, "images", f"hero size {size[0]}x{size[1]} from the anchor panel; workflow={hero_workflow or avatars.hero_default_workflow() or 'default'}; {len(manifest['chunks'])} chunk(s)")

    def make_prompt(text: str) -> str:
        user = "Segment text:\n" + text.strip() + ("\n\nSources for this broadcast:\n" + "\n".join(source_lines) if source_lines else "")
        reply = generate_script.call_openai_compatible(instruction, user, timeout) or ""
        first = reply.strip().split("\n\n")[0].strip().strip('"').strip()
        return first

    def render(prompt: str, target: Path) -> dict:
        return avatars.render_image(prompt, target, width=size[0], height=size[1], name=f"{run_id}-hero", workflow=hero_workflow or avatars.hero_default_workflow() or None)

    def card(chunk: dict, target: Path) -> None:
        # The story this chunk most likely covers: the selected source whose title shares the most words with it.
        def words(text: str) -> set[str]:
            return {w.lower().strip(".,;:!?\"'()") for w in text.split() if len(w) > 3}

        chunk_words = words(chunk["text"])
        best, best_score = None, 0
        for source in selected:
            score = len(chunk_words & words(source.get("title") or ""))
            if score > best_score:
                best, best_score = source, score
        if best is not None:
            headline = best.get("title") or chunk["text"].split(".")[0].strip()
            outlet = prompting.outlet_name(best.get("source") or "") if hasattr(prompting, "outlet_name") else (best.get("source") or "")
        else:
            headline = chunk["text"].split(".")[0].strip()
            outlet = ""
        segments.headline_card(target, size, headline, outlet=outlet, footer=subject)

    try:
        result = segments.generate_heroes(run_dir, manifest, make_prompt, render, lambda stage, message: log_event(run_dir, stage, message), should_cancel, fallback=card)
    except Exception as exc:
        log_event(run_dir, "images", f"ERROR {type(exc).__name__}: {exc}")
        raise PipelineError("images", str(exc)) from exc
    return {"status": "success", "run_id": run_id, "hero_size": list(size), **result}


def narrate_segmented(
    *,
    run_id: str,
    run_dir: Path,
    script: str,
    voice: str,
    voice_sample_path: Optional[Path] = None,
    should_cancel=None,
) -> dict:
    """Audio stage under the chunk rule: split the script, narrate each chunk, append into narration.wav."""
    if not segments.enabled():
        return generate_audio_stage(run_id=run_id, run_dir=run_dir, script=script, voice=voice, voice_sample_path=voice_sample_path)
    manifest = segments.build_manifest(run_dir, script)
    log_event(run_dir, "audio", f"segmenting: {len(manifest['chunks'])} chunk(s) of <= {manifest['max_words']} words from {len(script.split())} words")
    state = {"sample": voice_sample_path}

    def narrate(text: str, path: Path) -> dict:
        result = generate_audio_stage(run_id=run_id, run_dir=run_dir, script=text, voice=voice, voice_sample_path=state["sample"], output_path=path)
        state["sample"] = None  # a sample upload creates the voice once; later chunks reuse it
        return {"duration_seconds": result["audio_duration_seconds"]}

    try:
        result = segments.narrate_chunks(run_dir, manifest, narrate, lambda stage, message: log_event(run_dir, stage, message), should_cancel)
    except PipelineError:
        raise
    except Exception as exc:
        log_event(run_dir, "audio", f"ERROR {type(exc).__name__}: {exc}")
        raise PipelineError("audio", str(exc)) from exc
    return {"status": "success", "run_id": run_id, "run_directory": str(run_dir), "audio_url": f"/runs/{run_id}/audio", "audio_duration_seconds": result["audio_duration_seconds"], "chunks": result["chunks"]}


def render_segmented(
    *,
    run_id: str,
    run_dir: Path,
    script: str,
    anchor_path: Path,
    video_prompt: Optional[str] = None,
    video_sound_prompt: Optional[str] = None,
    prompt_enhance: Optional[bool] = None,
    should_cancel=None,
) -> dict:
    """Video stage under the chunk rule: render each chunk's clip, append into video.mp4."""
    if not segments.enabled():
        return generate_video_stage(run_id=run_id, run_dir=run_dir, script=script, anchor_path=anchor_path, narration_path=run_dir / "narration.wav", video_prompt=video_prompt, video_sound_prompt=video_sound_prompt, prompt_enhance=prompt_enhance)
    manifest = segments.load_manifest(run_dir)
    if manifest is None or manifest.get("script") != script:
        raise PipelineError("video", "chunks.json is missing or does not match the script; run the audio stage first.")
    log_event(run_dir, "video", f"rendering {len(manifest['chunks'])} chunk(s)")
    last: dict = {}

    def render(text: str, narration: Path, out_dir: Path, on_queued: Callable[[str], None]) -> dict:
        last.clear()
        last.update(generate_video_stage(run_id=run_id, run_dir=run_dir, script=text, anchor_path=anchor_path, narration_path=narration, video_prompt=video_prompt, video_sound_prompt=video_sound_prompt, prompt_enhance=prompt_enhance, output_dir=out_dir, on_queued=on_queued))
        return last

    def render_from_frame(text: str, narration: Path, out_dir: Path, on_queued: Callable[[str], None], first_frame: Optional[Path]) -> dict:
        """Continuity variant of render: the previous chunk's last frame stands in for the anchor."""
        last.clear()
        last.update(generate_video_stage(run_id=run_id, run_dir=run_dir, script=text, anchor_path=first_frame or anchor_path, narration_path=narration, video_prompt=video_prompt, video_sound_prompt=video_sound_prompt, prompt_enhance=prompt_enhance, output_dir=out_dir, on_queued=on_queued))
        return last

    try:
        if segments.continue_from_last_frame():
            log_event(run_dir, "video", "continuity on: each chunk starts from the last frame of the previous chunk (SEGMENT_CONTINUE_FROM_LAST_FRAME)")
            result = segments.render_chunks_continuous(run_dir, manifest, render_from_frame, lambda stage, message: log_event(run_dir, stage, message), should_cancel, fetch_finished=lambda prompt_id, path: fetch_finished_render(run_dir, prompt_id, path), anchor=anchor_path)
        else:
            result = segments.render_chunks(run_dir, manifest, render, lambda stage, message: log_event(run_dir, stage, message), should_cancel, fetch_finished=lambda prompt_id, path: fetch_finished_render(run_dir, prompt_id, path), anchor=anchor_path)
    except PipelineError:
        raise
    except Exception as exc:
        log_event(run_dir, "video", f"ERROR {type(exc).__name__}: {exc}")
        raise PipelineError("video", str(exc)) from exc
    final = run_dir / "video.mp4"
    size = probe_video_dimensions(final)
    return {
        "status": "success", "run_id": run_id, "run_directory": str(run_dir), "video_url": f"/runs/{run_id}/video",
        "video_duration_seconds": result["video_duration_seconds"], "video_size_bytes": final.stat().st_size,
        "video_width": size[0] if size else last.get("video_width"), "video_height": size[1] if size else last.get("video_height"), "chunks": result["chunks"],
    }


def generate_news_video(
    *,
    run_id: str,
    run_dir: Path,
    search_text: str,
    voice: str,
    anchor_path: Path,
    script_text: Optional[str] = None,
    voice_sample_path: Optional[Path] = None,
    categories: Optional[str] = None,
    language: Optional[str] = None,
    safesearch: Optional[str] = None,
    max_articles: Optional[int] = None,
    max_age_days: Optional[int] = None,
    time_range: Optional[str] = None,
    search_timeout_seconds: Optional[int] = None,
    rerank_top_n: Optional[int] = None,
    summarize_prompt: Optional[str] = None,
    intended_video_max_seconds: Optional[float] = None,
    wpm: Optional[float] = None,
    video_prompt: Optional[str] = None,
    video_sound_prompt: Optional[str] = None,
    prompt_enhance: Optional[bool] = None,
) -> dict:
    log_event(run_dir, "run", f"run_id={run_id} search_text={search_text!r} voice={voice!r} anchor={anchor_path.name} script_provided={bool(script_text)}")
    # Preflight: refuse the run before any stage if a required service is down or misconfigured.
    # A voice that arrives as an uploaded sample does not exist in Chatterbox yet, so it is not checked.
    checks = preflight.run_preflight(
        voice=None if voice_sample_path is not None else voice,
        anchor_path=anchor_path,
        need_search=not script_text,
        need_llm=not script_text,
    )
    for check in checks["checks"]:
        log_event(run_dir, "preflight", f"{check['service']}: {check['status']} ({check['latency_ms']} ms) {check['detail']}")
    (run_dir / "preflight.json").write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    if not checks["ok"]:
        failures = preflight.failure_summary(checks)
        log_event(run_dir, "preflight", f"ERROR {failures}")
        raise PipelineError("preflight", f"Preflight failed: {failures}")
    log_event(run_dir, "preflight", f"ok in {checks['duration_ms']} ms" + (" (cached)" if checks.get("cached") else ""))
    script_result = generate_script_stage(
        run_id=run_id,
        run_dir=run_dir,
        search_text=search_text,
        script_text=script_text,
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
    audio_result = narrate_segmented(
        run_id=run_id,
        run_dir=run_dir,
        script=script_result["script"],
        voice=voice,
        voice_sample_path=voice_sample_path,
    )
    video_result = render_segmented(
        run_id=run_id,
        run_dir=run_dir,
        script=script_result["script"],
        anchor_path=anchor_path,
        video_prompt=video_prompt,
        video_sound_prompt=video_sound_prompt,
        prompt_enhance=prompt_enhance,
    )

    log_event(run_dir, "run", f"complete: video={run_dir / 'video.mp4'} video_duration={video_result['video_duration_seconds']} audio_duration={audio_result['audio_duration_seconds']}")
    return {
        "status": "success",
        "run_id": run_id,
        "run_directory": str(run_dir),
        "video_url": f"/runs/{run_id}/video",
        "audio_url": f"/runs/{run_id}/audio",
        "script_url": f"/runs/{run_id}/script",
        "report_url": f"/runs/{run_id}/report",
        "sources_url": f"/runs/{run_id}/sources",
        "script": script_result["script"],
        "word_count": script_result["word_count"],
        "target_words": script_result["target_words"],
        "audio_duration_seconds": audio_result["audio_duration_seconds"],
        "video_duration_seconds": video_result["video_duration_seconds"],
        "selected_sources": script_result["selected_sources"],
    }