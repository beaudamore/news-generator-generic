#!/usr/bin/env python3
"""Generate a ComfyUI talking-head video from an image, audio, and prompt."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import struct
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    dotenv = importlib.import_module("dotenv")
    dotenv.load_dotenv(path, override=True)


def resolve_project_path(path_arg: str) -> Path:
    path = Path(path_arg).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w']+\b", text))


def read_text_arg(text: Optional[str], text_file: Optional[str]) -> str:
    if text:
        return text.strip()
    if text_file:
        return resolve_project_path(text_file).read_text(encoding="utf-8").strip()
    return ""


def read_env_text(name: str, file_name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    file_value = os.environ.get(file_name, "").strip()
    if file_value:
        return resolve_project_path(file_value).read_text(encoding="utf-8").strip()
    return ""


def duration_from_words(text: str, wpm: float) -> float:
    if wpm <= 0:
        raise ValueError("WPM must be greater than zero.")
    return word_count(text) / wpm * 60.0


def build_video_prompt(visual_prompt: str, speech_text: str, sound_prompt: str) -> str:
    if "[SPEECH]:" in visual_prompt:
        return visual_prompt
    return f"[VISUAL]: {visual_prompt.strip()}\n[SPEECH]: {speech_text.strip()}\n[SOUNDS]: {sound_prompt.strip()}"


def wav_duration_seconds(path: Path) -> Optional[float]:
    try:
        with wave.open(str(path), "rb") as wav_file:
            rate = wav_file.getframerate()
            if rate <= 0:
                return None
            return wav_file.getnframes() / float(rate)
    except wave.Error:
        return None


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as file:
        header = file.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a PNG file: {path}")
    return struct.unpack(">II", header[16:24])


def image_dimensions(path: Path) -> tuple[int, int]:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return png_dimensions(path)
    raise ValueError(f"Unsupported image type for dimension detection: {path.suffix}")


def free_memory(base_url: str, timeout: int) -> bool:
    """Ask the ComfyUI server to unload its models and release its cache.

    A remote call over the ComfyUI HTTP API like every other call here; it makes
    no assumption about where ComfyUI runs relative to this app or to any other
    backend. Only ever affects ComfyUI's own memory. Best effort - a server that
    refuses or is unreachable just means less headroom, never a failed run.
    """
    requests = importlib.import_module("requests")
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=timeout,
        )
        return response.ok
    except Exception:
        return False


def upload_input_file(base_url: str, path: Path, timeout: int) -> str:
    requests = importlib.import_module("requests")
    with path.open("rb") as file:
        response = requests.post(
            f"{base_url.rstrip('/')}/upload/image",
            data={"type": "input", "overwrite": "true"},
            files={"image": (path.name, file, "application/octet-stream")},
            timeout=timeout,
        )
    response.raise_for_status()
    data = response.json()
    return data["name"]


def replace_placeholders(value: Any, replacements: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: replace_placeholders(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_placeholders(item, replacements) for item in value]
    if isinstance(value, str):
        if value in replacements:
            return replacements[value]
        for placeholder, replacement in replacements.items():
            value = value.replace(placeholder, str(replacement))
        return value
    return value


def apply_workflow_placeholders(
    workflow: dict[str, Any],
    image_name: str,
    audio_name: str,
    prompt: str,
    width: int,
    height: int,
    fps: int,
    duration_seconds: float,
    seed: int,
    output_prefix: str,
    prompt_enhance: bool,
) -> dict[str, Any]:
    replacements = {
        "{{FIRST_FRAME_IMAGE}}": image_name,
        "{{REFERENCE_AUDIO}}": audio_name,
        "{{PROMPT}}": prompt,
        "{{WIDTH}}": width,
        "{{HEIGHT}}": height,
        "{{FPS}}": fps,
        "{{DURATION}}": duration_seconds,
        "{{SEED}}": seed,
        "{{OUTPUT_PREFIX}}": output_prefix,
        "{{PROMPT_ENHANCE}}": prompt_enhance,
    }
    return replace_placeholders(workflow, replacements)


REQUIRED_PLACEHOLDERS = (
    "{{FIRST_FRAME_IMAGE}}", "{{REFERENCE_AUDIO}}", "{{PROMPT}}", "{{WIDTH}}", "{{HEIGHT}}",
    "{{FPS}}", "{{DURATION}}", "{{SEED}}", "{{OUTPUT_PREFIX}}",
)
# Nodes that mark the ID-LoRA voice-clone variant of the LTX-2.3 workflow. That variant
# uses the narration WAV only as a voice reference and generates its own speech, so the
# video never says the narration. The official IA2V template has neither node.
FORBIDDEN_AUDIO_NODES = ("LTXVReferenceAudio", "LTXVEmptyLatentAudio")


# ----------------------------------------------------------------------------- template families
#
# ltx      LTX-2.3 image+audio to video: one pass renders every frame, so the clip length is a
#          {{DURATION}} placeholder and the model holds a shot for roughly 10 seconds at most.
# wan_s2v  Wan2.2 S2V: renders 77-frame segments at 16 fps (4.8 s each). The first
#          WanSoundImageToVideo node covers the first segment; every WanSoundImageToVideoExtend
#          node continues from all frames rendered so far. Length comes from how many Extend
#          stages are chained, which fit_wan_s2v_chain sets to cover the narration.

WAN_REQUIRED_PLACEHOLDERS = (
    "{{FIRST_FRAME_IMAGE}}", "{{REFERENCE_AUDIO}}", "{{PROMPT}}", "{{WIDTH}}", "{{HEIGHT}}",
    "{{FPS}}", "{{SEED}}", "{{OUTPUT_PREFIX}}",
)


def workflow_family(workflow: dict[str, Any]) -> str:
    classes = {v.get("class_type", "") for v in workflow.values() if isinstance(v, dict)}
    if "WanSoundImageToVideo" in classes:
        return "wan_s2v"
    if any(c.startswith("LTXV") for c in classes):
        return "ltx"
    return "unknown"


def family_settings(family: str) -> dict[str, int]:
    """Frame rate and output size per template family. Wan S2V is trained at 16 fps; sizes are multiples of 16."""
    if family == "wan_s2v":
        return {
            "fps": int(os.environ.get("VIDEO_WAN_S2V_FPS", "16")),
            "width": int(os.environ.get("VIDEO_WAN_S2V_WIDTH", "1280")),
            "height": int(os.environ.get("VIDEO_WAN_S2V_HEIGHT", "720")),
        }
    return {
        "fps": int(os.environ.get("VIDEO_COMFYUI_FPS", "24")),
        "width": int(os.environ.get("VIDEO_COMFYUI_WIDTH", "1280")),
        "height": int(os.environ.get("VIDEO_COMFYUI_HEIGHT", "720")),
    }


def _links_to(workflow: dict[str, Any], node_id: str) -> list[tuple[str, str]]:
    """(consumer node id, input name) pairs whose value links to node_id."""
    found = []
    for k, v in workflow.items():
        for name, value in v.get("inputs", {}).items():
            if isinstance(value, list) and value and value[0] == node_id:
                found.append((k, name))
    return found


def wan_s2v_chain(workflow: dict[str, Any]) -> dict[str, Any]:
    """Describe the segment chain: base node, base sampler, ordered Extend stages, the last total latent, frames per segment."""
    classes = {k: v.get("class_type") for k, v in workflow.items() if isinstance(v, dict)}
    bases = [k for k, c in classes.items() if c == "WanSoundImageToVideo"]
    if len(bases) != 1:
        raise RuntimeError(f"expected one WanSoundImageToVideo node, found {len(bases)}")
    base = bases[0]
    samplers = [k for k, name in _links_to(workflow, base) if name == "latent_image" and classes.get(k) == "KSampler"]
    if len(samplers) != 1:
        raise RuntimeError("expected one KSampler taking the base WanSoundImageToVideo latent")
    base_sampler = samplers[0]
    length_link = workflow[base]["inputs"].get("length")
    if isinstance(length_link, list):
        frames = int(workflow[length_link[0]]["inputs"]["value"])
    else:
        frames = int(length_link)
    stages: list[dict[str, str]] = []
    total = base_sampler  # the latent holding everything rendered so far
    while True:
        extends = [k for k, name in _links_to(workflow, total) if name == "video_latent" and classes.get(k) == "WanSoundImageToVideoExtend"]
        if not extends:
            break
        extend = extends[0]
        sampler = next(k for k, name in _links_to(workflow, extend) if name == "latent_image" and classes.get(k) == "KSampler")
        concat = next(k for k, name in _links_to(workflow, sampler) if name == "samples2" and classes.get(k) == "LatentConcat")
        stages.append({"extend": extend, "sampler": sampler, "concat": concat, "previous": total})
        total = concat
    return {"base": base, "base_sampler": base_sampler, "stages": stages, "total": total, "frames_per_segment": frames}


def _remap(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, list) and value and isinstance(value[0], str) and value[0] in mapping:
        return [mapping[value[0]], *value[1:]]
    if isinstance(value, dict):
        return {k: _remap(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, mapping) for v in value]
    return value


def fit_wan_s2v_chain(workflow: dict[str, Any], audio_seconds: float, fps: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a copy of the workflow with exactly enough segments to cover audio_seconds, and what was done.

    Segments needed = ceil(audio frames / frames per segment). Extra Extend stages from the export are
    removed; missing ones are cloned from the last stage with the same wiring pattern (extend continues
    from the previous total, sampler feeds a LatentConcat of previous total + new segment). Whatever
    consumed the old last total (the decode tail) is pointed at the new last total.
    """
    workflow = json.loads(json.dumps(workflow))
    chain = wan_s2v_chain(workflow)
    frames = chain["frames_per_segment"]
    needed = max(1, math.ceil(audio_seconds * fps / frames))
    have = 1 + len(chain["stages"])
    stages = chain["stages"]
    old_total = chain["total"]
    if needed < have:
        keep = stages[: needed - 1]
        for stage in stages[needed - 1 :]:
            for node_id in (stage["extend"], stage["sampler"], stage["concat"]):
                workflow.pop(node_id, None)
        new_total = keep[-1]["concat"] if keep else chain["base_sampler"]
        stages = keep
    elif needed > have:
        template = stages[-1] if stages else None
        if template is None:
            raise RuntimeError("the template has no WanSoundImageToVideoExtend stage to clone for longer audio")
        previous = template["concat"]
        for k in range(have, needed):
            ids = {"extend": f"wanext{k}:76", "sampler": f"wanext{k}:77", "concat": f"wanext{k}:78"}
            mapping = {template["extend"]: ids["extend"], template["sampler"]: ids["sampler"], template["concat"]: ids["concat"], template["previous"]: previous}
            for role in ("extend", "sampler", "concat"):
                workflow[ids[role]] = _remap(json.loads(json.dumps(workflow[template[role]])), mapping)
            stages.append({**ids, "previous": previous})
            previous = ids["concat"]
        new_total = previous
    else:
        new_total = old_total
    if new_total != old_total:
        for consumer, name in _links_to(workflow, old_total):
            if consumer in workflow and consumer not in {s["extend"] for s in stages} | {s["concat"] for s in stages}:
                link = workflow[consumer]["inputs"][name]
                workflow[consumer]["inputs"][name] = [new_total, *link[1:]]
    return workflow, {"segments": needed, "frames_per_segment": frames, "seconds_per_segment": round(frames / fps, 3), "covers_seconds": round(needed * frames / fps, 2), "exported_segments": have}


