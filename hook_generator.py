"""
hook_generator.py — multiple scroll-stopping opening hooks per video.

Claude first (varied, on-brand). Deterministic template fallback (config.yaml:
hook_templates) when Claude is unavailable. English only. Returns a ranked list;
index 0 is the chosen hook, the rest are A/B alternates stored for the learner.
"""
from __future__ import annotations

import random
from typing import Any

from ai_client import ai
from settings import cfg
from utils import get_logger, truncate

log = get_logger("hook_generator")

_SYSTEM = (
    "You write viral TikTok/YouTube Shorts hooks for football content. Each hook is "
    "one line, under 60 characters, English, designed to stop the scroll in the first "
    "second. Lean into drama, surprise, emotion, controversy and curiosity. Never use "
    "hashtags. Never invent fake scores or players."
)


def _fill(template: str, f: dict[str, Any]) -> str:
    a = f.get("winner") or f.get("home") or "They"
    b = f.get("loser") or f.get("away") or "them"
    late = f.get("late_goals") or []
    minute = (late[-1].get("minute") if late else None) or (f.get("goals") or [{}])[-1].get("minute", 90)
    total = (f.get("home_score") or 0) + (f.get("away_score") or 0)
    return (
        template.replace("{A}", str(a))
        .replace("{B}", str(b))
        .replace("{S}", str(f.get("scoreline", "")))
        .replace("{P}", str(f.get("top_scorer") or a))
        .replace("{M}", str(minute))
        .replace("{N}", str(total))
    )


def _template_hooks(f: dict[str, Any], n: int) -> list[str]:
    templates = list(cfg("hook_templates", []))
    random.shuffle(templates)
    seen, out = set(), []
    for t in templates:
        h = truncate(_fill(t, f), 60)
        if h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
        if len(out) >= n:
            break
    return out or ["Nobody saw this coming at the World Cup..."]


class HookGenerator:
    def generate(self, pkg: dict[str, Any], n: int = 6) -> list[dict[str, Any]]:
        f = pkg.get("facts", {})
        hooks: list[str] = []
        source = "template"

        if ai.available:
            prompt = (
                f"Match: {f.get('home')} {f.get('scoreline')} {f.get('away')} "
                f"(stage {f.get('stage')}). Angle: {pkg.get('angle')}. "
                f"Comeback: {f.get('comeback_team')}. Late goals: {len(f.get('late_goals', []))}. "
                f"Red cards: {len(f.get('reds', []))}. Top scorer: {f.get('top_scorer')}.\n\n"
                f"Write {n} DISTINCT one-line hooks for this video. "
                'Return JSON: {"hooks": ["...", "..."]}'
            )
            data = ai.json(prompt, system=_SYSTEM, temperature=1.0)
            if isinstance(data, dict) and isinstance(data.get("hooks"), list):
                hooks = [truncate(str(h).strip(), 60) for h in data["hooks"] if str(h).strip()]
                source = "claude"

        if not hooks:
            hooks = _template_hooks(f, n)

        # de-dupe, keep order, cap at n
        seen, ranked = set(), []
        for i, h in enumerate(hooks):
            k = h.lower()
            if k in seen:
                continue
            seen.add(k)
            ranked.append({"text": h, "source": source, "rank": len(ranked)})
            if len(ranked) >= n:
                break
        log.info("hook_generator: %d hooks (%s) for %s", len(ranked), source, pkg.get("variant"))
        return ranked


hook_generator = HookGenerator()
