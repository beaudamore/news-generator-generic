"""Segmented rendering: split the script into small chunks, narrate and render each, append with ffmpeg.

Rule (docs/ui-dev1.md): whatever the script stage produces, for however many
articles, is split into chunks of at most SEGMENT_MAX_WORDS at sentence
boundaries. Each chunk is narrated and rendered on its own, which bounds GPU
memory per render, then the clips are appended into the run's video.mp4.

Layout inside a run directory:
  chunks.json                 manifest: chunks with text, words, durations, status
  chunks/NN/narration.wav     per-chunk narration
  chunks/NN/video.mp4         per-chunk render
  chunks/NN/hero.png          optional support image for the chunk (any of hero.png/.jpg/.jpeg/.webp)
  chunks/NN/composited.mp4    the render with the hero keyed into the anchor's green panel
  narration.wav               all chunk narrations appended (for the audio artifact)
  video.mp4                   all chunk renders appended
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def enabled() -> bool:
    return _env("SEGMENT_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def max_words() -> int:
    return max(20, int(_env("SEGMENT_MAX_WORDS", "100")))


def continue_from_last_frame() -> bool:
    """Start each chunk's render from the last frame of the previous chunk instead of the anchor image.

    Off: every chunk restarts from the anchor, so the presenter snaps back to the reference pose at each
    cut. On: the clips play as one continuous take; the model may drift from the reference over a long run.
    """
    return _env("SEGMENT_CONTINUE_FROM_LAST_FRAME", "false").lower() in ("1", "true", "yes", "on")


def last_frame(clip: Path, target: Path) -> Path:
    """Write the last frame of a clip as a PNG (the frame nearest the end, after any tail trim)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(["-sseof", "-0.2", "-i", str(clip), "-frames:v", "1", "-update", "1", str(target)])
    if not target.is_file():
        raise RuntimeError(f"could not extract the last frame of {clip.name}")
    return target


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


def split_script(text: str, limit: Optional[int] = None) -> list[str]:
    """Chunks of at most `limit` words, never splitting a sentence unless one sentence alone exceeds the limit."""
    limit = limit or max_words()
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in split_sentences(text):
        words = sentence.split()
        if len(words) > limit:
            if current:
                chunks.append(" ".join(current))
                current, count = [], 0
            for start in range(0, len(words), limit):
                chunks.append(" ".join(words[start : start + limit]))
            continue
        if count + len(words) > limit and current:
            chunks.append(" ".join(current))
            current, count = [], 0
        current.append(sentence)
        count += len(words)
    if current:
        chunks.append(" ".join(current))
    return chunks


# ----------------------------------------------------------------------------- ffmpeg helpers


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def concat_media(parts: list[Path], target: Path, reencode: bool = False) -> None:
    """Append clips. Stream copy first (same encoder settings from the same workflow); re-encode on failure."""
    list_file = target.with_suffix(".concat.txt")
    list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    try:
        if reencode:
            raise subprocess.CalledProcessError(1, "forced")
        _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(target)])
    except subprocess.CalledProcessError:
        if target.suffix == ".wav":
            _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file), "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(target)])
        else:
            _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file), "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target)])
    finally:
        list_file.unlink(missing_ok=True)


def media_duration(path: Path) -> Optional[float]:
    if not path.is_file() or not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], text=True)
        return float(json.loads(out).get("format", {}).get("duration") or 0) or None
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None


# ----------------------------------------------------------------------------- hero panel composite

HERO_NAMES = ("hero.png", "hero.jpg", "hero.jpeg", "hero.webp")
PANEL_KEY_COLOR = "0x0EF50F"  # the chroma green the anchor's studio monitor is painted with


def _is_key_green(pixel: tuple[int, int, int]) -> bool:
    r, g, b = pixel[:3]
    return g > 120 and g > r * 1.6 and g > b * 1.6


def detect_panel(anchor: Path) -> Optional[tuple[int, int, int, int]]:
    """Bounding box (x, y, w, h) of the green panel in the anchor image, found by green density per row and column.

    Returns None when the anchor has no panel (less than a 5% by 5% patch of dense green).
    """
    with Image.open(anchor) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        px = rgb.load()
        rows = [sum(1 for x in range(0, width, 2) if _is_key_green(px[x, y])) for y in range(height)]
        cols = [sum(1 for y in range(0, height, 2) if _is_key_green(px[x, y])) for x in range(width)]
    dense_rows = [y for y, n in enumerate(rows) if n > (width / 2) * 0.3]
    dense_cols = [x for x, n in enumerate(cols) if n > (height / 2) * 0.3]
    if len(dense_rows) < height * 0.05 or len(dense_cols) < width * 0.05:
        return None
    x0, x1, y0, y1 = min(dense_cols), max(dense_cols), min(dense_rows), max(dense_rows)
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1


def hero_size(anchor: Path, long_side: int = 1024) -> Optional[tuple[int, int]]:
    """Generation size for heroes: the anchor panel's aspect ratio, long side `long_side`, both sides rounded to 16."""
    panel = detect_panel(anchor)
    if panel is None:
        return None
    w, h = panel[2], panel[3]
    scale = long_side / max(w, h)
    width = max(256, int(round(w * scale / 16)) * 16)
    height = max(256, int(round(h * scale / 16)) * 16)
    return width, height


