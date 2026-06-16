"""
story_engine.py — Story Engine Mode.

Takes a match and produces N original story packages (one per configured video
variant). The deterministic skeleton comes from fallback_content_engine; when
Claude is available it rewrites the on-screen copy to be punchier and more viral
WITHOUT inventing facts and WITHOUT changing the beat structure (so the renderer
and the data stay in sync). English only.
"""
from __future__ import annotations

import json
from typing import Any

from ai_client import ai
from fallback_content_engine import build_package
from settings import cfg, settings
from utils import get_logger, truncate

log = get_logger("story_engine")

_SYSTEM = (
    "You are a world-class short-form football editor writing for TikTok and "
    "YouTube Shorts. You write tight, punchy, emotional English copy that maximises "
    "retention. You NEVER invent facts (scores, players, minutes) — you only "
    "rephrase what you are given. Output English only."
)


def _polish(pkg: dict[str, Any]) -> dict[str, Any]:
    """Use Claude to rewrite beat copy + title. Returns pkg unchanged on failure."""
    if not ai.available:
        return pkg

    beats_min = [{"id": b["id"], "visual": b["visual"], "text": b["text"]} for b in pkg["beats"]]
    facts = pkg["facts"]
    prompt = (
        f"Match: {facts['home']} {facts['scoreline']} {facts['away']} "
        f"(stage: {facts['stage']}, winner: {facts['winner'] or 'draw'}).\n"
        f"Key facts: comeback_team={facts['comeback_team']}, late_goals={len(facts['late_goals'])}, "
        f"red_cards={len(facts['reds'])}, top_scorer={facts['top_scorer']} "
        f"({facts['top_scorer_goals']} goals).\n"
        f"Content angle: {pkg['angle']}.\n\n"
        "Rewrite the on-screen text for each beat below to be more viral, emotional and "
        "scroll-stopping. Keep each line under 70 characters. Keep the SAME number of beats "
        "and the SAME ids. Do not change any numbers, names, scores or minutes. The first "
        "beat is the hook — make it irresistible.\n\n"
        f"BEATS:\n{json.dumps(beats_min, ensure_ascii=False)}\n\n"
        'Return JSON: {"title": "...", "beats": [{"id": "...", "text": "..."}, ...]}'
    )
    data = ai.json(prompt, system=_SYSTEM, temperature=0.95)
    if not isinstance(data, dict):
        return pkg
    new_beats = {b.get("id"): b.get("text") for b in data.get("beats", []) if isinstance(b, dict)}
    if not new_beats:
        return pkg

    for b in pkg["beats"]:
        nt = new_beats.get(b["id"])
        if nt and isinstance(nt, str):
            b["text"] = truncate(nt.strip(), 90)
    if data.get("title"):
        pkg["title"] = truncate(str(data["title"]).strip(), 110)
    pkg["ai_polished"] = True
    pkg["narrative"] = " ".join(b["text"] for b in pkg["beats"])
    return pkg


class StoryEngine:
    def generate_all(self, match: dict[str, Any], broll_assets: list[dict] | None = None) -> list[dict[str, Any]]:
        """Produce one package per configured variant (default 5)."""
        variants = cfg("video_variants", [])
        if not variants:
            variants = [{"id": "drama", "angle": "drama", "template": "timeline_reveal"}]
        # Cost control: each variant calls Claude — cap at VIDEOS_PER_MATCH.
        variants = variants[: max(1, int(settings.videos_per_match))]
        packages: list[dict[str, Any]] = []
        for vc in variants:
            try:
                pkg = build_package(match, vc)
                pkg = _polish(pkg)
                if broll_assets:
                    pkg["broll_assets"] = broll_assets[:3]
                packages.append(pkg)
            except Exception as exc:  # noqa: BLE001
                log.warning("story_engine: variant %s failed (%s)", vc.get("id"), exc)
        log.info(
            "story_engine: %d packages for %s (ai_polished=%s)",
            len(packages),
            match.get("id"),
            ai.available,
        )
        return packages

    def generate_one(self, match: dict[str, Any], variant_id: str) -> dict[str, Any] | None:
        for vc in cfg("video_variants", []):
            if vc.get("id") == variant_id:
                return _polish(build_package(match, vc))
        return None


story_engine = StoryEngine()
