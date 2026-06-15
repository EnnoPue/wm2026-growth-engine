"""
upload_tiktok.py — TikTok publishing via the official Content Posting API.

There is no pip SDK; we call the REST endpoints directly over httpx:
  1. /v2/post/publish/video/init/   -> publish_id + upload_url
  2. PUT bytes to upload_url (single chunk for typical <64MB Shorts)
  3. /v2/post/publish/status/fetch/ -> poll until published

Requires an approved developer app + a valid user access token (TIKTOK_ACCESS_TOKEN).
While your app is in sandbox/unaudited, TikTok only permits PRIVACY_LEVEL=SELF_ONLY
and posts land in the user's drafts — set TIKTOK_PRIVACY_LEVEL accordingly.

Missing/invalid credentials -> the upload is QUEUED and retried; never crashes.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

import tiktok_oauth
from database import Upload, Video, db
from settings import settings
from utils import get_logger, truncate, utcnow

log = get_logger("upload_tiktok")

PLATFORM = "tiktok"
API = "https://open.tiktokapis.com/v2"


class TikTokUploader:
    @property
    def available(self) -> bool:
        return tiktok_oauth.connected()

    def _headers(self) -> dict[str, str]:
        token = tiktok_oauth.valid_access_token() or ""
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    def do_upload(self, file_path: str, meta: dict[str, Any]) -> dict[str, Any]:
        size = os.path.getsize(file_path)
        title = truncate(meta.get("caption") or meta.get("title") or "World Cup 2026", 2200)

        init_body = {
            "post_info": {
                "title": title,
                "privacy_level": settings.tiktok_privacy_level,
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,        # single chunk for typical Short
                "total_chunk_count": 1,
            },
        }
        with httpx.Client(timeout=60) as c:
            r = c.post(f"{API}/post/publish/video/init/", headers=self._headers(), json=init_body)
            r.raise_for_status()
            data = r.json()
            if data.get("error", {}).get("code") not in (None, "ok"):
                raise RuntimeError(f"init error: {data['error']}")
            publish_id = data["data"]["publish_id"]
            upload_url = data["data"]["upload_url"]

            # 2) upload the bytes
            with open(file_path, "rb") as fh:
                payload = fh.read()
            put_headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(size),
                "Content-Range": f"bytes 0-{size - 1}/{size}",
            }
            pr = c.put(upload_url, headers=put_headers, content=payload)
            pr.raise_for_status()

        status = self._poll_status(publish_id)
        return {
            "remote_id": publish_id,
            "remote_url": status.get("share_url") or "",
            "publish_status": status.get("status", "PROCESSING"),
        }

    def _poll_status(self, publish_id: str, tries: int = 8) -> dict[str, Any]:
        with httpx.Client(timeout=30) as c:
            for _ in range(tries):
                r = c.post(
                    f"{API}/post/publish/status/fetch/",
                    headers=self._headers(),
                    json={"publish_id": publish_id},
                )
                if r.status_code == 200:
                    d = r.json().get("data", {})
                    st = d.get("status")
                    if st in {"PUBLISH_COMPLETE", "FAILED"}:
                        return d
                time.sleep(3)
        return {"status": "PROCESSING"}

    # ---- publish (queue-aware) -------------------------------------------
    def publish(self, video_id: str, file_path: str, meta: dict[str, Any]) -> dict[str, Any]:
        with db.session() as s:
            row = Upload(video_id=video_id, platform=PLATFORM, status="queued", payload=meta)
            s.add(row)
            s.flush()
            upload_id = row.id

        if settings.dry_run or not settings.enable_uploads:
            log.info("upload_tiktok: DRY_RUN/disabled — queued %s", video_id)
            return {"status": "queued", "upload_id": upload_id}
        if not self.available:
            log.info("upload_tiktok: no access token — queued %s", video_id)
            return {"status": "queued", "upload_id": upload_id}
        return self._attempt(upload_id, file_path, meta)

    def _attempt(self, upload_id: int, file_path: str, meta: dict[str, Any]) -> dict[str, Any]:
        with db.session() as s:
            row = s.get(Upload, upload_id)
            if not row:
                return {"status": "failed"}
            row.status, row.attempts = "uploading", (row.attempts or 0) + 1
        try:
            res = self.do_upload(file_path, meta)
            with db.session() as s:
                row = s.get(Upload, upload_id)
                row.status = "published"
                row.remote_id = res["remote_id"]
                row.remote_url = res.get("remote_url", "")
                row.published_at = utcnow()
            log.info("upload_tiktok: published %s (publish_id=%s)", file_path, res["remote_id"])
            return {"status": "published", "upload_id": upload_id, **res}
        except Exception as exc:  # noqa: BLE001
            with db.session() as s:
                row = s.get(Upload, upload_id)
                row.status = "queued"
                row.last_error = str(exc)[:480]
            log.warning("upload_tiktok: failed (%s) — re-queued %s", exc, upload_id)
            return {"status": "queued", "upload_id": upload_id, "error": str(exc)}

    def retry_queue(self, limit: int = 10) -> int:
        if not self.available or settings.dry_run or not settings.enable_uploads:
            return 0
        done = 0
        with db.session() as s:
            rows = s.scalars(
                select(Upload).where(Upload.platform == PLATFORM, Upload.status == "queued").limit(limit)
            ).all()
            jobs = [(r.id, r.video_id, dict(r.payload or {})) for r in rows]
        for upload_id, video_id, meta in jobs:
            with db.session() as s:
                v = s.get(Video, video_id)
                path = v.file_path if v else None
            if not path or not Path(path).exists():
                continue
            res = self._attempt(upload_id, path, meta)
            if res.get("status") == "published":
                done += 1
        if done:
            log.info("upload_tiktok: cleared %d queued uploads", done)
        return done


tiktok_uploader = TikTokUploader()
