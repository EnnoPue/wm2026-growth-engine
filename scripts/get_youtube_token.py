"""
scripts/get_youtube_token.py — one-time helper to mint a YouTube refresh token.

Run this LOCALLY (it opens a browser). It uses the OAuth client JSON you
downloaded from Google Cloud Console (Desktop app credentials) and prints a
refresh token to paste into .env as YOUTUBE_REFRESH_TOKEN.

    python scripts/get_youtube_token.py path/to/client_secret.json
"""
from __future__ import annotations

import sys

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/get_youtube_token.py <client_secret.json>")
        return 2
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as exc:  # pragma: no cover
        print(f"missing deps: {exc}\n  pip install google-auth-oauthlib")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    creds = flow.run_local_server(port=0)
    print("\n=== SUCCESS ===")
    print("Add this to your .env:")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
    print(f"YOUTUBE_CLIENT_SECRETS_FILE={sys.argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
