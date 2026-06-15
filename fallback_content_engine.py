"""
fallback_content_engine.py — deterministic, AI-free story construction.

Turns a normalised match dict into derived facts and a per-variant list of
"beats" (the timed scenes + subtitle lines that video_builder renders). This is
the backbone of Story Engine Mode: it works with zero API keys and zero footage.
story_engine.py layers Claude polish on top of these beats when available.

A "beat":
    {"id", "text", "seconds", "visual", "data"}
visual ∈ {title_card, scoreboard, timeline, player_card, big_number,
          stat_split, bracket, quote, outro}
"""
from __future__ import annotations

from typing import Any

from settings import cfg, settings
from utils import clamp, get_logger

log = get_logger("fallback_engine")

PRIORITY = lambda: settings.priority_team_list  # noqa: E731


# --------------------------------------------------------------------------- #
# Fact extraction
# --------------------------------------------------------------------------- #
def summarize(match: dict[str, Any]) -> dict[str, Any]:
    home, away = match.get("home_team", "Home"), match.get("away_team", "Away")
    hs, as_ = match.get("home_score") or 0, match.get("away_score") or 0
    events = sorted(match.get("events") or [], key=lambda e: e.get("minute") or 0)
    goals = [e for e in events if e.get("type") in {"goal", "penalty", "own_goal"}]
    reds = [e for e in events if e.get("type") == "red_card"]
    late = [e for e in goals if (e.get("minute") or 0) >= 80]

    winner = home if hs > as_ else (away if as_ > hs else None)
    loser = away if winner == home else (home if winner == away else None)
    margin = abs(hs - as_)

    # comeback / lead changes
    h = a = 0
    leads = []
    for g in goals:
        t = (g.get("team") or "").lower()
        if t and t in home.lower():
            h += 1
        elif t and t in away.lower():
            a += 1
        leads.append(1 if h > a else (-1 if a > h else 0))
    comeback_team = None
    if leads:
        if leads[-1] == 1 and -1 in leads:
            comeback_team = home
        elif leads[-1] == -1 and 1 in leads:
            comeback_team = away

    top_scorer = None
    counts: dict[str, int] = {}
    for g in goals:
        p = g.get("player")
        if p:
            counts[p] = counts.get(p, 0) + 1
    if counts:
        top_scorer = max(counts.items(), key=lambda kv: kv[1])

    pr = [t.lower() for t in PRIORITY()]
    def is_priority(name: str) -> bool:
        n = name.lower()
        return any(t and (t in n or n in t) for t in pr)

    return {
        "home": home,
        "away": away,
        "home_score": hs,
        "away_score": as_,
        "scoreline": f"{hs}-{as_}",
        "winner": winner,
        "loser": loser,
        "margin": margin,
        "is_draw": winner is None,
        "events": events,
        "goals": goals,
        "reds": reds,
        "late_goals": late,
        "has_late_drama": bool(late) or bool([r for r in reds if (r.get("minute") or 0) >= 80]),
        "comeback_team": comeback_team,
        "top_scorer": top_scorer[0] if top_scorer else None,
        "top_scorer_goals": top_scorer[1] if top_scorer else 0,
        "stage": match.get("stage") or "GROUP",
        "is_knockout": any(k in (match.get("stage") or "").upper() for k in ("FINAL", "SEMI", "QUARTER", "16", "QF", "SF", "R16", "KNOCK")),
        "priority_home": is_priority(home),
        "priority_away": is_priority(away),
        "stats": match.get("stats") or {},
    }


def _scoreboard_data(f: dict) -> dict:
    return {"home": f["home"], "away": f["away"], "home_score": f["home_score"], "away_score": f["away_score"], "stage": f["stage"]}


def _timeline_data(f: dict) -> dict:
    items = []
    for e in f["events"]:
        if e.get("type") in {"goal", "penalty", "own_goal", "red_card"}:
            items.append(
                {
                    "minute": e.get("minute"),
                    "label": ("🟥 " if e["type"] == "red_card" else "⚽ ") + (e.get("player") or e.get("team") or ""),
                    "team": e.get("team"),
                    "type": e.get("type"),
                }
            )
    return {"items": items[:8], "home": f["home"], "away": f["away"]}


