"""
viral_scorer.py — score each video package 0..100 on:
    viral, emotional, story, retention, controversy, novelty
Heuristic baseline (always available) + optional Claude calibration (±). The
headline `viral` score is a weighted blend (config.yaml: viral_weights). Videos
below config min_publish_viral_score are kept as drafts, not auto-uploaded.
"""
from __future__ import annotations

from typing import Any

from ai_client import ai
from settings import cfg, settings
from utils import clamp, get_logger

log = get_logger("viral_scorer")


def _heuristic(pkg: dict[str, Any]) -> dict[str, float]:
    f = pkg.get("facts", {})
    angle = pkg.get("angle", "")
    margin = f.get("margin", 0)
    total_goals = (f.get("home_score") or 0) + (f.get("away_score") or 0)
    late = len(f.get("late_goals", []))
    reds = len(f.get("reds", []))
    knockout = bool(f.get("is_knockout"))
    comeback = bool(f.get("comeback_team"))
    priority = bool(f.get("priority_home") or f.get("priority_away"))

    emotional = 35 + 18 * late + (15 if comeback else 0) + (12 if knockout else 0) + (10 if priority else 0)
    story = 30 + (25 if comeback else 0) + (20 if angle in {"underdog", "comeback"} else 0) + (10 if f.get("top_scorer") else 0) + (10 if knockout else 0)
    retention = 45 + 6 * min(len(pkg.get("beats", [])), 6) + (12 if pkg.get("ai_polished") else 0) + (8 if late else 0)
    controversy = 20 + 28 * reds + (18 if margin <= 1 and total_goals >= 2 else 0) + (12 if f.get("is_draw") else 0)
    novelty = 30 + (28 if (angle == "underdog") else 0) + min(total_goals, 6) * 5 + (10 if margin >= 3 else 0)

    return {
        "emotional": clamp(emotional, 0, 100),
        "story": clamp(story, 0, 100),
        "retention": clamp(retention, 0, 100),
        "controversy": clamp(controversy, 0, 100),
        "novelty": clamp(novelty, 0, 100),
    }


def _blend(scores: dict[str, float]) -> float:
    w = cfg("viral_weights", {})
    total_w = sum(float(w.get(k, 0)) for k in scores) or 1
    return clamp(sum(scores[k] * float(w.get(k, 0)) for k in scores) / total_w, 0, 100)


class ViralScorer:
    def score(self, pkg: dict[str, Any]) -> dict[str, Any]:
        scores = _heuristic(pkg)
        rationale = "heuristic baseline"

        if ai.available:
            f = pkg.get("facts", {})
            prompt = (
                f"Rate this football Short 0-100 on each axis. Match: {f.get('home')} "
                f"{f.get('scoreline')} {f.get('away')} ({f.get('stage')}). Angle: {pkg.get('angle')}. "
                f"Hook/title: {pkg.get('title')}. Comeback: {f.get('comeback_team')}, late goals: "
                f"{len(f.get('late_goals', []))}, reds: {len(f.get('reds', []))}.\n"
                "Axes: emotional, story, retention, controversy, novelty.\n"
                'Return JSON: {"emotional":n,"story":n,"retention":n,"controversy":n,'
                '"novelty":n,"rationale":"one short sentence"}'
            )
            data = ai.json(prompt, system="You are a precise viral-content scorer for football Shorts.", temperature=0.4)
            if isinstance(data, dict):
                for k in scores:
                    try:
                        # average heuristic + model for stability
                        scores[k] = clamp((scores[k] + float(data.get(k, scores[k]))) / 2, 0, 100)
                    except Exception:
                        pass
                rationale = str(data.get("rationale", rationale))[:240]

        viral = _blend(scores)
        result = {
            "viral": round(viral, 1),
            **{k: round(v, 1) for k, v in scores.items()},
            "rationale": rationale,
            "publishable": viral >= cfg("min_publish_viral_score", 55),
        }
        log.info("viral_scorer: %s -> viral=%.1f (publishable=%s)", pkg.get("variant"), result["viral"], result["publishable"])
        return result


viral_scorer = ViralScorer()
