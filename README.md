# wm2026-growth-engine

Fully-automated short-form content engine for the **FIFA World Cup 2026**, built to
maximise **views, followers, watch time, shares and revenue** on **TikTok** and
**YouTube Shorts** (in that priority order). Instagram is intentionally deprioritised.

It runs at **Automation Level 3** — no human in the loop:

```
match occurs → fetch data → rank → build 5 videos → hooks → captions → metadata
            → score → auto-upload (or queue) → track performance → learn → repeat
```

---

## ⚖️ Legal stance (read this first)

**This system never assumes access to copyrighted FIFA broadcasts and never
downloads, rips, restreams, or re-encodes broadcast match footage.** That is the
non-negotiable design constraint.

On every run it executes a two-step content-sourcing strategy:

1. **Rights discovery** (`rights_discovery.py`) — probes a small allow-list of
   genuinely legal/free sources (YouTube clips explicitly licensed **Creative
   Commons**, publisher-permitted **oEmbed** embeds, **Wikimedia Commons**
   public-domain/CC b-roll, **Pexels/Pixabay**-licensed generic football b-roll).
   For each candidate it records *why it is legal, what the limitations are, and
   how it's accessed*. Nothing without an explicit free licence is ever used.
2. **Story Engine Mode** (`story_engine.py` + `fallback_content_engine.py`) — if no
   practical legal footage exists (the realistic default for live WC matches), the
   system **automatically** generates original videos from **data**: scores, events,
   player stats, standings, bracket progression — rendered as motion graphics,
   scoreboards, player cards, timelines, charts and animated text. 100% original,
   100% clearable.

The fallback is automatic and silent — you are never asked.

> You are responsible for your own platform-policy and music-licensing compliance
> in your jurisdiction. Use rights-cleared music in `assets/music/` only.

---

## 🧱 Architecture

| Layer | Modules |
|---|---|
| **Ingest** | `match_fetcher.py` (API-Football / football-data.org / SportMonks adapters + local fallback) |
| **Select** | `match_ranker.py` (team-priority + drama weighting), `learning_engine.py` |
| **Source** | `rights_discovery.py` → `story_engine.py` (auto-fallback) |
| **Generate** | `hook_generator.py`, `caption_generator.py`, `subtitle_generator.py`, `fallback_content_engine.py` |
| **Render** | `video_builder.py` (Pillow scenes + FFmpeg assembly, 1080×1920) |
| **Judge** | `viral_scorer.py` (viral / emotional / story / retention / controversy 0–100) |
| **Publish** | `upload_tiktok.py`, `upload_youtube.py` (queue on failure) |
| **Measure** | `performance_tracker.py`, `analytics.py` |
| **Learn** | `learning_engine.py` (PostgreSQL-backed ranking that feeds back into selection) |
| **Run** | `main.py` (status API + orchestrator), `scheduler.py` (APScheduler) |

**AI:** Claude is primary (`ai_client.py`). If `ANTHROPIC_API_KEY` is unset or the
API errors, every generator falls back to deterministic templates — the pipeline
never stops.

**Data store:** PostgreSQL (Railway). If `DATABASE_URL` is missing/unreachable it
falls back to a local SQLite file. Schema in [`sql/schema.sql`](sql/schema.sql).

---

## 🎬 Output

For every match the engine produces **5 distinct cuts** (drama / controversy /
underdog / star / stat-shock), each **20–45s** (hard limits 15–60s), **1080×1920
MP4**. Each lands in `output/<match_id>/<variant>/` with:

```
video.mp4   metadata.json   hooks.json   captions.json   hashtags.json   analytics.json
```

---

## 🚀 Deploy to Railway

1. Push this repo to GitHub.
2. **Railway → New Project → Deploy from repo.** It auto-detects the `Dockerfile`.
3. Add the **PostgreSQL** plugin → `DATABASE_URL` is injected automatically.
4. Add env vars from [`.env.example`](.env.example) (all optional — start with none
   and it runs in full Story-Engine + queue mode).
5. Deploy. The container binds `$PORT` for the status API and runs the scheduler in
   the background. Health: `GET /health`, status: `GET /status`.

### Local

```bash
cp .env.example .env            # fill in what you have (nothing is required)
docker compose up --build       # app + postgres
# or, bare metal:
pip install -r requirements.txt
python main.py                  # status API on :8080 + scheduler
python -m scripts.run_once      # one full pipeline pass on demand
```

---

## 🔌 Configuration

- **Secrets / keys** → `.env` (see `.env.example`, every value optional).
- **Tunables** (scoring weights, team priority, hooks, hashtags, video style,
  rights sources) → [`config.yaml`](config.yaml).

### Football data keys (free tiers)
- **football-data.org** — easiest free key, used as the default adapter.
- **API-Football** — free tier via RapidAPI or direct.
- **SportMonks** — free tier available.

The first provider with a key wins; with none configured, the engine uses bundled
sample/local fixtures so you can see the whole pipeline run end-to-end offline.

### Upload credentials
- **YouTube Shorts** — OAuth client + refresh token (Data API v3).
- **TikTok** — approved Content Posting API app (client key/secret + access token).

Missing either just means those uploads are **queued** in the `uploads` table and
retried automatically once credentials appear.

---

## 🧠 Learning loop

`performance_tracker.py` re-pulls views/likes/comments/shares/retention on a timer
into `performance`. `learning_engine.py` aggregates that into `learning_data`,
scoring which **angles, templates, hook styles, teams and post-hours** actually
perform — then `match_ranker.py` and the generators consult those learned scores
(with an ε-greedy exploration rate so it keeps testing new ideas).

---

## 🛟 Robustness guarantees

The pipeline is designed to **never hard-fail**:

- missing Claude key → template generators
- missing football API → next adapter → local fixtures
- missing upload creds → queued, retried later
- missing system `ffmpeg` → bundled `imageio-ffmpeg` binary
- missing PostgreSQL → SQLite
- any single match/video error → logged, skipped, pipeline continues

---

## 📂 Layout

```
wm2026-growth-engine/
├── main.py  scheduler.py  settings.py  database.py  utils.py  ai_client.py
├── match_fetcher.py  match_ranker.py  rights_discovery.py
├── story_engine.py  hook_generator.py  caption_generator.py
├── viral_scorer.py  subtitle_generator.py  video_builder.py
├── fallback_content_engine.py  learning_engine.py  analytics.py
├── upload_tiktok.py  upload_youtube.py  performance_tracker.py
├── config.yaml  requirements.txt  Dockerfile  docker-compose.yml  .env.example
├── sql/schema.sql
├── scripts/run_once.py
├── assets/ (fonts, music, backgrounds, logos)
├── templates/ data/ logs/ output/
```

Not legal advice. Ship responsibly.
