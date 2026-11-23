"""Layer 2: what fault codes mean (definitions), what we told the owner (insights), and
what every LLM call cost (llm_calls)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

SEVERITIES = ("info", "watch", "service_soon", "stop_safely")  # least to most urgent
INSIGHT_SOURCES = ("personalised", "generic", "template")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class DtcDefinition(Base):
    """A generic fault code in our own words. Source of truth: app/config/dtc/definitions.yaml
    (synced). Severity and stop-safely come from here, never from the LLM (PRD 5)."""

    __tablename__ = "dtc_definitions"

    code: Mapped[str] = mapped_column(String(5), primary_key=True)
    description: Mapped[str] = mapped_column(String(300))
    system: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    stop_safely: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    causes: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    escalation: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("code ~ '^[PCBU][0-9A-F]{4}$'", name="code_format"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity_known"),
        CheckConstraint(
            "stop_safely = (severity = 'stop_safely')", name="stop_safely_matches_severity"
        ),
    )


class Insight(Base):
    """Something we told the owner. For Phase 4: a fault-code explanation. Also the cache:
    the same vehicle, subject and input hash are never generated twice."""

    __tablename__ = "insights"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))  # dtc_explanation (Phase 6 adds more)
    subject: Mapped[str] = mapped_column(String(64))  # e.g. "P0171:stored"
    content: Mapped[dict] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(16))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("source", INSIGHT_SOURCES), name="source_known"),
        UniqueConstraint(
            "vehicle_id",
            "kind",
            "subject",
            "input_hash",
            name="uq_insights_cache",
            postgresql_nulls_not_distinct=True,
        ),  # fmt: skip
        Index("ix_insights_vehicle_kind_created", "vehicle_id", "kind", "created_at"),
    )


class LlmCall(Base):
    """Every attempt to call the LLM: what, which model and prompt, tokens, cost, outcome
    (PRD 5: every call logged). Feeds the monthly budget cap and the admin usage page."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    purpose: Mapped[str] = mapped_column(String(32))  # e.g. dtc_explain
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(64))
    tokens_in: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), server_default=text("0"))
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(String(500))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint(
            "cost_usd >= 0 AND tokens_in >= 0 AND tokens_out >= 0", name="non_negative"
        ),
        Index("ix_llm_calls_ts", "ts"),
    )