def _validate_wan_s2v(workflow: dict[str, Any], source: str, text: str) -> dict[str, Any]:
    missing = [p for p in WAN_REQUIRED_PLACEHOLDERS if p not in text]
    if missing:
        raise RuntimeError(f"{source}: missing placeholders {missing}. Substitute these into the API-format Wan S2V export.")
    classes = {k: v.get("class_type") for k, v in workflow.items() if isinstance(v, dict)}
    try:
        chain = wan_s2v_chain(workflow)
    except (RuntimeError, StopIteration, KeyError) as exc:
        raise RuntimeError(f"{source}: cannot read the Wan S2V segment chain: {exc}") from exc
    audio_link = workflow[chain["base"]]["inputs"].get("audio_encoder_output")
    if not isinstance(audio_link, list) or classes.get(audio_link[0]) != "AudioEncoderEncode":
        raise RuntimeError(f"{source}: WanSoundImageToVideo.audio_encoder_output is not fed by AudioEncoderEncode, so the narration never drives the video.")
    encoder_audio = workflow[audio_link[0]]["inputs"].get("audio")
    if not isinstance(encoder_audio, list) or classes.get(encoder_audio[0]) != "LoadAudio":
        raise RuntimeError(f"{source}: AudioEncoderEncode.audio is not fed by LoadAudio ({{REFERENCE_AUDIO}}).")
    return {"family": "wan_s2v", "placeholders": "ok", "narration_path": "LoadAudio -> AudioEncoderEncode -> WanSoundImageToVideo", "exported_segments": 1 + len(chain["stages"]), "frames_per_segment": chain["frames_per_segment"]}


