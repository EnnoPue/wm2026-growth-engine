"""
subtitle_generator.py — timed captions for each video.

Two modes:
  • Default (no audio narration): derive an SRT directly from the beat timings,
    so captions.json + subtitles.srt always exist and stay in sync with scenes.
  • If a narration/voiceover audio file is supplied AND faster-whisper is
    installed, transcribe it locally (free, no API) for word-accurate captions.

Returns the captions list and writes subtitles.srt next to the video.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from utils import get_logger

log = get_logger("subtitle_generator")


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _to_srt(cues: list[dict[str, Any]]) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{c['text']}\n")
    return "\n".join(out)


class SubtitleGenerator:
    def from_beats(self, beats: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cues, t = [], 0.0
        for b in beats:
            dur = float(b.get("seconds", 3.0))
            text = str(b.get("text", "")).strip()
            if text:
                cues.append({"start": round(t, 2), "end": round(t + dur, 2), "text": text})
            t += dur
        return cues

    def from_audio(self, audio_path: str | Path) -> list[dict[str, Any]] | None:
        """Local transcription with faster-whisper. None if unavailable."""
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            log.info("subtitle_generator: faster-whisper unavailable (%s)", exc)
            return None
        try:
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments, _info = model.transcribe(str(audio_path), language="en", vad_filter=True)
            return [
                {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
                for s in segments
                if s.text.strip()
            ]
        except Exception as exc:
            log.warning("subtitle_generator: transcription failed (%s)", exc)
            return None

    def generate(
        self,
        pkg: dict[str, Any],
        out_dir: str | Path,
        audio_path: str | Path | None = None,
    ) -> dict[str, Any]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        cues = None
        source = "beats"
        if audio_path and Path(audio_path).exists():
            cues = self.from_audio(audio_path)
            if cues:
                source = "whisper"
        if not cues:
            cues = self.from_beats(pkg.get("beats", []))

        srt_path = out_dir / "subtitles.srt"
        srt_path.write_text(_to_srt(cues), encoding="utf-8")
        log.info("subtitle_generator: %d cues (%s) -> %s", len(cues), source, srt_path.name)
        return {"language": "en", "source": source, "srt_path": str(srt_path), "cues": cues}


subtitle_generator = SubtitleGenerator()
