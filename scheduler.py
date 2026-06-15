"""
scheduler.py — the autonomous heartbeat (APScheduler).

Jobs:
  • content_cycle       : fetch → rank → build → upload, every POLL_INTERVAL_MINUTES
                          (and once ~10s after boot)
  • performance_refresh : pull metrics + retrain the learner, every N hours
  • queue_retry         : flush queued uploads, every 10 minutes

Each job is wrapped so an exception is logged but never kills the scheduler.
Call start_scheduler(pipeline) once from main.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from learning_engine import learning
from performance_tracker import performance_tracker
from settings import cfg, settings
from upload_tiktok import tiktok_uploader
from upload_youtube import youtube_uploader
from utils import get_logger

log = get_logger("scheduler")

_scheduler: BackgroundScheduler | None = None


def _safe(fn, name: str):
    def wrapper():
        try:
            log.info("scheduler: job '%s' start", name)
            fn()
            log.info("scheduler: job '%s' done", name)
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduler: job '%s' failed (%s)", name, exc)

    return wrapper


def start_scheduler(pipeline: Any) -> BackgroundScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler

    sched = BackgroundScheduler(timezone=settings.tz, job_defaults={"coalesce": True, "max_instances": 1})

    poll = int(cfg("scheduler.poll_interval_minutes", settings.poll_interval_minutes))
    perf_hours = int(cfg("scheduler.performance_refresh_hours", 6))

    # main content pipeline
    sched.add_job(
        _safe(pipeline.run_cycle, "content_cycle"),
        "interval",
        minutes=poll,
        next_run_time=datetime.now() + timedelta(seconds=10),
        id="content_cycle",
    )

    # performance + learning
    def _refresh():
        performance_tracker.refresh()
        learning.update()

    sched.add_job(_safe(_refresh, "performance_refresh"), "interval", hours=perf_hours,
                  next_run_time=datetime.now() + timedelta(minutes=2), id="performance_refresh")

    # upload queue retry
    def _retry():
        tiktok_uploader.retry_queue()
        youtube_uploader.retry_queue()

    sched.add_job(_safe(_retry, "queue_retry"), "interval", minutes=10, id="queue_retry")

    sched.start()
    _scheduler = sched
    log.info("scheduler: started (content every %dm, perf every %dh)", poll, perf_hours)
    return sched


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
