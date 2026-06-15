"""
match_ranker.py — decide which matches become content first.

Combines static drama heuristics (config.yaml: ranking_weights) with the team
priority list and the learned per-team desirability from learning_engine. Pure
`score_match()` is unit-testable; `select_unprocessed()` pulls finished, not-yet
-processed matches from the DB ranked best-first.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from database import Match, db
from settings import cfg, settings
from utils import clamp, get_logger

log = get_logger("match_ranker")


def _team_priority_score(home: str, away: str) -> float:
    """1.0 if the #1 priority team plays, decaying down the list; 0 if neither."""
    teams = [t.lower() for t in settings.priority_team_list]
    best = 0.0
    for name in (home, away):
        n = (name or "").lower()
        for idx, t in enumerate(teams):
            if t and (t in n or n in t):
                best = max(best, 1.0 - (idx / max(len(teams), 1)) * 0.7)
    return best


def _goal_signal(home_score: int, away_score: int) -> tuple[float, float, float]:
    total = (home_score or 0) + (away_score or 0)
    margin = abs((home_score or 0) - (away_score or 0))
    goals_total = clamp(total / 6.0, 0, 1)  # 6 goals ~ ceiling
    # Drama peaks at a 1-goal thriller, dips at 2, rises again for blowouts (3+).
    if margin <= 1:
        margin_drama = 1.0
    elif margin == 2:
        margin_drama = 0.45
    else:
        margin_drama = clamp(0.6 + (margin - 3) * 0.13, 0, 1)
    return goals_total, margin_drama, margin


def _late_drama(events: list[dict]) -> float:
    late = [e for e in events if (e.get("minute") or 0) >= 80 and e.get("type") in {"goal", "red_card", "penalty", "own_goal"}]
    return clamp(len(late) / 2.0, 0, 1)


def _red_cards(events: list[dict]) -> float:
    reds = [e for e in events if e.get("type") == "red_card"]
    return clamp(len(reds) / 2.0, 0, 1)


def _comeback(events: list[dict], home: str, away: str) -> float:
    """Detect a lead change (trailing side ends up winning/level after being down)."""
    h = a = 0
    lead_history = []
    for e in sorted(events, key=lambda x: x.get("minute") or 0):
        if e.get("type") in {"goal", "penalty", "own_goal"}:
            team = (e.get("team") or "").lower()
            if team and team in (home or "").lower():
                h += 1
            elif team and team in (away or "").lower():
                a += 1
            lead_history.append(1 if h > a else (-1 if a > h else 0))
    # comeback if the eventual leader was once trailing
    if not lead_history:
        return 0.0
    final = lead_history[-1]
    if final == 1 and -1 in lead_history:
        return 1.0
    if final == -1 and 1 in lead_history:
        return 1.0
    return 0.0


def _knockout(stage: str | None) -> float:
    s = (stage or "").upper()
    if any(k in s for k in ("FINAL", "SEMI", "QUARTER", "16", "KNOCK", "R16", "QF", "SF")):
        return 1.0
    if "GROUP" in s:
        return 0.2
    return 0.4


def score_match(match: dict[str, Any]) -> float:
    """Return a 0..100 desirability score for a match dict."""
    w = cfg("ranking_weights", {})
    events = match.get("events") or []
    home, away = match.get("home_team", ""), match.get("away_team", "")
    goals_total, margin_drama, _ = _goal_signal(match.get("home_score") or 0, match.get("away_score") or 0)

    components = {
        "team_priority": _team_priority_score(home, away),
        "goals_total": goals_total,
        "goal_margin_drama": margin_drama,
        "late_drama": _late_drama(events),
        "red_cards": _red_cards(events),
        "knockout_stage": _knockout(match.get("stage")),
        "comeback": _comeback(events, home, away),
    }
    base = sum(components[k] * float(w.get(k, 0)) for k in components)

    # Learned multiplier from past performance of these teams (1.0 if untrained).
    try:
        from learning_engine import team_multiplier

        base *= team_multiplier(home, away)
    except Exception:
        pass

    return round(clamp(base, 0, 1) * 100, 2)


class MatchRanker:
    def rank_all(self) -> None:
        """Recompute and persist rank_score for every match."""
        with db.session() as s:
            for m in s.scalars(select(Match)).all():
                payload = {
                    "home_team": m.home_team,
                    "away_team": m.away_team,
                    "home_score": m.home_score,
                    "away_score": m.away_score,
                    "events": m.events or [],
                    "stage": m.stage,
                }
                m.rank_score = score_match(payload)

    def select_unprocessed(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Finished, not-yet-processed matches, best score first."""
        limit = limit or cfg("scheduler.max_matches_per_cycle", 6)
        self.rank_all()
        out: list[dict[str, Any]] = []
        with db.session() as s:
            rows = s.scalars(
                select(Match)
                .where(Match.status == "FINISHED", Match.processed.is_(False))
                .order_by(Match.rank_score.desc())
                .limit(limit)
            ).all()
            for m in rows:
                out.append(
                    {
                        "id": m.id,
                        "provider": m.provider,
                        "competition": m.competition,
                        "stage": m.stage,
                        "home_team": m.home_team,
                        "away_team": m.away_team,
                        "home_score": m.home_score,
                        "away_score": m.away_score,
                        "status": m.status,
                        "utc_kickoff": m.utc_kickoff,
                        "events": m.events or [],
                        "stats": m.stats or {},
                        "rank_score": m.rank_score,
                    }
                )
        log.info("match_ranker: selected %d matches", len(out))
        return out

    def mark_processed(self, match_id: str) -> None:
        with db.session() as s:
            m = s.get(Match, match_id)
            if m:
                m.processed = True


ranker = MatchRanker()
