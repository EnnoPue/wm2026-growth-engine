"""
tiktok_oauth.py — TikTok Login Kit OAuth + token storage with auto-refresh.

TikTok access tokens expire after ~24h. Storing a static token in an env var
therefore breaks autonomous posting after a day. This module instead persists
the full token set (access + refresh + open_id + expiry) in the `app_state`
table and transparently refreshes the access token shortly before it expires.

Flow:
    authorize_url()  -> send the user to TikTok to authorize
    exchange_code()  -> swap the returned ?code for tokens
    save_tokens()    -> persist to DB
    valid_access_token() -> always returns a fresh, usable access token
                            (refreshing on the fly), or the static env token as
                            a last-resort fallback.

Nothing here raises into the upload pipeline; failures degrade to None.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from database import db
from settings import settings
from utils import get_logger

log = get_logger("tiktok_oauth")

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
SCOPES = "user.info.basic,video.publish,video.upload"
STATE_KEY = "tiktok_oauth"          # app_state key holding the token record
NONCE_KEY = "tiktok_oauth_state"    # app_state key holding the CSRF state nonce
_REFRESH_SKEW = 120                 # refresh this many seconds before expiry


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------- #
# OAuth steps
# --------------------------------------------------------------------------- #
def authorize_url(redirect_uri: str, state: str) -> str:
    """Build the TikTok authorize URL the user clicks to grant access."""
    q = {
        "client_key": settings.tiktok_client_key or "",
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(q)}"


def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange an authorization code for an access/refresh token set."""
    data = {
        "client_key": settings.tiktok_client_key or "",
        "client_secret": settings.tiktok_client_secret or "",
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    with httpx.Client(timeout=30) as c:
        r = c.post(TOKEN_URL, data=data,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.json()


def _refresh(refresh_token: str) -> dict[str, Any]:
    data = {
        "client_key": settings.tiktok_client_key or "",
        "client_secret": settings.tiktok_client_secret or "",
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    with httpx.Client(timeout=30) as c:
        r = c.post(TOKEN_URL, data=data,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.json()


# --------------------------------------------------------------------------- #
# Token persistence
# --------------------------------------------------------------------------- #
def save_tokens(tok: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    record = {
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "open_id": tok.get("open_id"),
        "scope": tok.get("scope"),
        "expires_at": now + int(tok.get("expires_in", 0) or 0),
        "refresh_expires_at": now + int(tok.get("refresh_expires_in", 0) or 0),
        "saved_at": now,
    }
    db.set_state(STATE_KEY, record)
    return record


def load_tokens() -> dict[str, Any] | None:
    try:
        return db.get_state(STATE_KEY)
    except Exception:  # pragma: no cover - DB not ready
        return None


def valid_access_token() -> str | None:
    """Return a usable access token, refreshing it if it is about to expire.

    Falls back to the static TIKTOK_ACCESS_TOKEN env var when no OAuth token has
    been stored yet (useful for manual testing)."""
    rec = load_tokens()
    if rec and rec.get("access_token"):
        if (rec.get("expires_at", 0) - _now()) <= _REFRESH_SKEW and rec.get("refresh_token"):
            try:
                fresh = _refresh(rec["refresh_token"])
                if fresh.get("access_token"):
                    fresh.setdefault("open_id", rec.get("open_id"))
                    rec = save_tokens(fresh)
                    log.info("tiktok_oauth: access token refreshed")
            except Exception as exc:  # noqa: BLE001
                log.warning("tiktok_oauth: refresh failed (%s) — using existing token", exc)
        return rec.get("access_token")
    return (settings.tiktok_access_token or "").strip() or None


def open_id() -> str | None:
    rec = load_tokens() or {}
    return rec.get("open_id") or (settings.tiktok_open_id or "").strip() or None


def connected() -> bool:
    return bool(valid_access_token())


# ---- CSRF state nonce (for the /connect -> /tiktok/callback round-trip) ----- #
def remember_state(state: str) -> None:
    db.set_state(NONCE_KEY, state)


def state_ok(state: str) -> bool:
    try:
        return bool(state) and db.get_state(NONCE_KEY) == state
    except Exception:  # pragma: no cover
        return False
