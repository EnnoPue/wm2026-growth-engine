"""
learning_engine.py — turn raw performance into learned preferences.

Aggregates the latest metrics per video and scores each value of several
dimensions (angle, template, hook_style, team_tier, post_hour) by how well it
performs. Results land in `learning_data` (0..1 `score`). The ranker and the
orchestrator consult these scores — with an ε exploration rate so the system
keeps testing non-optimal ideas instead of collapsing onto one format.

Public helpers:
    learning.update()                      -> recompute learning_data
    learning.score_for(dim, key, platform) -> 0..1 (0.5 prior if undertrained)
    learning.rank_variants(variant_ids)    -> reordered, ε-greedy
    team_multiplier(home, away)            -> ~0.85..1.2 for match ranking
"""
from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from database import LearningData, Match, Performance, Video, db
from settings import cfg, settings
from utils import clamp, get_logger

log = get_logger("learning_engine")

MIN_SAMPLES = cfg("learning.min_samples_to_trust", 12)
EPSILON = cfg("learning.exploration_rate", 0.15)


def _tier(team: str) -> str:
    teams = [t.lower() for t in settings.priority_team_list]
    n = (team or "").lower()
    for idx, t in enumerate(teams):
        if t and (t in n or n in t):
            return f"tier{1 + idx // 2}"  # tier1..tier4 for the 8 priority teams
    return "tier5"


class LearningEngine:
    # ---- recompute -------------------------------------------------------
    def update(self) -> dict[str, int]:
        # latest (max-views) performance per video
        latest: dict[str, Performance] = {}
        with db.session() as s:
            for p in s.scalars(select(Performance)).all():
                cur = latest.get(p.video_id)
                if cur is None or (p.views or 0) > (cur.views or 0):
                    latest[p.video_id] = p
            if not latest:
                log.info("learning_engine: no performance data yet")
                return {}

            videos = {v.id: v for v in s.scalars(select(Video).where(Video.id.in_(latest.keys()))).all()}
            matches = {m.id: m for m in s.scalars(select(Match)).all()}

        # accumulate per (dimension, key, platform)
        agg: dict[tuple[str, str, str], list[dict[str, float]]] = defaultdict(list)

        for vid, perf in latest.items():
            v = videos.get(vid)
            if not v:
                continue
            views = max(perf.views or 0, 1)
            engagement = (perf.likes + perf.comments + perf.shares) / views
            retention = perf.retention_pct or 0.0
            sample = {"views": views, "engagement": engagement, "retention": retention}
            platform = perf.platform or "all"

            keys: list[tuple[str, str]] = [
                ("angle", v.angle or "unknown"),
                ("template", v.template or "unknown"),
            ]
            meta = v.video_metadata or {}
            if meta.get("hook_style"):
                keys.append(("hook_style", meta["hook_style"]))
            m = matches.get(v.match_id)
            if m:
                keys.append(("team_tier", _tier(m.home_team)))
                keys.append(("team_tier", _tier(m.away_team)))
            if meta.get("post_hour") is not None:
                keys.append(("post_hour", str(meta["post_hour"])))

            for dim, key in keys:
                agg[(dim, key, "all")].append(sample)
                agg[(dim, key, platform)].append(sample)

        # normalise within each dimension+platform to a 0..1 desirability
        by_group: dict[tuple[str, str], list[tuple[str, dict]]] = defaultdict(list)
        for (dim, key, platform), samples in agg.items():
            avg = {
                "views": sum(s["views"] for s in samples) / len(samples),
                "engagement": sum(s["engagement"] for s in samples) / len(samples),
                "retention": sum(s["retention"] for s in samples) / len(samples),
                "n": len(samples),
            }
            by_group[(dim, platform)].append((key, avg))

        written = 0
        with db.session() as s:
            for (dim, platform), entries in by_group.items():
                vmax = max(e[1]["views"] for e in entries) or 1
                emax = max(e[1]["engagement"] for e in entries) or 1
                rmax = max(e[1]["retention"] for e in entries) or 1
                for key, avg in entries:
                    score = clamp(
                        0.5 * (avg["views"] / vmax) + 0.3 * (avg["engagement"] / emax) + 0.2 * (avg["retention"] / rmax),
                        0, 1,
                    )
                    row = s.scalar(
                        select(LearningData).where(
                            LearningData.dimension == dim, LearningData.key == key, LearningData.platform == platform
                        )
                    )
                    if row is None:
                        row = LearningData(dimension=dim, key=key, platform=platform)
                        s.add(row)
                    row.samples = int(avg["n"])
                    row.avg_views = avg["views"]
                    row.avg_engagement = avg["engagement"]
                    row.avg_retention = avg["retention"]
                    row.score = round(score, 4)
                    written += 1
        log.info("learning_engine: updated %d learned rows", written)
        return {"rows": written, "videos": len(latest)}

    # ---- query -----------------------------------------------------------
    def score_for(self, dimension: str, key: str, platform: str = "all") -> float:
        with db.session() as s:
            row = s.scalar(
                select(LearningData).where(
                    LearningData.dimension == dimension, LearningData.key == key, LearningData.platform == platform
                )
            )
        if not row or row.samples < MIN_SAMPLES:
            return 0.5  # neutral prior until we trust the data
        return float(row.score)

    def rank_variants(self, variant_ids: list[str], platform: str = "all") -> list[str]:
        """Order variants best-first by learned angle score, with ε exploration."""
        if random.random() < EPSILON:
            shuffled = list(variant_ids)
            random.shuffle(shuffled)
            return shuffled
        return sorted(variant_ids, key=lambda v: self.score_for("angle", v, platform), reverse=True)

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with db.session() as s:
            for row in s.scalars(select(LearningData).order_by(LearningData.dimension, LearningData.score.desc())).all():
                out.setdefault(row.dimension, []).append(
                    {"key": row.key, "platform": row.platform, "score": round(row.score, 3), "samples": row.samples}
                )
        return out


learning = LearningEngine()


def team_multiplier(home: str, away: str) -> float:
    """Used by match_ranker. 1.0 when untrained; nudges toward teams whose content
    has historically performed."""
    try:
        best = max(learning.score_for("team_tier", _tier(home)), learning.score_for("team_tier", _tier(away)))
        return clamp(0.85 + best * 0.4, 0.85, 1.25)
    except Exception:
        return 1.0