_CARD_FONTS = ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Helvetica.ttc")
_CARD_BOLD_FONTS = ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Helvetica.ttc")


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for candidate in (_CARD_BOLD_FONTS if bold else _CARD_FONTS):
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(" ".join(lines).split()) < len(words) and lines:
        lines[-1] = lines[-1].rstrip(".,;:") + "\u2026"
    return lines


def headline_card(target: Path, size: tuple[int, int], headline: str, outlet: str = "", footer: str = "") -> Path:
    """A dark panel with the outlet, the headline, and a footer line: the hero used when no image could be made."""
    width, height = size
    image = Image.new("RGB", (width, height), (16, 24, 40))
    draw = ImageDraw.Draw(image)
    for y in range(height):  # subtle vertical gradient
        shade = int(16 + 18 * y / height)
        draw.line([(0, y), (width, y)], fill=(shade, shade + 8, shade + 30))
    margin = int(width * 0.07)
    accent = (46, 134, 222)
    draw.rectangle([margin, int(height * 0.16), margin + int(width * 0.012), int(height * 0.84)], fill=accent)
    text_x = margin + int(width * 0.04)
    text_width = width - text_x - margin
    if outlet:
        outlet_font = _font(int(height * 0.055), bold=True)
        draw.text((text_x, int(height * 0.16)), outlet.upper(), font=outlet_font, fill=accent)
    title_font = _font(int(height * 0.105), bold=True)
    lines = _wrap(draw, headline, title_font, text_width, max_lines=4)
    y = int(height * 0.26)
    for line in lines:
        draw.text((text_x, y), line, font=title_font, fill=(245, 247, 250))
        y += int(height * 0.125)
    if footer:
        footer_font = _font(int(height * 0.045))
        draw.text((text_x, int(height * 0.86)), footer, font=footer_font, fill=(150, 165, 190))
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG", optimize=True)
    return target


def hero_for(chunk_dir: Path) -> Optional[Path]:
    for name in HERO_NAMES:
        path = chunk_dir / name
        if path.is_file():
            return path
    return None


def video_dimensions(path: Path) -> Optional[tuple[int, int]]:
    try:
        out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)], text=True)
        w, h = out.strip().split(",")[:2]
        return int(w), int(h)
    except (subprocess.SubprocessError, ValueError):
        return None


def _green_mask(image: Image.Image) -> Image.Image:
    """Mode-L mask of chroma-green pixels (g > 1.6 r, g > 1.6 b, g > 50), computed with band arithmetic in C.

    The brightness floor is low on purpose: the video model may fade or darken a frame, and a darkened
    panel must still key by its color ratio.
    """
    r, g, b = image.convert("RGB").split()
    scaled_r = r.point(lambda v: min(255, int(v * 1.6)))
    scaled_b = b.point(lambda v: min(255, int(v * 1.6)))
    over_r = ImageChops.subtract(g, scaled_r).point(lambda v: 255 if v > 0 else 0)
    over_b = ImageChops.subtract(g, scaled_b).point(lambda v: 255 if v > 0 else 0)
    bright = g.point(lambda v: 255 if v > 50 else 0)
    return ImageChops.multiply(ImageChops.multiply(over_r, over_b), bright)


def _corners_from_mask(mask: Image.Image, window: tuple[int, int, int, int]) -> Optional[tuple[tuple[int, int], ...]]:
    """Corners (tl, tr, bl, br) of the green region inside `window` (x, y, w, h), from per-row first/last green pixels."""
    wx, wy, ww, wh = window
    x0, y0 = max(0, wx), max(0, wy)
    x1, y1 = min(mask.width, wx + ww), min(mask.height, wy + wh)
    if x1 <= x0 or y1 <= y0:
        return None
    region = mask.crop((x0, y0, x1, y1))
    data = region.tobytes()
    width = x1 - x0
    tl = tr = bl = br = None
    s_min = d_max = d_min = s_max = None
    green_rows = 0
    for row in range(y1 - y0):
        line = data[row * width : (row + 1) * width]
        first = line.find(b"\xff")
        if first < 0:
            continue
        last = line.rfind(b"\xff")
        if last - first < width * 0.2:  # a stray sliver, not the panel
            continue
        green_rows += 1
        fx, lx, y = first + x0, last + x0, row + y0
        if s_min is None or fx + y < s_min:
            s_min, tl = fx + y, (fx, y)
        if d_max is None or lx - y > d_max:
            d_max, tr = lx - y, (lx, y)
        if d_min is None or fx - y < d_min:
            d_min, bl = fx - y, (fx, y)
        if s_max is None or lx + y > s_max:
            s_max, br = lx + y, (lx, y)
    if green_rows < (y1 - y0) * 0.2 or tl is None:
        return None
    return tl, tr, bl, br


def _expand_quad(quad: tuple[tuple[float, float], ...], factor: float) -> list[tuple[float, float]]:
    cx = sum(p[0] for p in quad) / 4
    cy = sum(p[1] for p in quad) / 4
    return [(cx + (p[0] - cx) * factor, cy + (p[1] - cy) * factor) for p in quad]


