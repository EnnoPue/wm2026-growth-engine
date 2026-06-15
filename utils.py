"""
utils.py — shared helpers: logging, JSON IO, slugs, timing, safe subprocess,
retry, and small text utilities used across the pipeline.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from settings import LOG_DIR, settings

try:
    from rich.logging import RichHandler

    _HANDLER: logging.Handler = RichHandler(rich_tracebacks=True, show_path=False)
    _FMT = "%(message)s"
except Exception:  # pragma: no cover - rich optional
    _HANDLER = logging.StreamHandler()
    _FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

_LOG_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    """Module logger. Streams (Rich if available) + rotating file."""
    global _LOG_CONFIGURED
    logger = logging.getLogger(name)
    if not _LOG_CONFIGURED:
        level = getattr(logging, settings.log_level.upper(), logging.INFO)
        root = logging.getLogger()
        root.setLevel(level)

        _HANDLER.setLevel(level)
        _HANDLER.setFormatter(logging.Formatter(_FMT, datefmt="%H:%M:%S"))
        root.addHandler(_HANDLER)

        try:
            from logging.handlers import RotatingFileHandler

            fh = RotatingFileHandler(
                LOG_DIR / "engine.log", maxBytes=5_000_000, backupCount=4, encoding="utf-8"
            )
            fh.setFormatter(
                logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
            )
            fh.setLevel(level)
            root.addHandler(fh)
        except Exception:
            pass

        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("apscheduler").setLevel(logging.WARNING)
        _LOG_CONFIGURED = True
    return logger


log = get_logger("utils")


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat()


def new_id(prefix: str = "") -> str:
    base = uuid.uuid4().hex[:12]
    return f"{prefix}{base}" if prefix else base


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
def slugify(text: str, maxlen: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", (text or "").lower()).strip()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:maxlen].strip("-") or "item"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def first_json_block(text: str) -> Any:
    """Extract the first JSON object/array from an LLM response, tolerating
    markdown fences and surrounding prose. Returns None on failure."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    start = min(
        [i for i in (candidate.find("{"), candidate.find("[")) if i != -1] or [-1]
    )
    if start == -1:
        return None
    depth = 0
    opener = candidate[start]
    closer = "}" if opener == "{" else "]"
    for i in range(start, len(candidate)):
        c = candidate[i]
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(candidate[start : i + 1])
                except Exception:
                    return None
    return None


# --------------------------------------------------------------------------- #
# JSON IO
# --------------------------------------------------------------------------- #
def write_json(path: str | Path, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
    return path


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# Subprocess / ffmpeg
# --------------------------------------------------------------------------- #
def run(cmd: list[str], timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output. Logs the command at debug level."""
    log.debug("exec: %s", " ".join(str(c) for c in cmd))
    proc = subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(map(str, cmd[:3]))} ...\n"
            f"stderr: {proc.stderr[-1500:]}"
        )
    return proc


def ffmpeg_bin() -> str:
    """Return a usable ffmpeg path: system ffmpeg, else the bundled
    imageio-ffmpeg static binary. Never raises."""
    import shutil

    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"  # last resort; caller handles failure


def has_ffmpeg() -> bool:
    try:
        run([ffmpeg_bin(), "-version"], timeout=20)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #
def retry(
    fn: Callable[[], Any],
    attempts: int = 3,
    backoff: Iterable[float] = (1, 3, 8),
    on_error: Callable[[Exception, int], None] | None = None,
) -> Any:
    """Call fn() with simple backoff. Returns fn() or raises the last error."""
    delays = list(backoff)
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if on_error:
                on_error(exc, i)
            else:
                log.warning("attempt %d/%d failed: %s", i + 1, attempts, exc)
            if i < attempts - 1:
                time.sleep(delays[min(i, len(delays) - 1)])
    assert last is not None
    raise last