# --------------------------------------------------------------------------- #
# Per-variant beat builders
# --------------------------------------------------------------------------- #
def _fit_duration(beats: list[dict]) -> list[dict]:
    """Scale beat seconds so total lands inside the hard limits."""
    lo, hi = settings.hard_seconds
    target_lo, target_hi = settings.target_seconds
    total = sum(b["seconds"] for b in beats) or 1
    target = clamp(total, target_lo, target_hi)
    scale = target / total
    for b in beats:
        b["seconds"] = round(clamp(b["seconds"] * scale, 1.2, 12), 2)
    # final clamp to hard limits
    tot = sum(b["seconds"] for b in beats)
    if tot < lo:
        beats[-1]["seconds"] += lo - tot
    elif tot > hi:
        over = tot - hi
        beats[-1]["seconds"] = max(1.2, beats[-1]["seconds"] - over)
    return beats


def build_drama(f: dict) -> list[dict]:
    hero = f["comeback_team"] or f["winner"] or f["home"]
    late = f["late_goals"][-1] if f["late_goals"] else None
    beats = [
        {"id": "hook", "text": f"{hero} were minutes from disaster…", "seconds": 3.0, "visual": "title_card", "data": {"accent": hero}},
        {"id": "score", "text": f"{f['home']} {f['scoreline']} {f['away']}", "seconds": 3.0, "visual": "scoreboard", "data": _scoreboard_data(f)},
        {"id": "timeline", "text": "How it happened", "seconds": 5.0, "visual": "timeline", "data": _timeline_data(f)},
    ]
    if late:
        beats.append({"id": "late", "text": f"{late.get('player') or late.get('team')} — {late.get('minute')}'", "seconds": 4.0, "visual": "big_number", "data": {"number": f"{late.get('minute')}'", "label": f"{late.get('player') or ''} struck"}})
    beats.append({"id": "outro", "text": f"{hero} survive. Follow for every World Cup moment.", "seconds": 3.0, "visual": "outro", "data": {"accent": hero}})
    return _fit_duration(beats)


def build_controversy(f: dict) -> list[dict]:
    red = f["reds"][0] if f["reds"] else None
    trigger = (red.get("player") if red else None) or "the call"
    beats = [
        {"id": "hook", "text": "The decision everyone is still arguing about…", "seconds": 3.0, "visual": "title_card", "data": {"accent": "#ff3b30"}},
        {"id": "score", "text": f"{f['home']} {f['scoreline']} {f['away']}", "seconds": 3.0, "visual": "scoreboard", "data": _scoreboard_data(f)},
    ]
    if red:
        beats.append({"id": "red", "text": f"{trigger} sent off — {red.get('minute')}'", "seconds": 4.0, "visual": "big_number", "data": {"number": "RED", "label": f"{trigger} sent off ({red.get('minute')}')"}})
    beats.append({"id": "split", "text": "The numbers don't lie", "seconds": 5.0, "visual": "stat_split", "data": _stat_split(f)})
    beats.append({"id": "outro", "text": "Was it the right call? Comment 👇", "seconds": 3.0, "visual": "outro", "data": {"accent": "#ff3b30"}})
    return _fit_duration(beats)


def build_underdog(f: dict) -> list[dict]:
    hero = f["winner"] or f["home"]
    favourite = f["loser"] or f["away"]
    beats = [
        {"id": "hook", "text": f"Nobody believed in {hero}…", "seconds": 3.0, "visual": "title_card", "data": {"accent": hero}},
        {"id": "score", "text": f"{hero} just beat {favourite}", "seconds": 3.5, "visual": "scoreboard", "data": _scoreboard_data(f)},
        {"id": "bracket", "text": "The run nobody saw coming", "seconds": 5.0, "visual": "bracket", "data": {"team": hero, "stage": f["stage"]}},
        {"id": "outro", "text": f"{hero}. Remember the name.", "seconds": 3.0, "visual": "outro", "data": {"accent": hero}},
    ]
    return _fit_duration(beats)


