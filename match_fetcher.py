"""
match_fetcher.py — pluggable football data ingestion.

Adapters (tried in order of available API key):
  1. API-Football        (v3.football.api-sports.io)
  2. football-data.org   (api.football-data.org/v4)
  3. SportMonks          (api.sportmonks.com/v3)
Then: LOCAL fallback (data/sample_matches.json or a built-in fixture set) so the
entire pipeline runs end-to-end offline with zero keys.

Every adapter normalises to one shape (see `_blank_match`). Nothing raises into
the caller — a failing provider is logged and skipped.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from database import db
from settings import DATA_DIR, settings
from utils import get_logger, iso

log = get_logger("match_fetcher")

COMPETITION = "FIFA World Cup 2026"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _blank_match(provider: str, pid: str) -> dict[str, Any]:
    return {
        "id": f"{provider}:{pid}",
        "provider": provider,
        "competition": COMPETITION,
        "season": settings.season,
        "stage": None,
        "home_team": "",
        "away_team": "",
        "home_score": None,
        "away_score": None,
        "status": "SCHEDULED",
        "utc_kickoff": None,
        "events": [],
        "stats": {},
        "raw": {},
    }


def _norm_status(s: str | None) -> str:
    s = (s or "").upper()
    if s in {"FINISHED", "FT", "AET", "PEN", "MATCH_FINISHED"}:
        return "FINISHED"
    if s in {"IN_PLAY", "LIVE", "PAUSED", "1H", "2H", "HT", "ET"}:
        return "LIVE"
    return "SCHEDULED"


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #
class BaseAdapter:
    name = "base"

    def enabled(self) -> bool:
        return False

    def fetch(self) -> list[dict[str, Any]]:
        raise NotImplementedError


class ApiFootballAdapter(BaseAdapter):
    name = "api_football"

    def enabled(self) -> bool:
        return bool(settings.api_football_key)

    def fetch(self) -> list[dict[str, Any]]:
        base = f"https://{settings.api_football_host}"
        headers = {"x-apisports-key": settings.api_football_key}
        params = {"league": settings.api_football_league_id, "season": settings.season}
        out: list[dict[str, Any]] = []
        with httpx.Client(timeout=25, headers=headers) as c:
            r = c.get(f"{base}/fixtures", params=params)
            r.raise_for_status()
            fixtures = r.json().get("response", [])
            for fx in fixtures:
                fid = str(fx.get("fixture", {}).get("id"))
                m = _blank_match(self.name, fid)
                m["home_team"] = fx.get("teams", {}).get("home", {}).get("name", "")
                m["away_team"] = fx.get("teams", {}).get("away", {}).get("name", "")
                m["home_score"] = fx.get("goals", {}).get("home")
                m["away_score"] = fx.get("goals", {}).get("away")
                m["status"] = _norm_status(fx.get("fixture", {}).get("status", {}).get("short"))
                m["stage"] = fx.get("league", {}).get("round")
                m["utc_kickoff"] = _parse_dt(fx.get("fixture", {}).get("date"))
                m["raw"] = fx
                # Pull events only for finished matches (saves quota)
                if m["status"] == "FINISHED":
                    try:
                        er = c.get(f"{base}/fixtures/events", params={"fixture": fid})
                        m["events"] = self._events(er.json().get("response", []))
                    except Exception as exc:
                        log.debug("api_football events failed for %s: %s", fid, exc)
                out.append(m)
        return out

    @staticmethod
    def _events(raw_events: list[dict]) -> list[dict]:
        evs = []
        for e in raw_events:
            etype = (e.get("type") or "").lower()
            detail = (e.get("detail") or "").lower()
            kind = "other"
            if etype == "goal":
                kind = "own_goal" if "own" in detail else ("penalty" if "penalty" in detail else "goal")
            elif etype == "card":
                kind = "red_card" if "red" in detail else "yellow_card"
            elif etype == "subst":
                kind = "sub"
            elif etype == "var":
                kind = "var"
            evs.append(
                {
                    "minute": (e.get("time", {}) or {}).get("elapsed") or 0,
                    "type": kind,
                    "team": (e.get("team", {}) or {}).get("name", ""),
                    "player": (e.get("player", {}) or {}).get("name", ""),
                    "detail": e.get("detail", ""),
                }
            )
        return evs


class FootballDataOrgAdapter(BaseAdapter):
    name = "football_data"

    def enabled(self) -> bool:
        return bool(settings.football_data_key)

    def fetch(self) -> list[dict[str, Any]]:
        comp = settings.football_data_competition
        url = f"https://api.football-data.org/v4/competitions/{comp}/matches"
        headers = {"X-Auth-Token": settings.football_data_key}
        out: list[dict[str, Any]] = []
        with httpx.Client(timeout=25, headers=headers) as c:
            r = c.get(url, params={"season": settings.season})
            r.raise_for_status()
            for fx in r.json().get("matches", []):
                m = _blank_match(self.name, str(fx.get("id")))
                m["home_team"] = (fx.get("homeTeam") or {}).get("name", "")
                m["away_team"] = (fx.get("awayTeam") or {}).get("name", "")
                ft = (fx.get("score") or {}).get("fullTime") or {}
                m["home_score"] = ft.get("home")
                m["away_score"] = ft.get("away")
                m["status"] = _norm_status(fx.get("status"))
                m["stage"] = fx.get("stage")
                m["utc_kickoff"] = _parse_dt(fx.get("utcDate"))
                m["raw"] = fx
                # football-data free tier exposes goal scorers on some plans:
                for g in fx.get("goals", []) or []:
                    m["events"].append(
                        {
                            "minute": (g.get("minute") or 0),
                            "type": "goal",
                            "team": (g.get("team") or {}).get("name", ""),
                            "player": (g.get("scorer") or {}).get("name", ""),
                            "detail": g.get("type", ""),
                        }
                    )
                out.append(m)
        return out


class SportMonksAdapter(BaseAdapter):
    name = "sportmonks"

    def enabled(self) -> bool:
        return bool(settings.sportmonks_key)

    def fetch(self) -> list[dict[str, Any]]:
        url = "https://api.sportmonks.com/v3/football/fixtures"
        params = {
            "api_token": settings.sportmonks_key,
            "include": "participants;scores;events;stage",
            "filters": f"fixtureSeasons:{settings.season}",
        }
        out: list[dict[str, Any]] = []
        with httpx.Client(timeout=25) as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            for fx in r.json().get("data", []):
                m = _blank_match(self.name, str(fx.get("id")))
                parts = fx.get("participants", []) or []
                for p in parts:
                    loc = (p.get("meta") or {}).get("location")
                    if loc == "home":
                        m["home_team"] = p.get("name", "")
                    elif loc == "away":
                        m["away_team"] = p.get("name", "")
                for sc in fx.get("scores", []) or []:
                    if sc.get("description") in ("CURRENT", "FT"):
                        goals = sc.get("score", {})
                        if goals.get("participant") == "home":
                            m["home_score"] = goals.get("goals")
                        elif goals.get("participant") == "away":
                            m["away_score"] = goals.get("goals")
                m["status"] = _norm_status((fx.get("state") or {}).get("state") or fx.get("status"))
                m["stage"] = (fx.get("stage") or {}).get("name") if isinstance(fx.get("stage"), dict) else None
                m["utc_kickoff"] = _parse_dt(fx.get("starting_at"))
                m["raw"] = fx
                out.append(m)
        return out


class LocalAdapter(BaseAdapter):
    """Always-available offline fallback. Reads data/sample_matches.json, else
    a built-in fixture set so the pipeline demonstrably runs with zero config."""

    name = "local"

    def enabled(self) -> bool:
        return True

    def fetch(self) -> list[dict[str, Any]]:
        path = DATA_DIR / "sample_matches.json"
        raw = None
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("local: bad sample_matches.json (%s) — using built-in", exc)
        if not raw:
            raw = _BUILTIN_FIXTURES
        out = []
        for fx in raw:
            m = _blank_match(self.name, str(fx.get("id")))
            m.update({k: v for k, v in fx.items() if k in m})
            m["id"] = f"local:{fx.get('id')}"
            m["status"] = _norm_status(fx.get("status", "FINISHED"))
            m["utc_kickoff"] = _parse_dt(fx.get("utc_kickoff")) or datetime.now(timezone.utc)
            out.append(m)
        return out


# Built-in dramatic fixtures (fictional but structurally complete) for offline demo.
_BUILTIN_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "demo-001",
        "stage": "GROUP",
        "home_team": "Germany",
        "away_team": "Mexico",
        "home_score": 2,
        "away_score": 1,
        "status": "FINISHED",
        "utc_kickoff": "2026-06-14T19:00:00+00:00",
        "events": [
            {"minute": 23, "type": "goal", "team": "Mexico", "player": "S. Giménez", "detail": ""},
            {"minute": 71, "type": "goal", "team": "Germany", "player": "F. Wirtz", "detail": ""},
            {"minute": 90, "type": "goal", "team": "Germany", "player": "K. Havertz", "detail": "90+4"},
        ],
        "stats": {"possession": {"Germany": 61, "Mexico": 39}, "shots": {"Germany": 18, "Mexico": 7}},
    },
    {
        "id": "demo-002",
        "stage": "GROUP",
        "home_team": "USA",
        "away_team": "England",
        "home_score": 1,
        "away_score": 1,
        "status": "FINISHED",
        "utc_kickoff": "2026-06-15T23:00:00+00:00",
        "events": [
            {"minute": 38, "type": "goal", "team": "England", "player": "J. Bellingham", "detail": ""},
            {"minute": 84, "type": "goal", "team": "USA", "player": "C. Pulisic", "detail": ""},
            {"minute": 66, "type": "red_card", "team": "England", "player": "D. Rice", "detail": ""},
        ],
        "stats": {"possession": {"USA": 44, "England": 56}, "shots": {"USA": 9, "England": 14}},
    },
    {
        "id": "demo-003",
        "stage": "R16",
        "home_team": "Argentina",
        "away_team": "Brazil",
        "home_score": 3,
        "away_score": 2,
        "status": "FINISHED",
        "utc_kickoff": "2026-07-01T23:00:00+00:00",
        "events": [
            {"minute": 12, "type": "goal", "team": "Brazil", "player": "Vinícius Jr", "detail": ""},
            {"minute": 34, "type": "goal", "team": "Argentina", "player": "L. Messi", "detail": "penalty"},
            {"minute": 55, "type": "goal", "team": "Argentina", "player": "J. Álvarez", "detail": ""},
            {"minute": 77, "type": "goal", "team": "Brazil", "player": "Rodrygo", "detail": ""},
            {"minute": 90, "type": "goal", "team": "Argentina", "player": "L. Messi", "detail": "90+6"},
        ],
        "stats": {"possession": {"Argentina": 52, "Brazil": 48}, "shots": {"Argentina": 15, "Brazil": 13}},
    },
]


ADAPTERS: list[type[BaseAdapter]] = [
    ApiFootballAdapter,
    FootballDataOrgAdapter,
    SportMonksAdapter,
]


class MatchFetcher:
    def fetch(self) -> list[dict[str, Any]]:
        """Return normalised matches from the first working remote adapter,
        else the local fallback."""
        for cls in ADAPTERS:
            adapter = cls()
            if not adapter.enabled():
                continue
            try:
                data = adapter.fetch()
                if data:
                    log.info("match_fetcher: %d matches from %s", len(data), adapter.name)
                    return data
                log.info("match_fetcher: %s returned no matches", adapter.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("match_fetcher: %s failed (%s)", adapter.name, exc)
        local = LocalAdapter().fetch()
        log.info("match_fetcher: using LOCAL fallback (%d matches)", len(local))
        return local

    def sync(self) -> list[dict[str, Any]]:
        """Fetch and upsert into the DB. Returns the matches."""
        matches = self.fetch()
        for m in matches:
            payload = dict(m)
            if isinstance(payload.get("utc_kickoff"), datetime):
                pass  # SQLAlchemy handles datetime
            try:
                db.upsert_match(payload)
            except Exception as exc:
                log.warning("match_fetcher: upsert failed for %s (%s)", m.get("id"), exc)
        db.set_state("last_fetch", iso())
        return matches


fetcher = MatchFetcher()
