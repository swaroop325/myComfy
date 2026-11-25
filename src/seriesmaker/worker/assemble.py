"""Assemble scene images + narration into a 1080x1920 H.264/AAC reel.

Each scene gets a zoompan (pan/zoom) over its still image and a burned-in
caption. Captions are rendered to a transparent PNG with Pillow and overlaid;
caption text is never interpolated into an ffmpeg filtergraph (injection-safe).
"""
from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

W, H, FPS = 1080, 1920, 30

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]


@dataclass
class SceneAsset:
    image_path: str
    audio_path: str
    duration_s: float
    caption: str


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-500:]}")


def _find_font() -> str | None:
    return next((f for f in FONT_CANDIDATES if Path(f).exists()), None)


def _wrap(text: str, draw, font, max_w: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _caption_png(text: str, path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    font_file = _find_font()
    font = (ImageFont.truetype(font_file, 64) if font_file
            else ImageFont.load_default(size=64))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lines = _wrap(text, d, font, 960)
    line_h = 80
    y = H - 420 - line_h * len(lines)
    for ln in lines:
        w = d.textlength(ln, font=font)
        d.text(((W - w) / 2, y), ln, font=font, fill="white",
               stroke_width=6, stroke_fill="black")
        y += line_h
    img.save(path)


def _motion(i: int, frames: int, motion: str | None) -> str:
    """zoompan expression: zoom AND x/y vary over the scene (on = output frame index)."""
    p = f"on/{frames}"
    if motion == "slow_pan":
        z, x, y = "1.25", f"(iw-iw/zoom)*{p}", "(ih-ih/zoom)/2"
    elif motion == "slow_zoom_in":
        z, x, y = f"1+0.2*{p}", f"(iw-iw/zoom)*(0.5-0.1*{p})", f"(ih-ih/zoom)*(0.5-0.1*{p})"
    elif i % 2 == 0:  # default: alternate by scene, drifting while zooming
        z, x, y = f"1.05+0.15*{p}", f"(iw-iw/zoom)*{p}", f"(ih-ih/zoom)*(1-{p})"
    else:
        z, x, y = f"1.2-0.15*{p}", f"(iw-iw/zoom)*(1-{p})", f"(ih-ih/zoom)*{p}"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS}"


def _scene_clip(i: int, a: SceneAsset, out: Path, tmp: Path, motion: str | None) -> None:
    """Caption text never enters the filtergraph: it is rendered to a PNG by Pillow."""
    frames = max(1, round(a.duration_s * FPS))
    base = (f"[0:v]scale=1620:2880:force_original_aspect_ratio=increase,"
            f"crop=1620:2880,{_motion(i, frames, motion)},setsar=1,format=yuv420p")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", a.image_path, "-i", a.audio_path]
    if a.caption:
        png = tmp / f"cap{i}.png"
        _caption_png(a.caption, png)
        cmd += ["-i", str(png)]
        fc = f"{base}[b];[b][2:v]overlay=0:0[v]"
    else:
        fc = f"{base}[v]"
    cmd += ["-filter_complex", fc + f";[1:a]aresample=44100,apad,atrim=0:{a.duration_s}[a]",
            "-map", "[v]", "-map", "[a]", "-t", str(a.duration_s), "-r", str(FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out)]
    _run(cmd)


def assemble_reel(assets: list[SceneAsset], music_path: str | None, out_path: str,
                  motion: str | None = None) -> None:
    if not assets:
        raise ValueError("no scenes to assemble")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        clips = []
        for i, a in enumerate(assets):
            clip = tmp / f"clip{i}.mp4"
            _scene_clip(i, a, clip, tmp, motion)
            clips.append(clip)
        listing = tmp / "list.txt"
        listing.write_text("".join(f"file '{c}'\n" for c in clips))
        total = sum(a.duration_s for a in assets)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing)]
        if music_path:
            cmd += ["-stream_loop", "-1", "-i", music_path,
                    "-filter_complex",
                    "[1:a]aresample=44100,volume=0.12[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-t", f"{total:.3f}", "-movflags", "+faststart", out_path]
        try:
            _run(cmd)
        except Exception:
            Path(out_path).unlink(missing_ok=True)
            raise
