-- ===========================================================================
-- wm2026-growth-engine — PostgreSQL schema
-- Loaded automatically by docker-compose (db init) and idempotently ensured by
-- database.py at startup. SQLite fallback uses the equivalent SQLAlchemy models.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS matches (
    id              TEXT PRIMARY KEY,              -- "<provider>:<provider_match_id>"
    provider        TEXT NOT NULL,
    competition     TEXT NOT NULL DEFAULT 'FIFA World Cup 2026',
    season          TEXT,
    stage           TEXT,                          -- GROUP, R16, QF, SF, FINAL...
    home_team       TEXT NOT NULL,
    away_team       TEXT NOT NULL,
    home_score      INTEGER,
    away_score      INTEGER,
    status          TEXT NOT NULL DEFAULT 'SCHEDULED', -- SCHEDULED|LIVE|FINISHED
    utc_kickoff     TIMESTAMPTZ,
    events          JSONB DEFAULT '[]'::jsonb,      -- goals, cards, subs, VAR...
    stats           JSONB DEFAULT '{}'::jsonb,      -- possession, shots, xG...
    raw             JSONB DEFAULT '{}'::jsonb,      -- original provider payload
    rank_score      DOUBLE PRECISION DEFAULT 0,
    processed       BOOLEAN DEFAULT FALSE,          -- have we generated videos yet?
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_matches_status      ON matches (status);
CREATE INDEX IF NOT EXISTS idx_matches_processed   ON matches (processed);
CREATE INDEX IF NOT EXISTS idx_matches_rank        ON matches (rank_score DESC);

-- One row per generated short (5 per match by default).
CREATE TABLE IF NOT EXISTS videos (
    id              TEXT PRIMARY KEY,              -- uuid
    match_id        TEXT REFERENCES matches(id) ON DELETE CASCADE,
    variant         TEXT NOT NULL,                 -- drama|controversy|underdog|star|stat_shock
    angle           TEXT,
    template        TEXT,
    source_mode     TEXT NOT NULL DEFAULT 'story_engine', -- story_engine|licensed_footage
    title           TEXT,
    file_path       TEXT,                          -- output/<match>/<variant>/video.mp4
    duration_sec    DOUBLE PRECISION,
    width           INTEGER DEFAULT 1080,
    height          INTEGER DEFAULT 1920,
    status          TEXT NOT NULL DEFAULT 'created', -- created|rendered|failed|published|queued
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_videos_match   ON videos (match_id);
CREATE INDEX IF NOT EXISTS idx_videos_status  ON videos (status);

CREATE TABLE IF NOT EXISTS hooks (
    id              BIGSERIAL PRIMARY KEY,
    video_id        TEXT REFERENCES videos(id) ON DELETE CASCADE,
    match_id        TEXT,
    text            TEXT NOT NULL,
    variant_rank    INTEGER DEFAULT 0,             -- 0 = chosen hook, 1+ = alternates
    source          TEXT DEFAULT 'claude',         -- claude|template
    predicted_ctr   DOUBLE PRECISION,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_hooks_video ON hooks (video_id);

CREATE TABLE IF NOT EXISTS captions (
    id              BIGSERIAL PRIMARY KEY,
    video_id        TEXT REFERENCES videos(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,                 -- tiktok|youtube_shorts
    caption         TEXT,
    description     TEXT,                          -- youtube long description
    hashtags        JSONB DEFAULT '[]'::jsonb,
    tags            JSONB DEFAULT '[]'::jsonb,     -- youtube tags
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_captions_video ON captions (video_id);

CREATE TABLE IF NOT EXISTS viral_scores (
    id              BIGSERIAL PRIMARY KEY,
    video_id        TEXT REFERENCES videos(id) ON DELETE CASCADE,
    viral           DOUBLE PRECISION,             -- 0..100 headline score
    emotional       DOUBLE PRECISION,
    story           DOUBLE PRECISION,
    retention       DOUBLE PRECISION,
    controversy     DOUBLE PRECISION,
    novelty         DOUBLE PRECISION,
    rationale       TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scores_video ON viral_scores (video_id);
CREATE INDEX IF NOT EXISTS idx_scores_viral ON viral_scores (viral DESC);

-- Upload attempts + queue. status=queued rows are retried by the scheduler.
CREATE TABLE IF NOT EXISTS uploads (
    id              BIGSERIAL PRIMARY KEY,
    video_id        TEXT REFERENCES videos(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,                 -- tiktok|youtube_shorts
    status          TEXT NOT NULL DEFAULT 'queued', -- queued|uploading|published|failed
    remote_id       TEXT,                          -- platform video id once published
    remote_url      TEXT,
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    payload         JSONB DEFAULT '{}'::jsonb,
    scheduled_for   TIMESTAMPTZ DEFAULT now(),
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_uploads_status   ON uploads (status);
CREATE INDEX IF NOT EXISTS idx_uploads_platform ON uploads (platform);

-- Time-series of performance pulled back from the platforms (learning signal).
CREATE TABLE IF NOT EXISTS performance (
    id              BIGSERIAL PRIMARY KEY,
    video_id        TEXT REFERENCES videos(id) ON DELETE CASCADE,
    upload_id       BIGINT REFERENCES uploads(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,
    views           BIGINT DEFAULT 0,
    likes           BIGINT DEFAULT 0,
    comments        BIGINT DEFAULT 0,
    shares          BIGINT DEFAULT 0,
    saves           BIGINT DEFAULT 0,
    avg_watch_sec   DOUBLE PRECISION,
    retention_pct   DOUBLE PRECISION,
    sampled_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_perf_video    ON performance (video_id);
CREATE INDEX IF NOT EXISTS idx_perf_sampled  ON performance (sampled_at DESC);

-- Aggregated, learned signal per (angle, template, hook-style, team-tier...).
-- learning_engine.py reads/writes this; match_ranker + generators consult it.
CREATE TABLE IF NOT EXISTS learning_data (
    id              BIGSERIAL PRIMARY KEY,
    dimension       TEXT NOT NULL,                 -- 'angle'|'template'|'hook_style'|'team_tier'|'post_hour'
    key             TEXT NOT NULL,                 -- e.g. 'drama' / 'player_card' / 'Germany'
    platform        TEXT NOT NULL DEFAULT 'all',
    samples         INTEGER DEFAULT 0,
    avg_views       DOUBLE PRECISION DEFAULT 0,
    avg_engagement  DOUBLE PRECISION DEFAULT 0,    -- (likes+comments+shares)/views
    avg_retention   DOUBLE PRECISION DEFAULT 0,
    score           DOUBLE PRECISION DEFAULT 0,    -- learned desirability 0..1
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (dimension, key, platform)
);
CREATE INDEX IF NOT EXISTS idx_learning_dim ON learning_data (dimension, score DESC);

-- Catalogue of legally-usable media discovered by rights_discovery.py.
CREATE TABLE IF NOT EXISTS rights_assets (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,                 -- youtube_cc|wikimedia|pexels...
    kind            TEXT,                          -- broll|clip|image|embed
    license         TEXT NOT NULL,
    legal_basis     TEXT,                          -- human-readable why-it's-legal
    url             TEXT,
    embed_url       TEXT,
    local_path      TEXT,
    attribution     TEXT,
    keywords        JSONB DEFAULT '[]'::jsonb,
    usable          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rights_source ON rights_assets (source);

-- Lightweight key/value for runtime state (cursors, last poll, learned weights).
CREATE TABLE IF NOT EXISTS app_state (
    key             TEXT PRIMARY KEY,
    value           JSONB,
    updated_at      TIMESTAMPTZ DEFAULT now()
);
