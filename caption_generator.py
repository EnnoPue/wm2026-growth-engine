"""
caption_generator.py — platform-native captions, titles, descriptions, hashtags,
tags. TikTok is optimised for the hook + FYP discovery; YouTube Shorts for search
intent + watch time. Claude first, deterministic fallback otherwise. English only.
"""
from __future__ import annotations

from typing import Any

from ai_client import ai
from settings import cfg, settings
from utils import get_logger, truncate

log = get_logger("caption_generator")

_SYSTEM = (
    "You write platform-native copy for football Shorts. TikTok captions are short, "
    "punchy and curiosity-driven. YouTube Short titles are search-friendly AND "
    "curiosity-driven (front-load the key terms). English only. Never promise footage "
    "you don't show; this content uses original graphics and data."
)


def _hashtags(f: dict[str, Any], platform: str) -> list[str]:
    pools = cfg("hashtags", {})
    tags = list(pools.get("core", []))
    tags += list(pools.get("tiktok_extra" if platform == "tiktok" else "youtube_extra", []))
    team_tags = pools.get("team_tags", {})
    for team in (f.get("home"), f.get("away")):
        for prio, tlist in team_tags.items():
            if team and prio.lower() in team.lower():
                tags += tlist
    # de-dupe preserve order, cap (TikTok ~5-8 is ideal, YT a few)
    seen, out = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[: 8 if platform == "tiktok" else 6]


def _yt_tags(f: dict[str, Any]) -> list[str]:
    base = [
        "world cup 2026",
        "world cup highlights",
        f"{f.get('home','')} vs {f.get('away','')}".strip(),
        f.get("home", ""),
        f.get("away", ""),
        f.get("top_scorer", "") or "",
        "football shorts",
        "soccer",
        f.get("stage", "") or "",
    ]
    return [t for t in dict.fromkeys([b.strip() for b in base if b and b.strip()])]


def _fallback(pkg: dict[str, Any], hook: str) -> dict[str, Any]:
    f = pkg["facts"]
    match_str = f"{f['home']} {f['scoreline']} {f['away']}"
    tiktok_cap = truncate(f"{hook} {match_str} 🤯", 150)
    yt_title = truncate(f"{match_str} — {hook}", 95)
    yt_desc = (
        f"{hook}\n\n{match_str} | FIFA World Cup 2026 ({f.get('stage','')}).\n"
        "Original data-driven recap (motion graphics, no broadcast footage).\n"
        "Subscribe for every World Cup 2026 moment.\n"
    )
    return {"tiktok_caption": tiktok_cap, "yt_title": yt_title, "yt_description": yt_desc}


class CaptionGenerator:
    def generate(self, pkg: dict[str, Any], hook: str) -> dict[str, Any]:
        f = pkg["facts"]
        copy = _fallback(pkg, hook)

        if ai.available:
            prompt = (
                f"Match: {f['home']} {f['scoreline']} {f['away']} ({f.get('stage')}). "
                f"Angle: {pkg.get('angle')}. Chosen hook: \"{hook}\".\n\n"
                "Produce: a TikTok caption (<=150 chars, no hashtags, 0-2 emojis), a YouTube "
                "Short title (<=95 chars, search-friendly + curiosity), and a 2-3 line YouTube "
                "description (no hashtags).\n"
                'Return JSON: {"tiktok_caption": "...", "yt_title": "...", "yt_description": "..."}'
            )
            data = ai.json(prompt, system=_SYSTEM, temperature=0.9)
            if isinstance(data, dict):
                copy["tiktok_caption"] = truncate(str(data.get("tiktok_caption") or copy["tiktok_caption"]), 150)
                copy["yt_title"] = truncate(str(data.get("yt_title") or copy["yt_title"]), 95)
                copy["yt_description"] = str(data.get("yt_description") or copy["yt_description"])

        tiktok_tags = _hashtags(f, "tiktok")
        yt_tags = _hashtags(f, "youtube_shorts")
        # YouTube Shorts: hashtags in description help; ensure #Shorts present.
        if "#Shorts" not in yt_tags and "#shorts" not in [t.lower() for t in yt_tags]:
            yt_tags = ["#Shorts"] + yt_tags

        result = {
            "tiktok": {
                "caption": f"{copy['tiktok_caption']} {' '.join(tiktok_tags)}".strip(),
                "caption_text": copy["tiktok_caption"],
                "hashtags": tiktok_tags,
            },
            "youtube_shorts": {
                "title": copy["yt_title"] if "#shorts" in copy["yt_title"].lower() else f"{copy['yt_title']} #Shorts",
                "description": f"{copy['yt_description']}\n\n{' '.join(yt_tags)}",
                "hashtags": yt_tags,
                "tags": _yt_tags(f),
            },
        }
        log.info("caption_generator: built captions for %s", pkg.get("variant"))
        return result


caption_generator = CaptionGenerator()
