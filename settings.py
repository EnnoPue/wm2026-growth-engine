"""
settings.py — central configuration.

Secrets/env  -> pydantic-settings (reads environment + .env)
Tunables     -> config.yaml (loaded into CONFIG dict)

Import anywhere:
    from settings import settings, CONFIG
Nothing here raises on missing values; everything has a safe default so the
pipeline can boot in a bare environment.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"

# Standard runtime directories (created on import so nothing has to mkdir later).
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
ASSETS_DIR = BASE_DIR / "assets"
TEMPLATES_DIR = BASE_DIR / "templates"
for _d in (OUTPUT_DIR, LOG_DIR, DATA_DIR, ASSETS_DIR, TEMPLATES_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[settings] WARNING: could not parse {path}: {exc}")
        return {}


CONFIG: dict[str, Any] = _load_yaml(CONFIG_PATH)


class Settings(BaseSettings):
    """Environment-backed secrets and switches. All optional."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # runtime
    env: str = "production"
    log_level: str = "INFO"
    tz: str = "UTC"
    port: int = 8080

    # database
    database_url: str = "postgresql://wm2026:wm2026@localhost:5432/wm2026"

    # AI
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    claude_max_tokens: int = 1200

    # football providers
    api_football_key: str = ""
    api_football_host: str = "v3.football.api-sports.io"
    api_football_league_id: int = 1
    football_data_key: str = ""
    football_data_competition: str = "WC"
    sportmonks_key: str = ""
    season: str = "2026"

    # pipeline
    videos_per_match: int = 5
    target_min_seconds: int = 20
    target_max_seconds: int = 45
    hard_min_seconds: int = 15
    hard_max_seconds: int = 60
    poll_interval_minutes: int = 15
    dry_run: bool = False
    enable_uploads: bool = True

    # rights discovery
    rights_discovery_enabled: bool = True
    youtube_data_api_key: str = ""

    # youtube upload
    youtube_client_secrets_file: str = "secrets/youtube_client_secret.json"
    youtube_token_file: str = "secrets/youtube_token.json"
    youtube_refresh_token: str = ""
    youtube_privacy_status: str = "public"
    youtube_category_id: str = "17"

    # tiktok upload
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_access_token: str = ""
    tiktok_open_id: str = ""
    tiktok_privacy_level: str = "PUBLIC_TO_EVERYONE"

    # team priority (comma separated; falls back to config.yaml)
    priority_teams: str = ""

    # ---- convenience accessors -------------------------------------------
    @property
    def has_claude(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    @property
    def priority_team_list(self) -> list[str]:
        if self.priority_teams.strip():
            return [t.strip() for t in self.priority_teams.split(",") if t.strip()]
        return list(CONFIG.get("team_priority", []))

    @property
    def target_seconds(self) -> tuple[int, int]:
        return (self.target_min_seconds, self.target_max_seconds)

    @property
    def hard_seconds(self) -> tuple[int, int]:
        return (self.hard_min_seconds, self.hard_max_seconds)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def cfg(path: str, default: Any = None) -> Any:
    """Dotted lookup into config.yaml, e.g. cfg('video.width', 1080)."""
    node: Any = CONFIG
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


# Honour TZ as early as possible.
os.environ.setdefault("TZ", settings.tz)