def _perspective_coeffs(target_quad: list[tuple[float, float]], source_size: tuple[int, int]) -> list[float]:
    """PIL PERSPECTIVE coefficients mapping output points (the quad tl, tr, bl, br) back to the source image corners."""
    sw, sh = source_size
    source = [(0, 0), (sw, 0), (0, sh), (sw, sh)]
    matrix: list[list[float]] = []
    rhs: list[float] = []
    for (x, y), (u, v) in zip(target_quad, source):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); rhs.append(u)
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y]); rhs.append(v)
    n = 8
    for col in range(n):  # Gaussian elimination with partial pivoting
        pivot = max(range(col, n), key=lambda r: abs(matrix[r][col]))
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        rhs[col], rhs[pivot] = rhs[pivot], rhs[col]
        div = matrix[col][col] or 1e-12
        for r in range(n):
            if r == col:
                continue
            factor = matrix[r][col] / div
            if factor:
                matrix[r] = [a - factor * b for a, b in zip(matrix[r], matrix[col])]
                rhs[r] -= factor * rhs[col]
    return [rhs[i] / (matrix[i][i] or 1e-12) for i in range(n)]


def _extrapolate_clipped(corners: tuple[tuple[int, int], ...], width: int, height: int, aspect: float) -> tuple[tuple[float, float], ...]:
    """When the panel runs off the frame edge, rebuild the hidden corners from the visible edge and the panel's aspect.

    The visible side gives the panel's height (or width) at that side and the slope of the top and bottom
    (or left and right) edges; the hidden side is that far away along those edges. The hero keeps its
    shape and runs off screen with the panel instead of being squeezed into the visible part.
    """
    tl, tr, bl, br = [(float(x), float(y)) for x, y in corners]
    edge = 1.5
    left, right = (tl[0] <= edge or bl[0] <= edge), (tr[0] >= width - 1 - edge or br[0] >= width - 1 - edge)
    top, bottom = (tl[1] <= edge or tr[1] <= edge), (bl[1] >= height - 1 - edge or br[1] >= height - 1 - edge)
    if left and not right:
        h = max(1.0, br[1] - tr[1]); w = h * aspect
        slope_top = (tr[1] - tl[1]) / max(1.0, tr[0] - tl[0]); slope_bottom = (br[1] - bl[1]) / max(1.0, br[0] - bl[0])
        tl = (tr[0] - w, tr[1] - slope_top * w); bl = (br[0] - w, br[1] - slope_bottom * w)
    elif right and not left:
        h = max(1.0, bl[1] - tl[1]); w = h * aspect
        slope_top = (tr[1] - tl[1]) / max(1.0, tr[0] - tl[0]); slope_bottom = (br[1] - bl[1]) / max(1.0, br[0] - bl[0])
        tr = (tl[0] + w, tl[1] + slope_top * w); br = (bl[0] + w, bl[1] + slope_bottom * w)
    if top and not bottom:
        w = max(1.0, br[0] - bl[0]); h = w / aspect
        slope_left = (bl[0] - tl[0]) / max(1.0, bl[1] - tl[1]); slope_right = (br[0] - tr[0]) / max(1.0, br[1] - tr[1])
        tl = (bl[0] - slope_left * h, bl[1] - h); tr = (br[0] - slope_right * h, br[1] - h)
    elif bottom and not top:
        w = max(1.0, tr[0] - tl[0]); h = w / aspect
        slope_left = (bl[0] - tl[0]) / max(1.0, bl[1] - tl[1]); slope_right = (br[0] - tr[0]) / max(1.0, br[1] - tr[1])
        bl = (tl[0] + slope_left * h, tl[1] + h); br = (tr[0] + slope_right * h, tr[1] + h)
    return tl, tr, bl, br


def _plausible(corners: tuple[tuple[float, float], ...], last: Optional[list[tuple[float, float]]], aspect: float, width: int, height: int) -> bool:
    """Is this detection the whole panel? Rejects slivers left when the model repaints the monitor, and shapes that are not the panel."""
    tl, tr, bl, br = corners
    edge = 1.5
    clipped_x = tl[0] <= edge or bl[0] <= edge or tr[0] >= width - 1 - edge or br[0] >= width - 1 - edge
    clipped_y = tl[1] <= edge or tr[1] <= edge or bl[1] >= height - 1 - edge or br[1] >= height - 1 - edge
    w_now = ((tr[0] - tl[0]) + (br[0] - bl[0])) / 2
    h_now = ((bl[1] - tl[1]) + (br[1] - tr[1])) / 2
    if w_now < 8 or h_now < 8:
        return False
    if not clipped_x and not clipped_y and abs((w_now / h_now) / aspect - 1) > 0.3:
        return False
    if last is not None:
        lw = ((last[1][0] - last[0][0]) + (last[3][0] - last[2][0])) / 2
        lh = ((last[2][1] - last[0][1]) + (last[3][1] - last[1][1])) / 2
        if not clipped_x and w_now < 0.6 * lw:
            return False
        if not clipped_y and h_now < 0.6 * lh:
            return False
        # A real camera move cannot shrink the panel by a quarter in one frame; a fade eating the key can.
        if w_now * h_now < 0.75 * lw * lh:
            return False
    return True


def _smooth(previous: Optional[list[tuple[float, float]]], current: tuple[tuple[float, float], ...], alpha: float = 0.5) -> list[tuple[float, float]]:
    if previous is None:
        return [(float(x), float(y)) for x, y in current]
    return [(px + (x - px) * alpha, py + (y - py) * alpha) for (px, py), (x, y) in zip(previous, current)]


