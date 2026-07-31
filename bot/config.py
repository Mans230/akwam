"""Bot configuration via environment variables (pydantic-settings)."""
from __future__ import annotations

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    BOT_TOKEN: str = ""
    AKWAM_DOMAIN: str = "https://akwam.it"
    STARCIMA_DOMAIN: str = "https://starcima.com"
    MOVIEBOX_DOMAIN: str = "https://themoviebox.xyz"
    ADMIN_IDS: Annotated[list[int], NoDecode] = []
    FORCE_CHANNEL: str = ""
    BOT_API_SERVER: str = ""
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    DEFAULT_MAX_CONCURRENT: int = 1
    DOWNLOAD_SEGMENTS: int = 8
    REQUIRE_APPROVAL: bool = False
    PREMIUM_SEGMENTS: int = 16
    CACHE_TTL_HOURS: int = 6
    EPISODES_PER_PAGE: int = 20
    SERVERS_PER_PAGE: int = 10
    DOWNLOAD_DIR: str = "./downloads"
    DB_PATH: str = "./data/bot.db"

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def _parse_admins(cls, v):
        if isinstance(v, str):
            v = v.strip().strip("[]")
            return [int(x.strip().strip('"').strip("'")) for x in v.split(",") if x.strip()]
        return v

    @field_validator("AKWAM_DOMAIN", "STARCIMA_DOMAIN", "MOVIEBOX_DOMAIN")
    @classmethod
    def _strip_domain(cls, v: str) -> str:
        return v.rstrip("/")


settings = Settings()
