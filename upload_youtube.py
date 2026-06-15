"""
upload_youtube.py — YouTube Shorts publishing via the official Data API v3.

Auth: OAuth client secrets + a pre-authorized refresh token (set up once,
out of band). If credentials are missing or the API errors, the upload is
QUEUED in the `uploads` table and retried later — it never crashes the run.

A video <60s, 9:16, with #Shorts in the title/description is treated as a Short
by YouTube automatically.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from database import Upload, db
from settings import settings
from utils import get_logger, iso, utcnow

log = get_logger("upload_youtube")

PLATFORM = "youtube_shorts"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubeUploader:
    @property
    def available(self) -> bool:
        return bool(settings.youtube_refresh_token) and Path(settings.youtube_client_secrets_file).exists()

    # ---- credentials -----------------------------------------------------
    def _credentials(self):
        from google.oauth2.credentials import Credentials

        secrets = json.loads(Path(settings.youtube_client_secrets_file).read_text())
        block = secrets.get("installed") or secrets.get("web") or {}
        return Credentials(
            token=None,
            refresh_token=settings.youtube_refresh_token,
            token_uri=block.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=block.get("client_id"),
            client_secret=block.get("client_secret"),
            scopes=SCOPES,
        )

    def _service(self):
        from googleapiclient.discovery import build

        return build("youtube", "v3", credentials=self._credentials(), cache_discovery=False)

    # ---- raw upload (raises on failure) ----------------------------------
    def do_upload(self, file_path: str, meta: dict[str, Any]) -> dict[str, Any]:
        from googleapiclient.http import MediaFileUpload

        youtube = self._service()
        body = {
            "snippet": {
                "title": meta.get("title", "World Cup 2026")[:100],
                "description": meta.get("description", "")[:4900],
                "tags": meta.get("tags", [])[:30],
                "categoryId": settings.youtube_category_id,
            },
            "status": {
                "privacyStatus": settings.youtube_privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(file_path, chunksize=-1, resumable=True, mimetype="video/mp4")
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _status, response = request.next_chunk()
        vid = response.get("id")
        return {"remote_id": vid, "remote_url": f"https://youtube.com/shorts/{vid}"}

    # ---- publish (queue-aware) -------------------------------------------
    def publish(self, video_id: str, file_path: str, meta: dict[str, Any]) -> dict[str, Any]:
        with db.session() as s:
            row = Upload(video_id=video_id, platform=PLATFORM, status="queued", payload=meta)
            s.add(row)
            s.flush()
            upload_id = row.id

        if settings.dry_run or not settings.enable_uploads:
            log.info("upload_youtube: DRY_RUN/disabled — queued %s", video_id)
            return {"status": "queued", "upload_id": upload_id}

        if not self.available:
            log.info("upload_youtube: no credentials — queued %s for later", video_id)
            return {"status": "queued", "upload_id": upload_id}

        return self._attempt(upload_id, file_path, meta)

    def _attempt(self, upload_id: int, file_path: str, meta: dict[str, Any]) -> dict[str, Any]:
        with db.session() as s:
            row = s.get(Upload, upload_id)
            if not row:
                return {"status": "failed", "error": "missing upload row"}
            row.status, row.attempts = "uploading", (row.attempts or 0) + 1
        try:
            res = self.do_upload(file_path, meta)
            with db.session() as s:
                row = s.get(Upload, upload_id)
                row.status = "published"
                row.remote_id = res["remote_id"]
                row.remote_url = res["remote_url"]
                row.published_at = utcnow()
            log.info("upload_youtube: published %s -> %s", file_path, res["remote_url"])
            return {"status": "published", "upload_id": upload_id, **res}
        except Exception as exc:  # noqa: BLE001
            with db.session() as s:
                row = s.get(Upload, upload_id)
                row.status = "queued"  # keep retrying
                row.last_error = str(exc)[:480]
            log.warning("upload_youtube: failed (%s) — re-queued %s", exc, upload_id)
            return {"status": "queued", "upload_id": upload_id, "error": str(exc)}

    def retry_queue(self, limit: int = 10) -> int:
        if not self.available or settings.dry_run or not settings.enable_uploads:
            return 0
        from database import Video

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
            log.info("upload_youtube: cleared %d queued uploads", done)
        return done


youtube_uploader = YouTubeUploader()
