"""Application settings, read from environment variables (and `.env` in development).

Adapted from vpm-backend's `app/core/config.py`: same `lru_cache` pattern, moved to
Pydantic v2 `SettingsConfigDict`, Redis/OpenAI settings removed (ADR-004, PRD 7).
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

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
    db_connect_timeout_s: float = 15.0  # Neon free tier can take ~5 s to wake from idle
    # Close pooled connections after this long unused, so nothing holds a serverless
    # database awake (ADR-008). 0 disables.
    db_idle_release_s: float = 60.0

    # --- Background jobs (ADR-004, ADR-008) ---
    # embedded: worker runs inside the API process (default; one service, DB can sleep)
    # standalone: run `python -m app.jobs.worker` as its own service
    # off: no worker in this process (tests, or a standalone worker elsewhere)
    worker_mode: Literal["embedded", "standalone", "off"] = "embedded"
    worker_listen: bool = False  # standalone only; LISTEN keeps the DB awake
    worker_max_idle_s: float = 6 * 3600  # SEED - validate (S14)

    # --- Auth ---  # SEED - validate (S2): token lifetimes are a product trade-off
    jwt_secret: SecretStr = SecretStr(INSECURE_DEFAULT_SECRET)
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 30
    jwt_refresh_ttl_days: int = 30

    # --- Devices ---
    device_online_s: int = 300  # SEED - validate (P9): "online" = seen within 5 min
    device_id_prefix: str = "FM1"  # auto-generated ids: FM1-0001, FM1-0002, ...
    # Ingestion (plan step 8). SEED - validate (P8, P10)
    clock_skew_threshold_ms: int = 2000  # correct device clocks only beyond this
    ingest_max_body_bytes: int = 256 * 1024  # gzip body as sent
    ingest_max_decoded_bytes: int = 2 * 1024 * 1024  # after decompression (gzip bombs)
    ingest_rate_per_min: int = 60  # requests per device; a backlog drains at this pace
    command_resend_s: int = 120  # resend a command the device didn't confirm (lost ack)
    trip_assemble_delay_s: int = 60  # debounce: one assembly per burst of batches
    # Local exit-gate run only (plan step 10): trust an `X-Sim-Server-Time` header as the
    # server clock, so a simulated week can be replayed in minutes. Refused in production.
    ingest_sim_clock: bool = False
    trip_close_after_s: int = 900  # SEED - validate (P11): no data for 15 min -> trip over
    trip_gap_s: int = 10  # SEED - validate (P14): a hole in 1 Hz data longer than this is a gap

    # --- LLM (Phase 4, ADR-013). Empty key -> no LLM calls, templates only. ---
    llm_model: str = "openrouter/openai/gpt-4o-mini"  # OpenRouter key (sk-or-...); plain
    # "openai/gpt-4o-mini" needs an OpenAI key instead
    llm_api_key: SecretStr = SecretStr("")
    llm_monthly_budget_usd: float = 2.0  # SEED - validate (D5): past it, no live calls
    llm_timeout_s: float = 30.0

    # --- Privacy ---  # SEED - validate (S3): NDPA sets no fixed purge period
    account_purge_days: int = 30

    # --- CORS: comma-separated origins ---
    cors_origins: str = "http://localhost:3000"

    # --- ML / datasets (outside the repo) ---
    datasets_dir: Path | None = None

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg_driver(cls, v: str) -> str:
        """Accept the URL exactly as a host (Railway, Neon) hands it out.

        - `postgres://` / `postgresql://` -> `postgresql+asyncpg://`
        - libpq's `sslmode=require` -> asyncpg's `ssl=require`
        - every other query parameter (`channel_binding`, `options`, ...) is dropped:
          SQLAlchemy passes them to asyncpg.connect() as keyword arguments, and asyncpg
          raises TypeError on any it doesn't know.
        - surrounding quotes/whitespace from copy-paste are removed
        """
        v = v.strip().strip("'\"").strip()
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                v = "postgresql+asyncpg://" + v[len(prefix) :]
                break
        url = make_url(v)
        q = dict(url.query)
        ssl = q.get("ssl") or q.get("sslmode")
        query = {"ssl": ssl} if ssl and ssl != "disable" else {}
        return url.set(query=query).render_as_string(hide_password=False)

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
        if self.environment == "production" and self.ingest_sim_clock:
            raise ValueError("INGEST_SIM_CLOCK is for local simulation runs, never production")
        return self

    @property
    def raw_asyncpg_dsn(self) -> str:
        """Plain DSN for `asyncpg.connect()` (the worker's LISTEN connection).
        asyncpg's own DSN parser understands `sslmode`, not `ssl`."""
        url = make_url(self.database_url).set(drivername="postgresql")
        query = dict(url.query)
        if "ssl" in query:
            query["sslmode"] = query.pop("ssl")
        return url.set(query=query).render_as_string(hide_password=False)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
