"""
performance_tracker.py — pull views/likes/comments/shares back from the platforms
into the `performance` table. This is the raw signal the learning engine learns
from.

Real metrics when credentials exist:
  • YouTube : Data API videos.list(part=statistics)
  • TikTok  : /v2/video/query/ (view/like/comment/share counts)

When credentials are absent and ENV != production, it SIMULATES plausible metrics
correlated with each video's viral score, so the full learn loop is demonstrable
offline. Simulation never runs in production.
"""
from __future__ import annotations

import random
from typing import Any

import httpx
from sqlalchemy import select

from database import Performance, Upload, ViralScore, db
from settings import settings
from utils import get_logger

log = get_logger("performance_tracker")


class PerformanceTracker:
    def refresh(self, limit: int = 100) -> int:
        # Production tracks real published uploads only. In dev/demo we also sample
        # queued uploads so the learning loop is exercisable without credentials.
        statuses = ["published"] if settings.env == "production" else ["published", "queued"]
        with db.session() as s:
            rows = s.scalars(
                select(Upload).where(Upload.status.in_(statuses)).limit(limit)
            ).all()
            jobs = [(r.id, r.video_id, r.platform, r.remote_id) for r in rows]
        n = 0
        for upload_id, video_id, platform, remote_id in jobs:
            metrics = self._fetch(platform, remote_id, video_id)
            if not metrics:
                continue
            with db.session() as s:
                s.add(
                    Performance(
                        video_id=video_id,
                        upload_id=upload_id,
                        platform=platform,
                        views=metrics.get("views", 0),
                        likes=metrics.get("likes", 0),
                        comments=metrics.get("comments", 0),
                        shares=metrics.get("shares", 0),
                        saves=metrics.get("saves", 0),
                        avg_watch_sec=metrics.get("avg_watch_sec"),
                        retention_pct=metrics.get("retention_pct"),
                    )
                )
            n += 1
        log.info("performance_tracker: sampled %d uploads", n)
        return n

    # ---- per-platform fetch ----------------------------------------------
    def _fetch(self, platform: str, remote_id: str | None, video_id: str | None) -> dict[str, Any] | None:
        try:
            if platform == "youtube_shorts" and remote_id:
                m = self._youtube(remote_id)
                if m:
                    return m
            if platform == "tiktok" and remote_id:
                m = self._tiktok(remote_id)
                if m:
                    return m
        except Exception as exc:
            log.debug("performance_tracker: %s fetch failed (%s)", platform, exc)
        return self._simulate(video_id)

    def _youtube(self, remote_id: str) -> dict[str, Any] | None:
        from upload_youtube import youtube_uploader

        if not youtube_uploader.available:
            return None
        yt = youtube_uploader._service()
        resp = yt.videos().list(part="statistics", id=remote_id).execute()
        items = resp.get("items", [])
        if not items:
            return None
        st = items[0].get("statistics", {})
        return {
            "views": int(st.get("viewCount", 0)),
            "likes": int(st.get("likeCount", 0)),
            "comments": int(st.get("commentCount", 0)),
            "shares": 0,
        }

    def _tiktok(self, remote_id: str) -> dict[str, Any] | None:
        if not settings.tiktok_access_token:
            return None
        url = "https://open.tiktokapis.com/v2/video/query/"
        headers = {"Authorization": f"Bearer {settings.tiktok_access_token}", "Content-Type": "application/json"}
        body = {"filters": {"video_ids": [remote_id]}}
        params = {"fields": "id,view_count,like_count,comment_count,share_count"}
        with httpx.Client(timeout=30) as c:
            r = c.post(url, headers=headers, params=params, json=body)
            r.raise_for_status()
            videos = r.json().get("data", {}).get("videos", [])
        if not videos:
            return None
        v = videos[0]
        return {
            "views": int(v.get("view_count", 0)),
            "likes": int(v.get("like_count", 0)),
            "comments": int(v.get("comment_count", 0)),
            "shares": int(v.get("share_count", 0)),
        }

    def _simulate(self, video_id: str | None) -> dict[str, Any] | None:
        """Demo-only synthetic metrics correlated with the viral score."""
        if settings.env == "production" or not video_id:
            return None
        with db.session() as s:
            vs = s.scalars(
                select(ViralScore).where(ViralScore.video_id == video_id).order_by(ViralScore.id.desc())
            ).first()
        viral = (vs.viral if vs else 55) / 100.0
        base = int((2000 + random.random() * 60000) * (0.4 + viral))
        views = max(50, int(base * (0.6 + viral)))
        retention = round(35 + viral * 45 + random.uniform(-6, 6), 1)
        return {
            "views": views,
            "likes": int(views * (0.04 + viral * 0.08)),
            "comments": int(views * (0.004 + viral * 0.01)),
            "shares": int(views * (0.006 + viral * 0.02)),
            "saves": int(views * (0.005 + viral * 0.01)),
            "avg_watch_sec": round(8 + viral * 18, 1),
            "retention_pct": min(retention, 98.0),
        }


performance_tracker = PerformanceTracker()
