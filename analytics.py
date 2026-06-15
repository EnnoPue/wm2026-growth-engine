"""
analytics.py — read-only rollups for the status API and logs. No side effects.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, select

from database import Match, Performance, Upload, Video, ViralScore, db
from learning_engine import learning
from utils import get_logger

log = get_logger("analytics")


class Analytics:
    def overview(self) -> dict[str, Any]:
        with db.session() as s:
            matches = s.scalar(select(func.count()).select_from(Match)) or 0
            processed = s.scalar(select(func.count()).select_from(Match).where(Match.processed.is_(True))) or 0
            videos = s.scalar(select(func.count()).select_from(Video)) or 0
            rendered = s.scalar(select(func.count()).select_from(Video).where(Video.status == "rendered")) or 0
            published = s.scalar(select(func.count()).select_from(Upload).where(Upload.status == "published")) or 0
            queued = s.scalar(select(func.count()).select_from(Upload).where(Upload.status == "queued")) or 0
            failed = s.scalar(select(func.count()).select_from(Upload).where(Upload.status == "failed")) or 0
            total_views = s.scalar(select(func.coalesce(func.sum(Performance.views), 0))) or 0
            total_likes = s.scalar(select(func.coalesce(func.sum(Performance.likes), 0))) or 0
            total_shares = s.scalar(select(func.coalesce(func.sum(Performance.shares), 0))) or 0
        return {
            "matches": matches,
            "matches_processed": processed,
            "videos": videos,
            "videos_rendered": rendered,
            "uploads_published": published,
            "uploads_queued": queued,
            "uploads_failed": failed,
            "total_views": int(total_views),
            "total_likes": int(total_likes),
            "total_shares": int(total_shares),
        }

    def top_videos(self, n: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with db.session() as s:
            rows = s.execute(
                select(Video, ViralScore.viral)
                .join(ViralScore, ViralScore.video_id == Video.id, isouter=True)
                .order_by(desc(ViralScore.viral))
                .limit(n)
            ).all()
            for v, viral in rows:
                # latest views for this video
                views = s.scalar(
                    select(func.coalesce(func.max(Performance.views), 0)).where(Performance.video_id == v.id)
                ) or 0
                out.append(
                    {
                        "id": v.id,
                        "variant": v.variant,
                        "title": v.title,
                        "status": v.status,
                        "viral_score": round(viral, 1) if viral is not None else None,
                        "views": int(views),
                        "file_path": v.file_path,
                    }
                )
        return out

    def recent_uploads(self, n: int = 15) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with db.session() as s:
            rows = s.scalars(select(Upload).order_by(desc(Upload.id)).limit(n)).all()
            for u in rows:
                out.append(
                    {
                        "id": u.id,
                        "platform": u.platform,
                        "status": u.status,
                        "remote_url": u.remote_url,
                        "attempts": u.attempts,
                        "last_error": (u.last_error or "")[:160],
                    }
                )
        return out

    def dashboard(self) -> dict[str, Any]:
        return {
            "overview": self.overview(),
            "top_videos": self.top_videos(8),
            "recent_uploads": self.recent_uploads(10),
            "learning": learning.snapshot(),
        }


analytics = Analytics()