def validate_wan_s2v_workflow(workflow: dict[str, Any], source: str = "workflow") -> dict[str, Any]:
    """Wan S2V counterpart of validate_video_workflow: placeholders present, LoadAudio -> AudioEncoderEncode -> WanSoundImageToVideo."""
    if "nodes" in workflow and "links" in workflow:
        raise RuntimeError(f"{source}: this is a ComfyUI UI export (nodes/links). Export it in API format instead.")
    return _validate_wan_s2v(workflow, source, json.dumps(workflow))


def validate_any_video_workflow(workflow: dict[str, Any], source: str = "workflow") -> dict[str, Any]:
    """Dispatch on template family: Wan S2V graphs get their own validator, everything else the LTX one."""
    if workflow_family(workflow) == "wan_s2v":
        return validate_wan_s2v_workflow(workflow, source)
    return validate_video_workflow(workflow, source)


def validate_video_workflow(workflow: dict[str, Any], source: str = "workflow") -> dict[str, Any]:
    """Refuse a video workflow that cannot carry the narration WAV through as the soundtrack.

    Checks, in order: every pipeline placeholder is present; no ID-LoRA audio nodes; the
    LTXVAudioVAEEncode output reaches a LTXVConcatAVLatent.audio_latent input (optionally
    through SetLatentNoiseMask). Raises RuntimeError with a specific message on failure.
    """
    if "nodes" in workflow and "links" in workflow:
        raise RuntimeError(f"{source}: this is a ComfyUI UI export (nodes/links). Export it in API format instead.")
    text = json.dumps(workflow)
    missing = [p for p in REQUIRED_PLACEHOLDERS if p not in text]
    if missing:
        raise RuntimeError(
            f"{source}: missing placeholders {missing}. The pipeline cannot inject the image, audio, prompt, "
            "size, duration, seed, or output prefix. Substitute these into the API-format export."
        )
    classes = {k: v.get("class_type") for k, v in workflow.items() if isinstance(v, dict)}
    forbidden = sorted({c for c in classes.values() if c in FORBIDDEN_AUDIO_NODES})
    if forbidden:
        raise RuntimeError(
            f"{source}: contains {forbidden}. That is the ID-LoRA voice-clone variant: it generates its own speech "
            "and uses the narration WAV only as a voice reference, so the video will not say the narration. "
            "Use the official LTX-2.3 IA2V template (comfyui-workflows/video_ltx2_3_ia2v-working.json)."
        )
    encoders = {k for k, c in classes.items() if c == "LTXVAudioVAEEncode"}
    if not encoders:
        raise RuntimeError(f"{source}: no LTXVAudioVAEEncode node, so the narration WAV is never encoded into the video.")
    fed = False
    for node_id, node in workflow.items():
        if classes.get(node_id) != "LTXVConcatAVLatent":
            continue
        link = node.get("inputs", {}).get("audio_latent")
        if not isinstance(link, list) or not link:
            continue
        upstream, hops = link[0], 0
        while classes.get(upstream) == "SetLatentNoiseMask" and hops < 5:
            samples = workflow[upstream].get("inputs", {}).get("samples")
            upstream = samples[0] if isinstance(samples, list) and samples else None
            hops += 1
        if upstream in encoders:
            fed = True
    if not fed:
        raise RuntimeError(
            f"{source}: the encoded narration (LTXVAudioVAEEncode) does not reach any LTXVConcatAVLatent.audio_latent "
            "input, so the sampler would generate its own audio instead of using the narration."
        )
    return {
        "placeholders": "ok",
        "narration_path": "LTXVAudioVAEEncode -> LTXVConcatAVLatent.audio_latent",
        "prompt_enhance_placeholder": "{{PROMPT_ENHANCE}}" in text,
    }


