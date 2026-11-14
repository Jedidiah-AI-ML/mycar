"""Application settings, read from environment variables (and `.env` in development).

Adapted from vpm-backend's `app/core/config.py`: same `lru_cache` pattern, moved to
Pydantic v2 `SettingsConfigDict`, Redis/OpenAI settings removed (ADR-004, PRD 7).
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULT_SECRET = "change-me"  # noqa: S105 - placeholder, refused in production


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_name: str = "AutoCare"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    timezone: str = "Africa/Lagos"

    # --- Database (also hosts the job queue, ADR-003/004) ---
    database_url: str = "postgresql+asyncpg://autocare:autocare@localhost:5433/autocare"
    db_pool_size: int = 5
    db_connect_timeout_s: float = 5.0

    # --- Auth ---  # SEED - validate (S2): token lifetimes are a product trade-off
    jwt_secret: SecretStr = SecretStr(INSECURE_DEFAULT_SECRET)
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 30
    jwt_refresh_ttl_days: int = 30

    # --- Privacy ---  # SEED - validate (S3): NDPA sets no fixed purge period
    account_purge_days: int = 30

    # --- CORS: comma-separated origins ---
    cors_origins: str = "http://localhost:3000"

    # --- ML / datasets (outside the repo) ---
    datasets_dir: Path | None = None

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg_driver(cls, v: str) -> str:
        # Railway (and most hosts) hand out postgres:// or postgresql:// URLs.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix) :]
        return v

    @field_validator("datasets_dir", mode="before")
    @classmethod
    def _empty_path_is_none(cls, v: object) -> object:
        return None if v == "" else v

    @model_validator(mode="after")
    def _refuse_insecure_production(self) -> "Settings":
        if (
            self.environment == "production"
            and self.jwt_secret.get_secret_value() == INSECURE_DEFAULT_SECRET
        ):
            raise ValueError("JWT_SECRET must be set in production")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
