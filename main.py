"""
main.py — entrypoint + orchestrator.

Boots:
  • the status/health API (FastAPI/uvicorn) bound to $PORT (Railway needs this)
  • the background scheduler (scheduler.py) that drives the autonomous pipeline

The Pipeline class implements the full Level-3 flow for one match:
  fetch → rights → story (5 variants) → hooks → captions → score → render
        → subtitles → persist → upload(or queue) → artifacts

Everything is wrapped so a single failing match/video is logged and skipped; the
process stays up.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from analytics import analytics
from caption_generator import caption_generator
from database import Caption, Hook, Video, ViralScore, db, init_db
from hook_generator import hook_generator
from match_fetcher import fetcher
from match_ranker import ranker
from rights_discovery import rights
from settings import OUTPUT_DIR, cfg, settings
from story_engine import story_engine
from subtitle_generator import subtitle_generator
from upload_tiktok import tiktok_uploader
from upload_youtube import youtube_uploader
from utils import get_logger, iso, new_id, slugify, utcnow, write_json
from video_builder import video_builder
from viral_scorer import viral_scorer

log = get_logger("main")


class Pipeline:
    # ---- one full video ---------------------------------------------------
    def _process_package(self, match: dict[str, Any], pkg: dict[str, Any], match_dir: Path) -> dict[str, Any]:
        variant = pkg.get("variant", "v")
        out_dir = match_dir / variant
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1) create the DB row up front so we have an id for FKs
        video_id = new_id("vid_")
        with db.session() as s:
            s.add(
                Video(
                    id=video_id,
                    match_id=match["id"],
                    variant=variant,
                    angle=pkg.get("angle"),
                    template=pkg.get("template"),
                    source_mode=pkg.get("source_mode", "story_engine"),
                    title=pkg.get("title"),
                    status="created",
                )
            )

        # 2) hooks
        hooks = hook_generator.generate(pkg, n=6)
        chosen_hook = hooks[0]["text"] if hooks else pkg.get("title", "World Cup 2026")
        hook_style = hooks[0]["source"] if hooks else "template"

        # 3) captions + 4) score
        captions = caption_generator.generate(pkg, chosen_hook)
        scores = viral_scorer.score(pkg)

        # 5) render video
        render = video_builder.build(pkg, chosen_hook, out_dir)

        # 6) subtitles
        subs = subtitle_generator.generate(pkg, out_dir)

        post_hour = utcnow().hour
        meta = {
            "video_id": video_id,
            "match_id": match["id"],
            "variant": variant,
            "angle": pkg.get("angle"),
            "template": pkg.get("template"),
            "source_mode": pkg.get("source_mode"),
            "title": pkg.get("title"),
            "hook": chosen_hook,
            "hook_style": hook_style,
            "scores": scores,
            "duration_sec": render.get("duration"),
            "render_status": render.get("status"),
            "post_hour": post_hour,
            "ai_polished": pkg.get("ai_polished", False),
            "language": "en",
            "created_at": iso(),
        }

        # 7) persist children
        with db.session() as s:
            v = s.get(Video, video_id)
            if v:
                v.file_path = render.get("file_path")
                v.duration_sec = render.get("duration")
                v.status = "rendered" if render.get("status") == "rendered" else "failed"
                v.video_metadata = meta
            for h in hooks:
                s.add(Hook(video_id=video_id, match_id=match["id"], text=h["text"], variant_rank=h["rank"], source=h["source"]))
            s.add(
                Caption(
                    video_id=video_id, platform="tiktok",
                    caption=captions["tiktok"]["caption"], hashtags=captions["tiktok"]["hashtags"],
                )
            )
            s.add(
                Caption(
                    video_id=video_id, platform="youtube_shorts",
                    caption=captions["youtube_shorts"]["title"], description=captions["youtube_shorts"]["description"],
                    hashtags=captions["youtube_shorts"]["hashtags"], tags=captions["youtube_shorts"]["tags"],
                )
            )
            s.add(
                ViralScore(
                    video_id=video_id, viral=scores["viral"], emotional=scores["emotional"], story=scores["story"],
                    retention=scores["retention"], controversy=scores["controversy"], novelty=scores["novelty"],
                    rationale=scores.get("rationale"),
                )
            )

        # 8) artifact files (spec-mandated six per short)
        write_json(out_dir / "metadata.json", meta)
        write_json(out_dir / "hooks.json", {"chosen": chosen_hook, "variants": hooks})
        write_json(out_dir / "captions.json", {"platforms": captions, "subtitles": subs})
        write_json(out_dir / "hashtags.json", {
            "tiktok": captions["tiktok"]["hashtags"],
            "youtube_shorts": captions["youtube_shorts"]["hashtags"],
        })
        write_json(out_dir / "analytics.json", {"video_id": video_id, "metrics": {}, "scores": scores})

        # 9) upload (TikTok first per priority), or queue
        published = []
        if render.get("status") == "rendered" and scores.get("publishable") and render.get("file_path"):
            tk = tiktok_uploader.publish(
                video_id, render["file_path"],
                {"caption": captions["tiktok"]["caption"], "title": pkg.get("title"), "platform": "tiktok"},
            )
            yt = youtube_uploader.publish(
                video_id, render["file_path"],
                {
                    "title": captions["youtube_shorts"]["title"],
                    "description": captions["youtube_shorts"]["description"],
                    "tags": captions["youtube_shorts"]["tags"],
                    "platform": "youtube_shorts",
                },
            )
            published = [tk, yt]
        else:
            log.info("main: %s not auto-published (status=%s, publishable=%s)", variant, render.get("status"), scores.get("publishable"))

        return {"video_id": video_id, "variant": variant, "render": render.get("status"), "viral": scores["viral"], "uploads": published}

    # ---- one match --------------------------------------------------------
    def process_match(self, match: dict[str, Any]) -> dict[str, Any]:
        log.info("main: processing match %s (%s %s-%s %s)", match["id"], match.get("home_team"),
                 match.get("home_score"), match.get("away_score"), match.get("away_team"))
        match_dir = OUTPUT_DIR / slugify(match["id"])
        match_dir.mkdir(parents=True, exist_ok=True)

        decision = rights.discover(match)
        write_json(match_dir / "rights_report.json", decision)

        packages = story_engine.generate_all(match, broll_assets=decision.get("broll_assets"))
        results = []
        for pkg in packages:
            try:
                results.append(self._process_package(match, pkg, match_dir))
            except Exception as exc:  # noqa: BLE001
                log.exception("main: package %s failed (%s)", pkg.get("variant"), exc)

        ranker.mark_processed(match["id"])
        return {"match_id": match["id"], "videos": results, "source_mode": decision["mode"]}

    # ---- one scheduler cycle ---------------------------------------------
    def run_cycle(self) -> dict[str, Any]:
        log.info("main: ===== pipeline cycle start =====")
        try:
            fetcher.sync()
        except Exception as exc:
            log.warning("main: fetch failed (%s) — continuing with stored matches", exc)

        matches = ranker.select_unprocessed(limit=cfg("scheduler.max_matches_per_cycle", 6))
        out = []
        for m in matches:
            try:
                out.append(self.process_match(m))
            except Exception as exc:  # noqa: BLE001
                log.exception("main: match %s failed (%s)", m.get("id"), exc)

        # opportunistically clear any queued uploads
        try:
            tiktok_uploader.retry_queue()
            youtube_uploader.retry_queue()
        except Exception as exc:
            log.debug("main: queue retry failed (%s)", exc)

        db.set_state("last_cycle", iso())
        log.info("main: ===== cycle done: %d matches processed =====", len(out))
        return {"processed": len(out), "matches": out}


pipeline = Pipeline()


# --------------------------------------------------------------------------- #
# Status / health API
# --------------------------------------------------------------------------- #
def create_app():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="wm2026-growth-engine", version="1.0")

    @app.get("/health")
    def health():
        return {"status": "ok", "time": iso(), "db": db.backend}

    @app.get("/status")
    def status():
        return {
            "service": "wm2026-growth-engine",
            "env": settings.env,
            "db_backend": db.backend,
            "claude": settings.has_claude,
            "uploads_enabled": settings.enable_uploads and not settings.dry_run,
            "tiktok_ready": tiktok_uploader.available,
            "youtube_ready": youtube_uploader.available,
            "last_fetch": db.get_state("last_fetch"),
            "last_cycle": db.get_state("last_cycle"),
            "counts": db.counts(),
        }

    @app.get("/dashboard")
    def dashboard():
        return JSONResponse(analytics.dashboard())

    @app.post("/run")
    def run_now():
        # manual trigger; runs synchronously (fine for on-demand ops)
        return pipeline.run_cycle()

    return app


def main() -> None:
    os.makedirs("secrets", exist_ok=True)
    init_db()
    log.info("boot: db=%s | claude=%s | uploads=%s | dry_run=%s",
             db.backend, settings.has_claude, settings.enable_uploads, settings.dry_run)

    # start the autonomous scheduler in the background
    try:
        from scheduler import start_scheduler

        start_scheduler(pipeline)
    except Exception as exc:  # noqa: BLE001
        log.error("boot: scheduler failed to start (%s) — API still serving", exc)

    import uvicorn

    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