def queue_prompt(base_url: str, workflow: dict[str, Any], timeout: int) -> str:
    requests = importlib.import_module("requests")
    payload = {"prompt": workflow, "client_id": str(uuid.uuid4())}
    response = requests.post(f"{base_url.rstrip('/')}/prompt", json=payload, timeout=timeout)
    if not response.ok:
        raise RuntimeError(f"ComfyUI prompt failed: {response.status_code} {response.text[:2000]}")
    return response.json()["prompt_id"]


def wait_for_history(base_url: str, prompt_id: str, timeout: int) -> dict[str, Any]:
    requests = importlib.import_module("requests")
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = requests.get(f"{base_url.rstrip('/')}/history/{prompt_id}", timeout=30)
        response.raise_for_status()
        data = response.json()
        if prompt_id in data:
            return data[prompt_id]
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for ComfyUI prompt {prompt_id}")


def find_media_output(history: dict[str, Any]) -> dict[str, Any]:
    outputs = history.get("outputs", {})
    for node_output in outputs.values():
        for output_key in ("videos", "gifs", "images", "audio"):
            items = node_output.get(output_key)
            if isinstance(items, list) and items:
                item = items[0]
                if isinstance(item, dict) and item.get("filename"):
                    return item
    raise ValueError(f"No downloadable media output found. Output keys: {list(outputs.keys())}")