def composite_hero(clip: Path, hero: Path, anchor: Path, target: Path, bleed: float = 1.03) -> dict[str, Any]:
    """Key the green panel out of every frame of the clip and show the hero through it, tracked frame by frame.

    The panel's four corners are found on each frame (inside a window around where they were on the
    previous frame; the anchor's panel box seeds the first), lightly smoothed, and the hero is
    perspective-warped onto them, grown by `bleed`. Only the panel's window is keyed, so green elsewhere
    in the shot (the desk reflection) is untouched. Frames stream through ffmpeg both ways; the audio is
    copied from the clip.
    """
    panel = detect_panel(anchor)
    if panel is None:
        raise RuntimeError(f"no green panel found in {anchor.name}")
    dims = video_dimensions(clip)
    if dims is None:
        raise RuntimeError(f"cannot read dimensions of {clip.name}")
    width, height = dims
    fps_out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(clip)], text=True).strip()
    with Image.open(anchor) as image:
        anchor_w, anchor_h = image.size
    sx, sy = width / anchor_w, height / anchor_h
    bx, by, bw, bh = panel[0] * sx, panel[1] * sy, panel[2] * sx, panel[3] * sy
    aspect = bw / bh
    window = (int(bx - bw * 0.3), int(by - bh * 0.3), int(bw * 1.6), int(bh * 1.6))
    with Image.open(hero) as hero_image:
        hero_rgb = hero_image.convert("RGB")
        hero_rgb.load()

    decoder = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(clip), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    encoder = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", fps_out, "-i", "-", "-i", str(clip),
         "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(target)],
        stdin=subprocess.PIPE,
    )
    frame_bytes = width * height * 3
    tracked: Optional[list[tuple[float, float]]] = None
    first_corners: Optional[tuple[tuple[int, int], ...]] = None
    frames = lost = 0
    max_move = 0.0
    try:
        while True:
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            frame = Image.frombytes("RGB", (width, height), raw)
            mask = _green_mask(frame)
            corners = _corners_from_mask(mask, window)
            if corners is not None:
                corners = _extrapolate_clipped(corners, width, height, aspect)
                if not _plausible(corners, tracked, aspect, width, height):
                    corners = None
            if corners is None:
                # The model repainted the monitor (or it left the frame): no green to key. Cover the last
                # tracked panel position with the hero as a solid shape so the invented picture never shows.
                lost += 1
                if tracked is None:
                    encoder.stdin.write(raw)
                    frames += 1
                    continue
                covered = True
            else:
                covered = False
                if first_corners is None:
                    first_corners = corners
                max_move = max(max_move, max(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(corners, first_corners)))
                tracked = _smooth(tracked, corners)
                cx = sum(p[0] for p in tracked) / 4
                cy = sum(p[1] for p in tracked) / 4
                qw = max(p[0] for p in tracked) - min(p[0] for p in tracked)
                qh = max(p[1] for p in tracked) - min(p[1] for p in tracked)
                window = (int(cx - qw * 0.7), int(cy - qh * 0.7), int(qw * 1.4), int(qh * 1.4))
            quad = _expand_quad(tuple(tracked), bleed)
            rx0 = max(0, int(min(p[0] for p in quad)))
            ry0 = max(0, int(min(p[1] for p in quad)))
            rx1 = min(width, int(max(p[0] for p in quad)) + 1)
            ry1 = min(height, int(max(p[1] for p in quad)) + 1)
            if rx1 - rx0 < 8 or ry1 - ry0 < 8:
                encoder.stdin.write(raw)
                frames += 1
                continue
            local = [(p[0] - rx0, p[1] - ry0) for p in quad]
            region_size = (rx1 - rx0, ry1 - ry0)
            warped = hero_rgb.transform(region_size, Image.PERSPECTIVE, _perspective_coeffs(local, hero_rgb.size), Image.BICUBIC)
            if covered:
                # Hero over the last known panel shape, plus whatever green is still visible there.
                region_mask = Image.new("L", region_size, 0)
                ImageDraw.Draw(region_mask).polygon([local[0], local[1], local[3], local[2]], fill=255)
                region_mask = ImageChops.lighter(region_mask, mask.crop((rx0, ry0, rx1, ry1)).filter(ImageFilter.MaxFilter(5)))
            else:
                region_mask = mask.crop((rx0, ry0, rx1, ry1)).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(0.8))
            region = frame.crop((rx0, ry0, rx1, ry1))
            region.paste(warped, (0, 0), region_mask)
            frame.paste(region, (rx0, ry0))
            encoder.stdin.write(frame.tobytes())
            frames += 1
    finally:
        decoder.stdout.close()
        encoder.stdin.close()
        decoder.wait()
        encoder.wait()
    if encoder.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to encode {target.name}")
    return {"frames": frames, "covered_frames": lost, "first_corners": first_corners, "max_move_px": max_move, "hero": hero.name}


def hero_composite_mode() -> str:
    """key: per-frame green key with tracking (composite_hero). locked: fill the measured panel shape, held still (composite_hero_locked)."""
    return _env("HERO_COMPOSITE_MODE", "key").lower()


