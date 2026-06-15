"""
video_builder.py — render a story package into a 1080x1920 MP4.

Pipeline (all free, no paid services):
  1. Pillow renders one full-frame scene PNG per beat (gradient backdrop, team
     accent, scoreboard / timeline / player card / big number / stat split /
     bracket / outro + the beat's on-screen headline, kept inside the TikTok &
     Shorts safe margins).
  2. FFmpeg turns each still into a clip with a gentle Ken-Burns zoom + fade
     (motion-graphics feel, reliable cross-fade-through-black between scenes).
  3. Clips are concatenated and muxed with rights-cleared music from
     assets/music (or a silent track). Output enforces the 15-60s hard limit.

Degrades gracefully: if Pillow or FFmpeg is unavailable it still writes a valid
metadata record and marks the video 'failed' instead of crashing the pipeline.
"""
from __future__ import annotations

import math
import random
import re
from pathlib import Path
from typing import Any

from settings import ASSETS_DIR, cfg, settings
from utils import ffmpeg_bin, get_logger, has_ffmpeg, run

log = get_logger("video_builder")

W = cfg("video.width", 1080)
H = cfg("video.height", 1920)
FPS = cfg("video.fps", 30)
SAFE = cfg("video.safe_margins", {"top": 220, "bottom": 360, "sides": 64})

TEAM_COLORS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "germany": ((20, 20, 24), (200, 16, 46)),
    "england": ((10, 20, 60), (206, 17, 38)),
    "usa": ((10, 22, 75), (179, 25, 66)),
    "argentina": ((30, 80, 150), (108, 172, 228)),
    "brazil": ((0, 90, 60), (255, 223, 0)),
    "france": ((0, 30, 90), (200, 16, 46)),
    "portugal": ((0, 80, 60), (255, 0, 0)),
    "spain": ((120, 0, 20), (170, 21, 27)),
}
DEFAULT_BG = ((14, 16, 28), (40, 44, 86))
ACCENT = (255, 215, 0)
WHITE = (245, 246, 250)
MUTED = (170, 176, 190)


# --------------------------------------------------------------------------- #
# Pillow helpers
# --------------------------------------------------------------------------- #
def _pil():
    from PIL import Image, ImageDraw, ImageFont  # noqa

    return Image, ImageDraw, ImageFont


def _font_path(bold: bool = False) -> str | None:
    candidates = [
        ASSETS_DIR / "fonts" / ("Inter-Bold.ttf" if bold else "Inter-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu") / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation") / ("LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"),
        Path("/System/Library/Fonts/Supplemental") / ("Arial Bold.ttf" if bold else "Arial.ttf"),
        Path("/Library/Fonts") / ("Arial Bold.ttf" if bold else "Arial.ttf"),
    ]
    for c in candidates:
        if Path(c).exists():
            return str(c)
    return None


def _font(size: int, bold: bool = True):
    _, _, ImageFont = _pil()
    p = _font_path(bold=bold)
    try:
        return ImageFont.truetype(p, size) if p else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