def download_media(base_url: str, media: dict[str, Any], output_path: Path, timeout: int) -> Path:
    requests = importlib.import_module("requests")
    params = {
        "filename": media["filename"],
        "subfolder": media.get("subfolder", ""),
        "type": media.get("type", "output"),
    }
    response = requests.get(f"{base_url.rstrip('/')}/view", params=params, timeout=timeout)
    response.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(response.content)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate video through ComfyUI.")
    parser.add_argument("--workflow", default=os.environ.get("COMFYUI_DEFAULT_WORKFLOW", "comfyui-workflows/video_ltx2_3_ia2v-working.json"), help="ComfyUI API workflow JSON path.")
    parser.add_argument("--image", required=True, help="Input image path. Dimensions are copied into the workflow.")
    parser.add_argument("--audio", required=True, help="Input narration WAV path.")
    parser.add_argument("--prompt", help="ComfyUI video-control prompt. Defaults to NEWS_VIDEO_PROMPT or NEWS_VIDEO_PROMPT_FILE.")
    parser.add_argument("--prompt-file", help="Path to a UTF-8 file containing the ComfyUI video-control prompt.")
    parser.add_argument("--speech-text", help="Narration text used for WPM-derived duration.")
    parser.add_argument("--speech-text-file", help="Path to narration text used for WPM-derived duration.")
    parser.add_argument("--sound-prompt", default=os.environ.get("NEWS_VIDEO_SOUND_PROMPT", ""), help="Sound description appended to the ID-LoRA prompt.")
    parser.add_argument("--output", required=True, help="Downloaded video output path.")
    parser.add_argument("--wpm", type=float, default=float(os.environ.get("NEWS_DEFAULT_WPM", "145")), help="Words per minute for duration calculation.")
    parser.add_argument("--fps", type=int, default=int(os.environ.get("COMFYUI_DEFAULT_FPS", "24")), help="Workflow frame rate.")
    parser.add_argument("--seed", type=int, default=int(time.time()), help="Noise seed.")
    parser.add_argument("--output-prefix", default=os.environ.get("COMFYUI_DEFAULT_OUTPUT_PREFIX", ""), help="ComfyUI SaveVideo filename prefix.")
    parser.add_argument(
        "--prompt-enhance",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("VIDEO_COMFYUI_PROMPT_ENHANCE", "").strip().lower() in ("1", "true", "yes", "on"),
        help="Enable the workflow prompt-enhancement switch ({{PROMPT_ENHANCE}}). Defaults to VIDEO_COMFYUI_PROMPT_ENHANCE.",
    )
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("COMFYUI_DEFAULT_TIMEOUT_SECONDS", "1800")), help="ComfyUI wait timeout in seconds.")
    return parser.parse_args()