def _quad_distance(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    return max(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q in zip(a, b))


def hero_bleed_px() -> float:
    """How far, in pixels, the hero is grown beyond the measured panel edge so no green peeks out (HERO_COMPOSITE_BLEED_PX)."""
    return max(0.0, float(_env("HERO_COMPOSITE_BLEED_PX", "4")))


def _grow_quad(quad: tuple[tuple[float, float], ...], pixels: float) -> list[tuple[float, float]]:
    """Push each corner `pixels` away from the quad's center along the corner's own direction."""
    cx = sum(p[0] for p in quad) / 4
    cy = sum(p[1] for p in quad) / 4
    grown = []
    for x, y in quad:
        dx, dy = x - cx, y - cy
        length = (dx * dx + dy * dy) ** 0.5 or 1.0
        grown.append((x + dx / length * pixels, y + dy / length * pixels))
    return grown


def composite_hero_locked(clip: Path, hero: Path, anchor: Path, target: Path, bleed_px: Optional[float] = None, lock_px: float = 6.0, dead_zone_px: float = 3.0) -> dict[str, Any]:
    """Fill the panel with the hero, held still when the camera holds still. A copy beside composite_hero.

    Pass one measures the panel corners on every frame (same detection as the tracker) without writing
    anything. If the corners never stray more than `lock_px` from their median, the hero is placed once at
    the median and stays put for the whole clip: no per-frame wiggle. Otherwise the clip is tracked with a
    dead zone: the position only updates when the corners move more than `dead_zone_px`. In both cases the
    hero fills the whole panel shape every frame (no green key), grown by `bleed_px` (HERO_COMPOSITE_BLEED_PX)
    so no green peeks out at the edges; the cost is that anything in front of the panel is painted over too.
    """
    bleed_px = hero_bleed_px() if bleed_px is None else bleed_px
    panel = detect_panel(anchor)
    if panel is None:
        raise RuntimeError(f"no green panel found in {anchor.name}")
    dims = video_dimensions(clip)
    if dims is None:
        raise RuntimeError(f"cannot read dimensions of {clip.name}")
    width, height = dims
    fps_out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(clip)], text=True).strip()
    with Image.open(anchor) as image:
        anchor_w, anchor_h = image.size
    sx, sy = width / anchor_w, height / anchor_h
    bx, by, bw, bh = panel[0] * sx, panel[1] * sy, panel[2] * sx, panel[3] * sy
    aspect = bw / bh
    window = (int(bx - bw * 0.3), int(by - bh * 0.3), int(bw * 1.6), int(bh * 1.6))
    frame_bytes = width * height * 3

    # ---- pass one: measure
    measured: list[Optional[list[tuple[float, float]]]] = []
    tracked: Optional[list[tuple[float, float]]] = None
    decoder = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(clip), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    try:
        while True:
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            corners = _corners_from_mask(_green_mask(Image.frombytes("RGB", (width, height), raw)), window)
            if corners is not None:
                corners = _extrapolate_clipped(corners, width, height, aspect)
                if not _plausible(corners, tracked, aspect, width, height):
                    corners = None
            if corners is None:
                measured.append(None)
                continue
            tracked = [(float(x), float(y)) for x, y in corners]
            measured.append(tracked)
            cx = sum(p[0] for p in tracked) / 4
            cy = sum(p[1] for p in tracked) / 4
            qw = max(p[0] for p in tracked) - min(p[0] for p in tracked)
            qh = max(p[1] for p in tracked) - min(p[1] for p in tracked)
            window = (int(cx - qw * 0.7), int(cy - qh * 0.7), int(qw * 1.4), int(qh * 1.4))
    finally:
        decoder.stdout.close()
        decoder.wait()
    good = [q for q in measured if q is not None]
    if not good:
        raise RuntimeError(f"no green panel found in any frame of {clip.name}")
    median = [(sorted(q[i][0] for q in good)[len(good) // 2], sorted(q[i][1] for q in good)[len(good) // 2]) for i in range(4)]
    distances = sorted(_quad_distance(q, median) for q in good)
    deviation = distances[-1]
    typical = distances[int(len(distances) * 0.95)] if len(distances) > 1 else deviation  # a few stray frames must not force tracking
    locked = typical <= lock_px

    # ---- the quad used on each frame
    per_frame: list[list[tuple[float, float]]] = []
    current = median
    for q in measured:
        if not locked and q is not None and _quad_distance(q, current) > dead_zone_px:
            current = q
        per_frame.append(current)

    # ---- pass two: composite
    with Image.open(hero) as hero_image:
        hero_rgb = hero_image.convert("RGB")
        hero_rgb.load()
    decoder = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(clip), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    encoder = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", fps_out, "-i", "-", "-i", str(clip),
         "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(target)],
        stdin=subprocess.PIPE,
    )
    cache: dict[tuple, tuple[Image.Image, Image.Image, int, int]] = {}
    frames = 0
    try:
        while True:
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            quad_key = tuple(per_frame[frames]) if frames < len(per_frame) else tuple(current)
            if quad_key not in cache:
                quad = _grow_quad(tuple(quad_key), bleed_px)
                rx0 = max(0, int(min(p[0] for p in quad)))
                ry0 = max(0, int(min(p[1] for p in quad)))
                rx1 = min(width, int(max(p[0] for p in quad)) + 1)
                ry1 = min(height, int(max(p[1] for p in quad)) + 1)
                local = [(p[0] - rx0, p[1] - ry0) for p in quad]
                region_size = (max(2, rx1 - rx0), max(2, ry1 - ry0))
                warped = hero_rgb.transform(region_size, Image.PERSPECTIVE, _perspective_coeffs(local, hero_rgb.size), Image.BICUBIC)
                shape = Image.new("L", region_size, 0)
                ImageDraw.Draw(shape).polygon([local[0], local[1], local[3], local[2]], fill=255)
                shape = shape.filter(ImageFilter.GaussianBlur(0.6))
                cache[quad_key] = (warped, shape, rx0, ry0)
            warped, shape, rx0, ry0 = cache[quad_key]
            frame = Image.frombytes("RGB", (width, height), raw)
            frame.paste(warped, (rx0, ry0), shape)
            encoder.stdin.write(frame.tobytes())
            frames += 1
    finally:
        decoder.stdout.close()
        encoder.stdin.close()
        decoder.wait()
        encoder.wait()
    if encoder.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to encode {target.name}")
    return {"mode": "locked" if locked else "tracked", "frames": frames, "lost_frames": sum(1 for q in measured if q is None), "max_deviation_px": deviation, "typical_deviation_px": typical, "bleed_px": bleed_px, "hero": hero.name}


def composite_hero_by_mode(clip: Path, hero: Path, anchor: Path, target: Path) -> tuple[dict[str, Any], str]:
    """Run whichever compositor HERO_COMPOSITE_MODE names and return (info, a one-line description for the log)."""
    if hero_composite_mode() == "locked":
        info = composite_hero_locked(clip, hero, anchor, target)
        return info, f"{info['mode']} fill over {info['frames']} frames (panel deviation typical {info['typical_deviation_px']:.0f}px, max {info['max_deviation_px']:.0f}px, {info['lost_frames']} frames without a measurable panel)"
    info = composite_hero(clip, hero, anchor, target)
    return info, f"tracked onto the panel over {info['frames']} frames (max move {info['max_move_px']:.0f}px, {info['covered_frames']} frames with the panel repainted or lost, covered)"


# ----------------------------------------------------------------------------- manifest


def manifest_path(run_dir: Path) -> Path:
    return run_dir / "chunks.json"


def load_manifest(run_dir: Path) -> Optional[dict[str, Any]]:
    path = manifest_path(run_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    manifest_path(run_dir).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def build_manifest(run_dir: Path, script: str) -> dict[str, Any]:
    """Create or reuse the chunk manifest. Reused only when the script text is unchanged (retries)."""
    existing = load_manifest(run_dir)
    if existing and existing.get("script") == script and existing.get("max_words") == max_words():
        return existing
    chunks = split_script(script)
    manifest = {
        "max_words": max_words(),
        "script": script,
        "chunks": [
            {"index": i + 1, "text": text, "words": len(text.split()), "narration": f"chunks/{i + 1:02d}/narration.wav", "video": f"chunks/{i + 1:02d}/video.mp4", "audio_seconds": None, "video_seconds": None, "status": "pending"}
            for i, text in enumerate(chunks)
        ],
    }
    save_manifest(run_dir, manifest)
    return manifest


# ----------------------------------------------------------------------------- stages


def generate_heroes(
    run_dir: Path,
    manifest: dict[str, Any],
    make_prompt: Callable[[str], str],
    render: Callable[[str, Path], dict[str, Any]],
    log: Callable[[str, str], None],
    should_cancel: Optional[Callable[[], bool]] = None,
    fallback: Optional[Callable[[dict[str, Any], Path], None]] = None,
) -> dict[str, Any]:
    """Write an image prompt and render a hero for every chunk lacking one.

    When the prompt or the render fails, `fallback` draws a headline card into the same file, so the
    chunk still gets a composite. Only a failed fallback leaves a chunk without a hero.
    """
    generated = failed = reused = cards = 0
    for chunk in manifest["chunks"]:
        target = run_dir / "chunks" / f"{chunk['index']:02d}" / "hero.png"
        if target.is_file() and chunk.get("hero_status") in ("generated", "card"):
            reused += 1
            log("images", f"chunk {chunk['index']}: hero exists, reusing")
            continue
        if should_cancel and should_cancel():
            raise RuntimeError("cancelled before hero for chunk %d" % chunk["index"])
        try:
            prompt = make_prompt(chunk["text"]).strip()
            if not prompt:
                raise RuntimeError("the LLM returned an empty image prompt")
            chunk["hero_prompt"] = prompt
            log("images", f"chunk {chunk['index']}: image prompt: {prompt[:160]}")
            result = render(prompt, target)
            chunk["hero"] = f"chunks/{chunk['index']:02d}/hero.png"
            chunk["hero_seed"] = result.get("seed")
            chunk["hero_status"] = "generated"
            generated += 1
            log("images", f"chunk {chunk['index']}: hero rendered {result.get('width')}x{result.get('height')} seed={result.get('seed')}")
        except Exception as exc:  # noqa: BLE001 - one bad hero must not sink the run
            chunk["hero_error"] = f"{type(exc).__name__}: {exc}"
            log("images", f"chunk {chunk['index']}: hero FAILED {chunk['hero_error']}")
            if fallback is not None:
                try:
                    fallback(chunk, target)
                    chunk["hero"] = f"chunks/{chunk['index']:02d}/hero.png"
                    chunk["hero_status"] = "card"
                    cards += 1
                    log("images", f"chunk {chunk['index']}: headline card drawn instead")
                except Exception as card_exc:  # noqa: BLE001
                    chunk["hero_status"] = "failed"
                    chunk["hero_error"] += f"; card failed: {type(card_exc).__name__}: {card_exc}"
                    failed += 1
                    log("images", f"chunk {chunk['index']}: headline card FAILED {card_exc}")
            else:
                chunk["hero_status"] = "failed"
                failed += 1
        save_manifest(run_dir, manifest)
    log("images", f"heroes: generated={generated} cards={cards} reused={reused} failed={failed} of {len(manifest['chunks'])} chunk(s)")
    return {"generated": generated, "cards": cards, "reused": reused, "failed": failed, "chunks": len(manifest["chunks"])}


def narrate_chunks(
    run_dir: Path,
    manifest: dict[str, Any],
    narrate: Callable[[str, Path], dict[str, Any]],
    log: Callable[[str, str], None],
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Narrate every chunk lacking a narration file, then append them into run_dir/narration.wav."""
    for chunk in manifest["chunks"]:
        target = run_dir / chunk["narration"]
        if target.is_file() and chunk.get("audio_seconds"):
            log("audio", f"chunk {chunk['index']}: narration exists ({chunk['audio_seconds']:.2f}s), reusing")
            continue
        if should_cancel and should_cancel():
            raise RuntimeError("cancelled before narrating chunk %d" % chunk["index"])
        target.parent.mkdir(parents=True, exist_ok=True)
        result = narrate(chunk["text"], target)
        chunk["audio_seconds"] = media_duration(target) or result.get("duration_seconds")
        chunk["status"] = "narrated"
        save_manifest(run_dir, manifest)
        log("audio", f"chunk {chunk['index']}: {chunk['words']} words -> {chunk['audio_seconds']:.2f}s narration")
    concat_media([run_dir / c["narration"] for c in manifest["chunks"]], run_dir / "narration.wav")
    total = media_duration(run_dir / "narration.wav")
    manifest["audio_seconds"] = total
    save_manifest(run_dir, manifest)
    log("audio", f"{len(manifest['chunks'])} chunk narrations appended -> narration.wav {total:.2f}s")
    return {"audio_duration_seconds": total, "chunks": len(manifest["chunks"])}


def render_chunks(
    run_dir: Path,
    manifest: dict[str, Any],
    render: Callable[[str, Path, Path, Callable[[str], None]], dict[str, Any]],
    log: Callable[[str, str], None],
    should_cancel: Optional[Callable[[], bool]] = None,
    fetch_finished: Optional[Callable[[str, Path], Optional[float]]] = None,
    anchor: Optional[Path] = None,
) -> dict[str, Any]:
    """Render every chunk lacking a valid clip, composite its hero if it has one, then append into run_dir/video.mp4.

    A chunk whose render was queued before (prompt_id in the manifest) is first looked up in ComfyUI;
    a finished render is downloaded instead of rendered again. A chunk folder holding a hero image
    gets chunks/NN/composited.mp4 (hero keyed into the anchor's green panel), which is what gets appended.
    """

    def composite(chunk: dict[str, Any], clip: Path) -> None:
        hero = hero_for(clip.parent)
        composited = clip.with_name("composited.mp4")
        if hero is None or anchor is None:
            chunk.pop("composited", None)
            return
        if composited.is_file() and chunk.get("composited") and chunk.get("hero") == hero.name:
            log("video", f"chunk {chunk['index']}: composite exists, reusing")
            return
        info, described = composite_hero_by_mode(clip, hero, anchor, composited)
        chunk["hero"] = hero.name
        chunk["composited"] = str(composited.relative_to(run_dir).as_posix())
        save_manifest(run_dir, manifest)
        log("video", f"chunk {chunk['index']}: hero {hero.name} {described} -> composited.mp4")

    for chunk in manifest["chunks"]:
        target = run_dir / chunk["video"]
        if target.is_file() and chunk.get("video_seconds") and chunk.get("status") == "rendered":
            log("video", f"chunk {chunk['index']}: clip exists ({chunk['video_seconds']:.2f}s), reusing")
            composite(chunk, target)
            continue
        if should_cancel and should_cancel():
            raise RuntimeError("cancelled before rendering chunk %d" % chunk["index"])
        if chunk.get("prompt_id") and fetch_finished:
            duration = fetch_finished(chunk["prompt_id"], target)
            if duration:
                chunk["video_seconds"] = duration
                chunk["status"] = "rendered"
                save_manifest(run_dir, manifest)
                log("video", f"chunk {chunk['index']}: {duration:.2f}s clip fetched from ComfyUI")
                composite(chunk, target)
                continue
        log("video", f"chunk {chunk['index']}/{len(manifest['chunks'])}: rendering {chunk['words']} words, {chunk['audio_seconds']:.2f}s narration")

        def queued(prompt_id: str, chunk: dict[str, Any] = chunk) -> None:
            chunk["prompt_id"] = prompt_id
            save_manifest(run_dir, manifest)

        result = render(chunk["text"], run_dir / chunk["narration"], target.parent, queued)
        chunk["video_seconds"] = result.get("video_duration_seconds") or media_duration(target)
        chunk["status"] = "rendered"
        save_manifest(run_dir, manifest)
        log("video", f"chunk {chunk['index']}: {chunk['video_seconds']:.2f}s clip")
        composite(chunk, target)
    parts = [run_dir / (c.get("composited") or c["video"]) for c in manifest["chunks"]]
    final = run_dir / "video.mp4"
    concat_media(parts, final)
    total = media_duration(final)
    manifest["video_seconds"] = total
    save_manifest(run_dir, manifest)
    log("video", f"{len(parts)} chunk clips appended -> video.mp4 {total:.2f}s")
    return {"video_duration_seconds": total, "chunks": len(parts)}


def render_chunks_continuous(
    run_dir: Path,
    manifest: dict[str, Any],
    render: Callable[[str, Path, Path, Callable[[str], None], Optional[Path]], dict[str, Any]],
    log: Callable[[str, str], None],
    should_cancel: Optional[Callable[[], bool]] = None,
    fetch_finished: Optional[Callable[[str, Path], Optional[float]]] = None,
    anchor: Optional[Path] = None,
) -> dict[str, Any]:
    """render_chunks, plus continuity: each chunk starts from the last frame of the previous chunk.

    A copy of render_chunks (SEGMENT_CONTINUE_FROM_LAST_FRAME=true selects it) so the original path stays as it was.
    The first chunk starts from the anchor; every later chunk starts from chunks/NN/first-frame.png, the last frame
    of the clip before it, so the clips play as one continuous take instead of snapping back to the reference pose.

    A chunk whose render was queued before (prompt_id in the manifest) is first looked up in ComfyUI;
    a finished render is downloaded instead of rendered again. A chunk folder holding a hero image
    gets chunks/NN/composited.mp4 (hero keyed into the anchor's green panel), which is what gets appended.
    """

    def composite(chunk: dict[str, Any], clip: Path) -> None:
        hero = hero_for(clip.parent)
        composited = clip.with_name("composited.mp4")
        if hero is None or anchor is None:
            chunk.pop("composited", None)
            return
        if composited.is_file() and chunk.get("composited") and chunk.get("hero") == hero.name:
            log("video", f"chunk {chunk['index']}: composite exists, reusing")
            return
        info, described = composite_hero_by_mode(clip, hero, anchor, composited)
        chunk["hero"] = hero.name
        chunk["composited"] = str(composited.relative_to(run_dir).as_posix())
        save_manifest(run_dir, manifest)
        log("video", f"chunk {chunk['index']}: hero {hero.name} {described} -> composited.mp4")

    previous_clip: Optional[Path] = None
    for chunk in manifest["chunks"]:
        target = run_dir / chunk["video"]
        if target.is_file() and chunk.get("video_seconds") and chunk.get("status") == "rendered":
            log("video", f"chunk {chunk['index']}: clip exists ({chunk['video_seconds']:.2f}s), reusing")
            composite(chunk, target)
            previous_clip = target
            continue
        first_frame: Optional[Path] = None
        if previous_clip is not None and previous_clip.is_file():
            first_frame = last_frame(previous_clip, target.parent / "first-frame.png")
            chunk["first_frame"] = str(first_frame.relative_to(run_dir).as_posix())
            log("video", f"chunk {chunk['index']}: first frame taken from the end of chunk {chunk['index'] - 1}")
        if should_cancel and should_cancel():
            raise RuntimeError("cancelled before rendering chunk %d" % chunk["index"])
        if chunk.get("prompt_id") and fetch_finished:
            duration = fetch_finished(chunk["prompt_id"], target)
            if duration:
                chunk["video_seconds"] = duration
                chunk["status"] = "rendered"
                save_manifest(run_dir, manifest)
                log("video", f"chunk {chunk['index']}: {duration:.2f}s clip fetched from ComfyUI")
                composite(chunk, target)
                previous_clip = target
                continue
        log("video", f"chunk {chunk['index']}/{len(manifest['chunks'])}: rendering {chunk['words']} words, {chunk['audio_seconds']:.2f}s narration")

        def queued(prompt_id: str, chunk: dict[str, Any] = chunk) -> None:
            chunk["prompt_id"] = prompt_id
            save_manifest(run_dir, manifest)

        result = render(chunk["text"], run_dir / chunk["narration"], target.parent, queued, first_frame)
        chunk["video_seconds"] = result.get("video_duration_seconds") or media_duration(target)
        chunk["status"] = "rendered"
        save_manifest(run_dir, manifest)
        log("video", f"chunk {chunk['index']}: {chunk['video_seconds']:.2f}s clip")
        composite(chunk, target)
        previous_clip = target
    parts = [run_dir / (c.get("composited") or c["video"]) for c in manifest["chunks"]]
    final = run_dir / "video.mp4"
    concat_media(parts, final)
    total = media_duration(final)
    manifest["video_seconds"] = total
    save_manifest(run_dir, manifest)
    log("video", f"{len(parts)} chunk clips appended -> video.mp4 {total:.2f}s")
    return {"video_duration_seconds": total, "chunks": len(parts)}