def build_star(f: dict) -> list[dict]:
    star = f["top_scorer"] or "the star"
    beats = [
        {"id": "hook", "text": f"{star} just took over the World Cup…", "seconds": 3.0, "visual": "title_card", "data": {"accent": f["winner"] or f["home"]}},
        {"id": "card", "text": star, "seconds": 5.0, "visual": "player_card", "data": {"name": star, "goals": f["top_scorer_goals"], "team": f["winner"] or f["home"], "scoreline": f["scoreline"]}},
        {"id": "score", "text": f"{f['home']} {f['scoreline']} {f['away']}", "seconds": 3.0, "visual": "scoreboard", "data": _scoreboard_data(f)},
        {"id": "outro", "text": f"Is {star} the player of the tournament?", "seconds": 3.0, "visual": "outro", "data": {"accent": f["winner"] or f["home"]}},
    ]
    return _fit_duration(beats)


def build_stat_shock(f: dict) -> list[dict]:
    stat = _headline_stat(f)
    beats = [
        {"id": "hook", "text": "A number that makes no sense…", "seconds": 3.0, "visual": "title_card", "data": {"accent": f["winner"] or f["home"]}},
        {"id": "big", "text": stat["label"], "seconds": 5.0, "visual": "big_number", "data": {"number": stat["number"], "label": stat["label"]}},
        {"id": "score", "text": f"{f['home']} {f['scoreline']} {f['away']}", "seconds": 3.0, "visual": "scoreboard", "data": _scoreboard_data(f)},
        {"id": "outro", "text": "Follow for the stats nobody else shows you.", "seconds": 3.0, "visual": "outro", "data": {"accent": f["winner"] or f["home"]}},
    ]
    return _fit_duration(beats)


def _stat_split(f: dict) -> dict:
    stats = f.get("stats") or {}
    poss = stats.get("possession") or {}
    shots = stats.get("shots") or {}
    rows = []
    if poss:
        rows.append({"label": "Possession", "home": f"{poss.get(f['home'], '-')}%", "away": f"{poss.get(f['away'], '-')}%"})
    if shots:
        rows.append({"label": "Shots", "home": shots.get(f["home"], "-"), "away": shots.get(f["away"], "-")})
    rows.append({"label": "Goals", "home": f["home_score"], "away": f["away_score"]})
    return {"home": f["home"], "away": f["away"], "rows": rows}


def _headline_stat(f: dict) -> dict:
    if f["late_goals"]:
        g = f["late_goals"][-1]
        return {"number": f"{g.get('minute')}'", "label": f"Winner in the {g.get('minute')}th minute"}
    if f["margin"] >= 3:
        return {"number": f"+{f['margin']}", "label": f"A {f['scoreline']} statement"}
    if f["comeback_team"]:
        return {"number": f["scoreline"], "label": f"{f['comeback_team']} came back from behind"}
    total = f["home_score"] + f["away_score"]
    return {"number": str(total), "label": f"{total} goals in one match"}


VARIANT_BUILDERS = {
    "drama": build_drama,
    "controversy": build_controversy,
    "underdog": build_underdog,
    "star_moment": build_star,
    "star": build_star,
    "surprising_stat": build_stat_shock,
    "stat_shock": build_stat_shock,
}


def build_package(match: dict[str, Any], variant_cfg: dict[str, Any]) -> dict[str, Any]:
    """Deterministically build one story package for a variant. Never raises."""
    f = summarize(match)
    angle = variant_cfg.get("angle", "drama")
    builder = VARIANT_BUILDERS.get(angle) or VARIANT_BUILDERS.get(variant_cfg.get("id", "drama")) or build_drama
    beats = builder(f)
    title = f"{f['home']} {f['scoreline']} {f['away']} — {angle.replace('_', ' ')}"
    return {
        "match_id": match.get("id"),
        "variant": variant_cfg.get("id", angle),
        "angle": angle,
        "template": variant_cfg.get("template", "timeline_reveal"),
        "title": title,
        "narrative": " ".join(b["text"] for b in beats),
        "beats": beats,
        "facts": f,
        "source_mode": "story_engine",
        "ai_polished": False,
    }
