"""SQLAlchemy ORM models mirroring the §3.3 schema.

The authoritative DDL lives in the Alembic migration (raw SQL, exactly as the
plan specifies). These ORM classes are the application-side view of the same
tables. Keep the two in sync — column names, types and defaults here must match
the migration.
"""

from __future__ import annotations

from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .db import Base


class Source(Base):
    """Raw ingested item (arXiv paper, SEC filing, Polymarket market, ...)."""

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("source_class", "external_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_class: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    # arXiv subject categories (e.g. ['cs.AI', 'cs.CR']); drives select.py.
    categories: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    cleaned_text: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    license_flag: Mapped[str | None] = mapped_column(Text)
    scrape_allowed: Mapped[bool | None] = mapped_column(
        Boolean, server_default=text("TRUE")
    )
    url_hash: Mapped[str] = mapped_column(Text, nullable=False)
    content_simhash: Mapped[int | None] = mapped_column(BigInteger)
    weight: Mapped[float | None] = mapped_column(Float, server_default=text("0.5"))


class Run(Base):
    """One pipeline attempt == one article. Drives the §3.4 state machine."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[str] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    article_type: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'raw'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    payload_hash: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Claim(Base):
    """Provenance, first-class: a claim locked to an immutable source span."""

    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_span: Mapped[str] = mapped_column(Text, nullable=False)
    span_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    entailment_score: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)


class Article(Base):
    """A drafted/published article and its quality + provenance metadata."""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    # Content vertical (multi-vertical isolation). Default "aixcrypto" backfills
    # existing rows; new verticals write their slug at pipeline persist time.
    vertical: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'aixcrypto'")
    )
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    dek: Mapped[str | None] = mapped_column(Text)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    body_final_md: Mapped[str | None] = mapped_column(Text)
    envelope_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    claims_used: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    quality_score: Mapped[float | None] = mapped_column(Float)
    fact_pass_rate: Mapped[float | None] = mapped_column(Float)
    similarity_max: Mapped[float | None] = mapped_column(Float)
    # --- Corrections / retraction workflow (Phase 3) ---------------------
    correction_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    correction_notes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    retraction_reason: Mapped[str | None] = mapped_column(Text)
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- Programmatic SEO clusters (Phase 3) -----------------------------
    # Supporting cluster members are de-indexed with the pillar as their canonical.
    noindex: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    canonical_url: Mapped[str | None] = mapped_column(Text)
    review_path: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'auto_gated'")
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'drafted'")
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class BudgetDay(Base):
    """Atomic daily budget (O-C3). See ``reserve_budget()`` SQL function.
    
    Composite PK (day, vertical) so each vertical has its own ceiling.
    """

    __tablename__ = "budget_day"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    vertical: Mapped[str] = mapped_column(
        Text, nullable=False, primary_key=True, server_default=text("'aixcrypto'")
    )
    reserved_usd: Mapped[float] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    actual_usd: Mapped[float] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    ceiling_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    escalations: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    escalation_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("5")
    )


class SpendLedger(Base):
    """Per-call spend records: reservations and reconciled actuals."""

    __tablename__ = "spend_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    in_tokens: Mapped[int | None] = mapped_column(Integer)
    out_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    kind: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class SystemState(Base):
    """Kill-switch and feature flags (key/value)."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    reason: Mapped[str | None] = mapped_column(Text)


class Eval(Base):
    """Gate / eval / human judge scores per article (O-C2)."""

    __tablename__ = "evals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int | None] = mapped_column(BigInteger)
    judge_kind: Mapped[str | None] = mapped_column(Text)
    judge_model: Mapped[str | None] = mapped_column(Text)
    scores_json: Mapped[dict | None] = mapped_column(JSONB)
    weighted: Mapped[float | None] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class SourceHealth(Base):
    """Ingestion volume / error counts per source per window (Phase 2, O-M2)."""

    __tablename__ = "source_health"

    source_class: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    items_seen: Mapped[int | None] = mapped_column(Integer)
    errors: Mapped[int | None] = mapped_column(Integer)


class Distribution(Base):
    """A repackaged article payload for one channel (X thread / Telegram post).

    Generation and posting are decoupled: `newsroom distribute` writes a row with
    status='generated'; the operator/agent posts via post_thread.py / telegram skill
    and updates status='posted' + external_url. Keeps the site a static SSG (this is
    DB-only state, never rendered into web/).
    """

    __tablename__ = "distributions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int | None] = mapped_column(BigInteger)
    # Extension seam: new distribution targets = new channel values (e.g. discord,
    # webhook, api) + a corresponding poster — no schema change needed.
    channel: Mapped[str] = mapped_column(Text, nullable=False)  # 'x' | 'telegram'
    variant: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'default'")
    )
    # Structured payload: X => {hooks[], body_tweets[], closing}; TG => {bullets[]}.
    payload_json: Mapped[dict | None] = mapped_column(JSONB)
    # Ready-to-post rendered text (X: hook A assembled thread; TG: full message).
    rendered_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'generated'")
    )  # 'generated' | 'posted' | 'failed'
    external_url: Mapped[str | None] = mapped_column(Text)  # tweet/post URL once posted
    # Per-distribution engagement, filled during the daily runbook from X/Telegram
    # analytics — lets us pick the winning A/B hook variant per row.
    impressions: Mapped[int | None] = mapped_column(Integer)
    link_clicks: Mapped[int | None] = mapped_column(Integer)
    in_tokens: Mapped[int | None] = mapped_column(Integer)
    out_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