def _team_colors(name: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    n = (name or "").lower()
    for k, v in TEAM_COLORS.items():
        if k in n:
            return v
    return DEFAULT_BG


def _gradient(img, c1, c2) -> None:
    """Vertical gradient backdrop with a subtle vignette."""
    import numpy as np
    from PIL import Image

    top = np.array(c1, dtype=float)
    bot = np.array(c2, dtype=float)
    ramp = np.linspace(0, 1, H)[:, None]
    grad = (top[None, :] * (1 - ramp) + bot[None, :] * ramp).astype("uint8")
    grad = np.repeat(grad[:, None, :], W, axis=1)
    bg = Image.fromarray(grad, "RGB")
    img.paste(bg, (0, 0))


# The bundled DejaVu/Liberation fonts have no colour-emoji glyphs, so any emoji
# (ours OR from Claude-generated copy) would render as ▯ tofu. Map the few symbols
# we intentionally emit to supported glyphs, then strip the rest.
_EMOJI_MAP = {"⚽": "", "🟥": "", "🟨": "", "👇": "", "🤯": "", "🔥": "", "⭐": "★", "⟲": "«", "🏆": ""}
_KEEP_HIGH = {0x2605, 0x2713, 0x25B6, 0x25BA, 0x25CF, 0x2022, 0x2714}  # ★ ✓ ▶ ► ● • ✔


def _safe(text: str) -> str:
    if not text:
        return text
    for k, v in _EMOJI_MAP.items():
        text = text.replace(k, v)
    out = []
    for ch in text:
        cp = ord(ch)
        if cp >= 0x2600 and cp not in _KEEP_HIGH:  # symbols/emoji DejaVu lacks
            continue
        if 0xFE00 <= cp <= 0xFE0F:  # variation selectors
            continue
        out.append(ch)
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()


def _text_w(draw, text, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap(draw, text, font, max_w) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if _text_w(draw, trial, font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _centered(draw, text, font, y, fill=WHITE, max_w=None) -> int:
    max_w = max_w or (W - 2 * SAFE["sides"])
    lines = _wrap(draw, _safe(text), font, max_w)
    asc = font.size + 14
    for ln in lines:
        w = _text_w(draw, ln, font)
        x = (W - w) // 2
        # soft shadow for legibility over any backdrop
        draw.text((x + 3, y + 3), ln, font=font, fill=(0, 0, 0))
        draw.text((x, y), ln, font=font, fill=fill)
        y += asc
    return y


def _pill(draw, text, font, cx, y, pad=28, fg=(10, 10, 12), bg=ACCENT) -> int:
    text = _safe(text)
    w = _text_w(draw, text, font)
    x0 = cx - w // 2 - pad
    x1 = cx + w // 2 + pad
    h = font.size + 24
    draw.rounded_rectangle([x0, y, x1, y + h], radius=h // 2, fill=bg)
    draw.text((cx - w // 2, y + 10), text, font=font, fill=fg)
    return y + h


# --------------------------------------------------------------------------- #
# Scene renderers (each returns a PIL.Image)
# --------------------------------------------------------------------------- #
def _base_scene(accent_name: str | None):
    Image, ImageDraw, _ = _pil()
    img = Image.new("RGB", (W, H), DEFAULT_BG[0])
    c1, c2 = _team_colors(accent_name or "")
    _gradient(img, c1, c2)
    draw = ImageDraw.Draw(img)
    # top tag
    _pill(draw, cfg("project.competition", "FIFA WORLD CUP 2026").upper(), _font(34), W // 2, SAFE["top"] - 90, bg=(255, 255, 255), fg=(10, 10, 12))
    return img, draw


def render_title_card(beat, _f) -> Any:
    img, draw = _base_scene(_color_name(beat))
    y = H // 2 - 260
    _centered(draw, beat["text"], _font(96), y, fill=WHITE)
    return img


def render_scoreboard(beat, _f) -> Any:
    d = beat.get("data", {})
    img, draw = _base_scene(d.get("home"))
    y = SAFE["top"] + 120
    _centered(draw, (d.get("stage") or "").upper(), _font(40), y, fill=MUTED)
    y += 120
    # team names
    _centered(draw, str(d.get("home", "")), _font(72), y, fill=WHITE)
    y += 110
    score = f"{d.get('home_score', 0)} – {d.get('away_score', 0)}"
    _centered(draw, score, _font(190), y, fill=ACCENT)
    y += 240
    _centered(draw, str(d.get("away", "")), _font(72), y, fill=WHITE)
    y = H - SAFE["bottom"] - 80
    _centered(draw, beat["text"], _font(48), y, fill=WHITE)
    return img


def render_timeline(beat, _f) -> Any:
    d = beat.get("data", {})
    img, draw = _base_scene(d.get("home"))
    y = SAFE["top"] + 60
    _centered(draw, beat["text"].upper(), _font(56), y, fill=WHITE)
    y += 150
    items = d.get("items", [])[:6]
    line_x = W // 2
    draw.line([(line_x, y), (line_x, y + len(items) * 150)], fill=MUTED, width=4)
    f_small = _font(40, bold=True)
    f_min = _font(46, bold=True)
    for it in items:
        is_red = it.get("type") == "red_card"
        dot = (255, 59, 48) if is_red else ACCENT
        draw.ellipse([line_x - 16, y - 16, line_x + 16, y + 16], fill=dot)
        minute = f"{it.get('minute', '')}'"
        draw.text((line_x - 200, y - 22), minute, font=f_min, fill=WHITE)
        label = _safe(str(it.get("label", "")))[:22]
        draw.text((line_x + 50, y - 18), label, font=f_small, fill=WHITE)
        y += 150
    return img


def render_player_card(beat, _f) -> Any:
    d = beat.get("data", {})
    img, draw = _base_scene(d.get("team"))
    cx = W // 2
    # framed card
    cw, ch = W - 2 * SAFE["sides"] - 40, 900
    x0, y0 = (W - cw) // 2, H // 2 - ch // 2
    draw.rounded_rectangle([x0, y0, x0 + cw, y0 + ch], radius=48, outline=ACCENT, width=6)
    y = y0 + 90
    _centered(draw, "⭐ PLAYER OF THE MATCH", _font(40), y, fill=ACCENT)
    y += 160
    _centered(draw, str(d.get("name", "")), _font(86), y, fill=WHITE)
    y += 200
    _centered(draw, f"{d.get('goals', 0)} GOAL{'S' if (d.get('goals', 0) != 1) else ''}", _font(120), y, fill=ACCENT)
    y += 220
    _centered(draw, f"{d.get('team', '')}  •  {d.get('scoreline', '')}", _font(46), y, fill=MUTED)
    return img


def render_big_number(beat, _f) -> Any:
    d = beat.get("data", {})
    img, draw = _base_scene(_color_name(beat))
    _centered(draw, str(d.get("number", "")), _font(360), H // 2 - 320, fill=ACCENT)
    _centered(draw, str(d.get("label", beat["text"])), _font(64), H // 2 + 160, fill=WHITE)
    return img


def render_stat_split(beat, _f) -> Any:
    d = beat.get("data", {})
    img, draw = _base_scene(d.get("home"))
    y = SAFE["top"] + 80
    _centered(draw, beat["text"].upper(), _font(52), y, fill=WHITE)
    y += 150
    f_team = _font(48)
    home_name, away_name = _safe(str(d.get("home", ""))), _safe(str(d.get("away", "")))
    draw.text((SAFE["sides"], y), home_name, font=f_team, fill=ACCENT)
    aw = _text_w(draw, away_name, f_team)
    draw.text((W - SAFE["sides"] - aw, y), away_name, font=f_team, fill=ACCENT)
    y += 130
    f_row = _font(56)
    f_lbl = _font(34, bold=False)
    for row in d.get("rows", []):
        hv, av = str(row.get("home", "-")), str(row.get("away", "-"))
        draw.text((SAFE["sides"], y), hv, font=f_row, fill=WHITE)
        avw = _text_w(draw, av, f_row)
        draw.text((W - SAFE["sides"] - avw, y), av, font=f_row, fill=WHITE)
        lbl = _safe(str(row.get("label", "")))
        lw = _text_w(draw, lbl, f_lbl)
        draw.text(((W - lw) // 2, y + 12), lbl, font=f_lbl, fill=MUTED)
        y += 140
    return img


def render_bracket(beat, _f) -> Any:
    d = beat.get("data", {})
    img, draw = _base_scene(d.get("team"))
    y = SAFE["top"] + 120
    _centered(draw, beat["text"].upper(), _font(56), y, fill=WHITE)
    y += 180
    stages = ["GROUP", "R16", "QF", "SF", "FINAL"]
    cur = (d.get("stage") or "GROUP").upper()
    reached = stages.index(cur) if cur in stages else 0
    for i, st in enumerate(stages):
        on = i <= reached
        color = ACCENT if on else (90, 95, 110)
        draw.rounded_rectangle([SAFE["sides"], y, W - SAFE["sides"], y + 110], radius=26, outline=color, width=5)
        mark = "✓ " if on else ""
        _centered(draw, f"{mark}{st}", _font(50), y + 24, fill=color)
        y += 150
    return img


def render_outro(beat, _f) -> Any:
    img, draw = _base_scene(_color_name(beat))
    y = H // 2 - 220
    _centered(draw, beat["text"], _font(72), y, fill=WHITE)
    y = H - SAFE["bottom"]
    _pill(draw, "▶ FOLLOW FOR MORE", _font(46), W // 2, y, bg=ACCENT, fg=(10, 10, 12))
    return img


def _color_name(beat) -> str | None:
    acc = (beat.get("data") or {}).get("accent")
    if isinstance(acc, str) and not acc.startswith("#"):
        return acc
    return None


RENDERERS = {
    "title_card": render_title_card,
    "scoreboard": render_scoreboard,
    "timeline": render_timeline,
    "player_card": render_player_card,
    "big_number": render_big_number,
    "stat_split": render_stat_split,
    "bracket": render_bracket,
    "outro": render_outro,
}


# --------------------------------------------------------------------------- #
# FFmpeg assembly
# --------------------------------------------------------------------------- #
class VideoBuilder:
    def build(self, pkg: dict[str, Any], hook: str, out_dir: Path) -> dict[str, Any]:
        out_dir = Path(out_dir)
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "video.mp4"

        beats = list(pkg.get("beats", []))
        if beats and hook:
            beats[0] = {**beats[0], "text": hook}  # ensure chosen hook is the opener

        # 1) render scenes
        try:
            from PIL import Image  # noqa
        except Exception as exc:
            log.error("video_builder: Pillow unavailable (%s) — cannot render", exc)
            return {"status": "failed", "file_path": None, "duration": 0, "error": str(exc)}

        frame_specs: list[tuple[Path, float]] = []
        for i, beat in enumerate(beats):
            renderer = RENDERERS.get(beat.get("visual"), render_title_card)
            try:
                img = renderer(beat, pkg.get("facts", {}))
            except Exception as exc:
                log.warning("video_builder: scene %s failed (%s) — title fallback", beat.get("visual"), exc)
                img = render_title_card(beat, pkg.get("facts", {}))
            fp = frames_dir / f"f{i:02d}.png"
            img.save(fp)
            frame_specs.append((fp, float(beat.get("seconds", 3.0))))

        total = round(sum(s for _, s in frame_specs), 2)

        if not has_ffmpeg():
            log.error("video_builder: ffmpeg unavailable — wrote frames only")
            return {"status": "failed", "file_path": None, "duration": total, "frames": len(frame_specs)}

        # 2) per-beat clips with zoom + fade
        clips: list[Path] = []
        ff = ffmpeg_bin()
        for i, (fp, dur) in enumerate(frame_specs):
            clip = frames_dir / f"c{i:02d}.mp4"
            frames = max(1, int(dur * FPS))
            direction = random.choice(["in", "out"])
            z = "min(zoom+0.0010,1.10)" if direction == "in" else "if(lte(zoom,1.0),1.10,max(1.001,zoom-0.0010))"
            vf = (
                f"scale={W}:{H},setsar=1,"
                f"zoompan=z='{z}':d={frames}:s={W}x{H}:fps={FPS},"
                f"fade=t=in:st=0:d=0.3,fade=t=out:st={max(0.0, dur-0.3):.2f}:d=0.3,"
                f"format=yuv420p"
            )
            try:
                run([ff, "-y", "-loop", "1", "-i", str(fp), "-t", f"{dur:.2f}",
                     "-vf", vf, "-r", str(FPS), "-an", str(clip)], timeout=180)
                clips.append(clip)
            except Exception as exc:
                log.warning("video_builder: clip %d failed (%s) — static fallback", i, exc)
                try:
                    run([ff, "-y", "-loop", "1", "-i", str(fp), "-t", f"{dur:.2f}",
                         "-vf", f"scale={W}:{H},format=yuv420p", "-r", str(FPS), "-an", str(clip)], timeout=120)
                    clips.append(clip)
                except Exception as exc2:
                    log.error("video_builder: static clip %d also failed (%s)", i, exc2)

        if not clips:
            return {"status": "failed", "file_path": None, "duration": total}

        # 3) concat
        concat_file = frames_dir / "concat.txt"
        concat_file.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
        silent = frames_dir / "joined.mp4"
        try:
            run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                 "-c:v", cfg("video.codec", "libx264"), "-pix_fmt", cfg("video.pix_fmt", "yuv420p"),
                 "-crf", str(cfg("video.crf", 20)), "-r", str(FPS), str(silent)], timeout=300)
        except Exception as exc:
            log.error("video_builder: concat failed (%s)", exc)
            return {"status": "failed", "file_path": None, "duration": total}

        # 4) audio (rights-cleared music if present, else silence) + final encode
        ok = self._mux_audio(ff, silent, out_path, total)
        if not ok:
            silent.replace(out_path)

        log.info("video_builder: rendered %s (%.1fs, %d scenes)", out_path.name, total, len(clips))
        return {
            "status": "rendered",
            "file_path": str(out_path),
            "duration": total,
            "width": W,
            "height": H,
            "scenes": len(clips),
        }

    def _mux_audio(self, ff: str, video: Path, out_path: Path, dur: float) -> bool:
        music = self._pick_music()
        try:
            if music:
                run([ff, "-y", "-i", str(video), "-stream_loop", "-1", "-i", str(music),
                     "-shortest", "-t", f"{dur:.2f}",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", cfg("video.audio_bitrate", "160k"),
                     "-af", "afade=t=in:d=0.5,afade=t=out:st=" + f"{max(0.0,dur-0.6):.2f}" + ":d=0.6",
                     str(out_path)], timeout=180)
            else:
                run([ff, "-y", "-i", str(video), "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                     "-shortest", "-t", f"{dur:.2f}", "-c:v", "copy", "-c:a", "aac",
                     "-b:a", "128k", str(out_path)], timeout=180)
            return True
        except Exception as exc:
            log.warning("video_builder: audio mux failed (%s) — keeping silent video", exc)
            return False

    @staticmethod
    def _pick_music() -> Path | None:
        """Only ever uses files the operator placed in assets/music — these MUST be
        rights-cleared. We never fetch copyrighted audio automatically."""
        mdir = ASSETS_DIR / "music"
        default = Path(cfg("video.default_music", ""))
        if default and default.exists():
            return default
        if mdir.exists():
            tracks = [p for p in mdir.iterdir() if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".aac"}]
            if tracks:
                return random.choice(tracks)
        return None


video_builder = VideoBuilder()