def main() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args()

    base_url = os.environ.get("COMFYUI_BASE_URL", "").strip()
    if not base_url:
        raise SystemExit("Set COMFYUI_BASE_URL.")
    workflow_path = resolve_project_path(args.workflow)
    image_path = resolve_project_path(args.image)
    audio_path = resolve_project_path(args.audio)
    output_path = resolve_project_path(args.output)
    if not args.output_prefix:
        raise SystemExit("Provide --output-prefix or COMFYUI_DEFAULT_OUTPUT_PREFIX.")
    video_prompt = read_text_arg(args.prompt, args.prompt_file) or read_env_text("NEWS_VIDEO_PROMPT", "NEWS_VIDEO_PROMPT_FILE")
    if not video_prompt:
        raise SystemExit("Provide --prompt, --prompt-file, NEWS_VIDEO_PROMPT, or NEWS_VIDEO_PROMPT_FILE.")
    speech_text = read_text_arg(args.speech_text, args.speech_text_file) or video_prompt
    workflow_prompt = build_video_prompt(video_prompt, speech_text, args.sound_prompt)

    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    validate_video_workflow(workflow, str(workflow_path))
    width, height = image_dimensions(image_path)
    audio_duration = wav_duration_seconds(audio_path)
    if audio_duration is None:
        raise SystemExit(f"Could not read WAV duration from {audio_path}.")
    duration_seconds = audio_duration

    image_name = upload_input_file(base_url, image_path, min(args.timeout, 120))
    audio_name = upload_input_file(base_url, audio_path, min(args.timeout, 120))
    patched = apply_workflow_placeholders(
        workflow,
        image_name=image_name,
        audio_name=audio_name,
        prompt=workflow_prompt,
        width=width,
        height=height,
        fps=args.fps,
        duration_seconds=duration_seconds,
        seed=args.seed,
        output_prefix=args.output_prefix,
        prompt_enhance=args.prompt_enhance,
    )
    prompt_id = queue_prompt(base_url, patched, min(args.timeout, 120))
    history = wait_for_history(base_url, prompt_id, args.timeout)
    media = find_media_output(history)
    downloaded = download_media(base_url, media, output_path, min(args.timeout, 300))

    print(f"prompt_id={prompt_id}")
    print(f"image_name={image_name}")
    print(f"audio_name={audio_name}")
    print(f"width={width}")
    print(f"height={height}")
    print(f"fps={args.fps}")
    print(f"wpm={args.wpm:g}")
    print(f"speech_word_count={word_count(speech_text)}")
    print(f"workflow_prompt_chars={len(workflow_prompt)}")
    print(f"workflow_duration_seconds={duration_seconds:.2f}")
    print(f"audio_duration_seconds={audio_duration:.2f}" if audio_duration is not None else "audio_duration_seconds=unknown")
    print(f"video_path={downloaded}")
    print(f"video_size_bytes={downloaded.stat().st_size}")


if __name__ == "__main__":
    main()