"""
database.py — SQLAlchemy models + access layer.

Primary store is PostgreSQL (Railway). If DATABASE_URL is missing or the server
is unreachable, it transparently falls back to a local SQLite file so the whole
pipeline still runs. Models mirror sql/schema.sql.

Import:
    from database import db, Match, Video, ...
    with db.session() as s: ...
"""
from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.types import JSON

from settings import DATA_DIR, settings
from utils import get_logger, new_id, utcnow

log = get_logger("database")


class Base(DeclarativeBase):
    pass


# Autoincrement integer PK that works on BOTH backends: SQLite only treats
# INTEGER PRIMARY KEY as a rowid alias (BIGINT stays NULL), so we downcast there.
BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class Match(Base):
    __tablename__ = "matches"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    competition: Mapped[str] = mapped_column(String, default="FIFA World Cup 2026")
    season: Mapped[str | None] = mapped_column(String, nullable=True)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    home_team: Mapped[str] = mapped_column(String)
    away_team: Mapped[str] = mapped_column(String)
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="SCHEDULED")
    utc_kickoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    events: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    rank_score: Mapped[float] = mapped_column(Float, default=0.0)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    videos: Mapped[list["Video"]] = relationship(back_populates="match", cascade="all, delete-orphan")


class Video(Base):
    __tablename__ = "videos"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("vid_"))
    match_id: Mapped[str | None] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), nullable=True)
    variant: Mapped[str] = mapped_column(String)
    angle: Mapped[str | None] = mapped_column(String, nullable=True)
    template: Mapped[str | None] = mapped_column(String, nullable=True)
    source_mode: Mapped[str] = mapped_column(String, default="story_engine")
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int] = mapped_column(Integer, default=1080)
    height: Mapped[int] = mapped_column(Integer, default=1920)
    status: Mapped[str] = mapped_column(String, default="created")
    video_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    match: Mapped["Match"] = relationship(back_populates="videos")


class Hook(Base):
    __tablename__ = "hooks"
    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), nullable=True)
    match_id: Mapped[str | None] = mapped_column(String, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    variant_rank: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String, default="claude")
    predicted_ctr: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Caption(Base):
    __tablename__ = "captions"
    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), nullable=True)
    platform: Mapped[str] = mapped_column(String)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ViralScore(Base):
    __tablename__ = "viral_scores"
    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), nullable=True)
    viral: Mapped[float | None] = mapped_column(Float, nullable=True)
    emotional: Mapped[float | None] = mapped_column(Float, nullable=True)
    story: Mapped[float | None] = mapped_column(Float, nullable=True)
    retention: Mapped[float | None] = mapped_column(Float, nullable=True)
    controversy: Mapped[float | None] = mapped_column(Float, nullable=True)
    novelty: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), nullable=True)
    platform: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued")
    remote_id: Mapped[str | None] = mapped_column(String, nullable=True)
    remote_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Performance(Base):
    __tablename__ = "performance"
    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), nullable=True)
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"), nullable=True)
    platform: Mapped[str] = mapped_column(String)
    views: Mapped[int] = mapped_column(BigInteger, default=0)
    likes: Mapped[int] = mapped_column(BigInteger, default=0)
    comments: Mapped[int] = mapped_column(BigInteger, default=0)
    shares: Mapped[int] = mapped_column(BigInteger, default=0)
    saves: Mapped[int] = mapped_column(BigInteger, default=0)
    avg_watch_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    retention_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearningData(Base):
    __tablename__ = "learning_data"
    __table_args__ = (UniqueConstraint("dimension", "key", "platform", name="uq_learning_dim_key_platform"),)
    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    dimension: Mapped[str] = mapped_column(String)
    key: Mapped[str] = mapped_column(String)
    platform: Mapped[str] = mapped_column(String, default="all")
    samples: Mapped[int] = mapped_column(Integer, default=0)
    avg_views: Mapped[float] = mapped_column(Float, default=0.0)
    avg_engagement: Mapped[float] = mapped_column(Float, default=0.0)
    avg_retention: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RightsAsset(Base):
    __tablename__ = "rights_assets"
    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    license: Mapped[str] = mapped_column(String)
    legal_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    embed_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    usable: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppState(Base):
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- #
# Engine / access layer
# --------------------------------------------------------------------------- #
class Database:
    def __init__(self) -> None:
        self.engine = None
        self._Session: sessionmaker | None = None
        self.url: str = ""
        self.backend: str = "unknown"

    @staticmethod
    def _normalise(url: str) -> str:
        # Railway sometimes hands out postgres:// — SQLAlchemy wants postgresql+psycopg2
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg2://" + url[len("postgresql://") :]
        return url

    def connect(self) -> None:
        """Connect to PostgreSQL; fall back to SQLite on any failure."""
        pg_url = self._normalise(settings.database_url) if settings.database_url else ""
        if pg_url:
            try:
                eng = create_engine(pg_url, pool_pre_ping=True, pool_recycle=1800, future=True)
                with eng.connect() as conn:
                    conn.execute(select(1))
                self.engine, self.url, self.backend = eng, pg_url, "postgresql"
                log.info("database: connected to PostgreSQL")
            except Exception as exc:
                log.warning("database: PostgreSQL unavailable (%s) — falling back to SQLite", exc)

        if self.engine is None:
            sqlite_path = DATA_DIR / "wm2026.db"
            self.url = f"sqlite:///{sqlite_path}"
            self.engine = create_engine(self.url, future=True, connect_args={"check_same_thread": False})
            self.backend = "sqlite"
            log.info("database: using SQLite at %s", sqlite_path)

        self._Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(self.engine)

    @contextlib.contextmanager
    def session(self) -> Iterator[Session]:
        if self._Session is None:
            self.connect()
        assert self._Session is not None
        s = self._Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ---- small reusable helpers ------------------------------------------
    def upsert_match(self, data: dict) -> str:
        """Insert or update a match by id. Returns match id."""
        with self.session() as s:
            row = s.get(Match, data["id"])
            if row is None:
                row = Match(id=data["id"])
                s.add(row)
            for k, v in data.items():
                if k == "id":
                    continue
                if hasattr(row, k):
                    setattr(row, k, v)
            return row.id

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            row = s.get(AppState, key)
            return row.value if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self.session() as s:
            row = s.get(AppState, key)
            if row is None:
                s.add(AppState(key=key, value=value))
            else:
                row.value = value

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        with self.session() as s:
            for model in (Match, Video, Upload, Performance):
                out[model.__tablename__] = s.scalar(select(func.count()).select_from(model)) or 0
        return out


db = Database()


def init_db() -> Database:
    """Connect (if needed) and ensure tables exist. Safe to call repeatedly."""
    if db.engine is None:
        db.connect()
    return db
