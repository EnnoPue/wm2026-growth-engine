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
# Legal pages (self-hosted ToS + Privacy Policy for TikTok / YouTube app review)
# Override the contact address with the CONTACT_EMAIL env var if you like.
# --------------------------------------------------------------------------- #
_LEGAL_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — WMPosting</title>
<style>
  body{max-width:760px;margin:40px auto;padding:0 20px;
       font:16px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       color:#1a1a1a;background:#fff}
  h1{font-size:1.6rem} h2{font-size:1.1rem;margin-top:1.6em}
  a{color:#0a58ca} code{background:#f2f2f2;padding:1px 5px;border-radius:4px}
  .muted{color:#666;font-size:.9rem} nav{margin-bottom:24px}
  nav a{margin-right:16px}
</style></head>
<body>
<nav><a href="/">Home</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></nav>
__BODY__
</body></html>"""

_INDEX_BODY = """
<h1>WMPosting</h1>
<p>WMPosting is an automated tool that creates and publishes original short-form
videos about the FIFA World Cup 2026 — animated motion graphics, scoreboards,
match timelines, and statistics — to the social media account you connect and
authorize.</p>
<p class="muted">See our <a href="/privacy">Privacy Policy</a> and
<a href="/terms">Terms of Service</a>.</p>"""

_PRIVACY_BODY = """
<h1>Privacy Policy</h1>
<p class="muted">Last updated: __DATE__</p>
<p>WMPosting ("the App", "we", "us") is an automated content-publishing tool
that posts original short-form videos to social media accounts that you
explicitly connect and authorize. This policy explains what data we handle and
why.</p>
<h2>1. Information we collect</h2>
<ul>
<li><strong>Account authorization.</strong> When you connect a TikTok or YouTube
account through the platform's official OAuth flow, we receive and store the
access token, refresh token, and your account identifier (such as the TikTok
<code>open_id</code> or YouTube channel ID) needed to publish on your behalf.
We never receive or store your password.</li>
<li><strong>Content performance.</strong> For videos the App publishes, we
retrieve public engagement metrics (views, likes, comments, shares) from the
platform's API.</li>
</ul>
<h2>2. How we use your information</h2>
<ul>
<li>To publish original videos — animated motion graphics, statistics, and
recaps about the FIFA World Cup 2026 — to the account you connected.</li>
<li>To measure the performance of those videos so the App can improve the
content it produces.</li>
</ul>
<h2>3. Sharing</h2>
<p>We do not sell or share your data with third parties. Data is transmitted
only to the platforms you connect (TikTok, YouTube) through their official APIs.
Video captions are generated using Anthropic's API — no account credentials or
personal data are sent to that service.</p>
<h2>4. Storage and retention</h2>
<p>Tokens and metrics are stored in our private database and retained only while
your account remains connected. You may disconnect at any time and request
deletion of all stored data by contacting us.</p>
<h2>5. Contact</h2>
<p>For privacy questions or deletion requests, email
<a href="mailto:__CONTACT__">__CONTACT__</a>.</p>"""

_TERMS_BODY = """
<h1>Terms of Service</h1>
<p class="muted">Last updated: __DATE__</p>
<h2>1. The service</h2>
<p>WMPosting ("the App") is an automated tool that generates and publishes
original short-form videos to social media accounts you connect and
authorize.</p>
<h2>2. Your responsibilities</h2>
<ul>
<li>You may only connect accounts that you own or are authorized to manage.</li>
<li>You are responsible for the content published through your connected
accounts and for complying with the terms and policies of each platform
(including TikTok and YouTube).</li>
</ul>
<h2>3. Content</h2>
<p>All videos produced by the App are original works — animated graphics,
statistics, and data visualizations. The App does not use copyrighted broadcast
footage.</p>
<h2>4. Availability and warranty</h2>
<p>The App is provided "as is", without warranty of any kind. We do not
guarantee uninterrupted or error-free operation.</p>
<h2>5. Limitation of liability</h2>
<p>To the maximum extent permitted by law, we are not liable for any damages
arising from the use of, or inability to use, the App.</p>
<h2>6. Changes</h2>
<p>We may update these terms from time to time. Continued use of the App after
changes take effect constitutes acceptance of the revised terms.</p>
<h2>7. Contact</h2>
<p>Questions about these terms:
<a href="mailto:__CONTACT__">__CONTACT__</a>.</p>"""


def _page(title: str, body: str) -> str:
    from datetime import datetime, timezone

    contact = os.environ.get("CONTACT_EMAIL", "pueschel.enno07@gmail.com")
    date = datetime.now(timezone.utc).strftime("%B %d, %Y")
    return (
        _LEGAL_TEMPLATE.replace("__TITLE__", title)
        .replace("__BODY__", body)
        .replace("__CONTACT__", contact)
        .replace("__DATE__", date)
    )


# --------------------------------------------------------------------------- #
# Status / health API
# --------------------------------------------------------------------------- #
def create_app():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="wm2026-growth-engine", version="1.0")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _page("WMPosting", _INDEX_BODY)

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy():
        return _page("Privacy Policy", _PRIVACY_BODY)

    @app.get("/terms", response_class=HTMLResponse)
    def terms():
        return _page("Terms of Service", _TERMS_BODY)

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
