"""
scripts/run_once.py — run exactly one full pipeline cycle and exit.

Usage:
    python -m scripts.run_once
    python -m scripts.run_once --match local:demo-003     # force a single match

Great for local testing and for a Railway one-off / cron job that you'd rather
drive externally than via the in-process scheduler.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# allow running as a plain script (python scripts/run_once.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import init_db  # noqa: E402
from main import pipeline  # noqa: E402
from match_fetcher import fetcher  # noqa: E402
from utils import get_logger  # noqa: E402

log = get_logger("run_once")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one wm2026-growth-engine cycle")
    ap.add_argument("--match", help="process a single match id instead of the ranked queue")
    args = ap.parse_args()

    init_db()

    if args.match:
        fetcher.sync()
        from database import Match, db

        with db.session() as s:
            m = s.get(Match, args.match)
            if not m:
                log.error("match %s not found (run a sync first)", args.match)
                return 2
            match = {
                "id": m.id, "home_team": m.home_team, "away_team": m.away_team,
                "home_score": m.home_score, "away_score": m.away_score, "status": m.status,
                "stage": m.stage, "events": m.events or [], "stats": m.stats or {},
            }
        result = pipeline.process_match(match)
    else:
        result = pipeline.run_cycle()

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
