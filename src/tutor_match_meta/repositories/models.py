"""PostgreSQL schema.

Notable choices, each with a reason:

* **`tutor_projection` is a sanitised copy, never an authority.** It carries a
  `source_checksum` and `synced_at` so staleness is measurable and a
  reconciliation job can detect drift without re-reading every row.
* **`conversation_state.lock_version`** implements the optimistic lock; the
  repository's UPDATE is conditional on it.
* **`idempotency_record` and `outbox_event` both carry unique keys**, which is
  what makes "exactly once" a database property rather than a hope.
* **No table stores a raw phone number, email or street address.** Parent
  identity is a peppered hash; tutor identity is `user_id`.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: JSONB on PostgreSQL, plain JSON on SQLite so the schema is testable locally.
JSONType = JSON().with_variant(JSONB(), "postgresql")  # type: ignore[no-untyped-call]


#: Read at import time because SQLAlchemy binds the schema into the metadata
#: before any settings object exists. Overridable for tests and for a
#: deployment that gives TutorMatch its own database.
SCHEMA: str | None = os.getenv("TMM_POSTGRES_SCHEMA", "tutor_match") or None
if SCHEMA in {"public", ""}:
    SCHEMA = None


class Base(DeclarativeBase):
    """All tables live in `SCHEMA`, keeping us out of another service's way."""

    metadata = MetaData(schema=SCHEMA)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TutorProjection(Base, TimestampMixin):
    """Sanitised search projection of the website's tutor data."""

    __tablename__ = "tutor_projection"

    tutor_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    public_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    gender: Mapped[str | None] = mapped_column(String(16))
    avatar_url: Mapped[str | None] = mapped_column(String(512))

    city: Mapped[str | None] = mapped_column(String(80), index=True)
    locality: Mapped[str | None] = mapped_column(String(120))
    district: Mapped[str | None] = mapped_column(String(80))
    state: Mapped[str | None] = mapped_column(String(80))
    pincode: Mapped[str | None] = mapped_column(String(12), index=True)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geo_granularity: Mapped[str | None] = mapped_column(String(16))

    # Arrays as JSON: the union of two source schemas is variable-length and is
    # always read whole, so a normalised child table would add joins for nothing.
    subjects: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    boards: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    classes: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    modes: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)

    experience_years: Mapped[int | None] = mapped_column(Integer)
    education: Mapped[str | None] = mapped_column(String(400))
    profile_summary: Mapped[str | None] = mapped_column(Text)

    fee_min: Mapped[int | None] = mapped_column(Integer)
    fee_max: Mapped[int | None] = mapped_column(Integer)
    fee_label: Mapped[str | None] = mapped_column(String(64))

    review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rating_avg: Mapped[float | None] = mapped_column(Float)
    expertise_avg: Mapped[float | None] = mapped_column(Float)
    patience_avg: Mapped[float | None] = mapped_column(Float)
    reliability_avg: Mapped[float | None] = mapped_column(Float)
    communication_avg: Mapped[float | None] = mapped_column(Float)
    latest_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    active_students: Mapped[int | None] = mapped_column(Integer)
    travel_radius_km: Mapped[float | None] = mapped_column(Float)

    projection_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Detects drift without diffing every column during reconciliation.
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_tutor_projection_city_synced", "city", "synced_at"),
        Index("ix_tutor_projection_synced", "synced_at"),
        CheckConstraint("review_count >= 0", name="ck_tutor_review_count_non_negative"),
        CheckConstraint(
            "rating_avg IS NULL OR (rating_avg >= 0 AND rating_avg <= 5)",
            name="ck_tutor_rating_range",
        ),
    )


class TutorAvailability(Base, TimestampMixin):
    """TMM-owned. The website schema has none (docs/assumptions.md A4)."""

    __tablename__ = "tutor_availability"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tutor_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(48), default="Asia/Kolkata", nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tutor_id", "weekday", "start_minute", name="uq_availability_slot"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_availability_weekday"),
        CheckConstraint("end_minute > start_minute", name="ck_availability_order"),
    )


class ConversationStateRow(Base, TimestampMixin):
    __tablename__ = "conversation_state"

    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    fsm_version: Mapped[str] = mapped_column(String(8), nullable=False)
    #: Optimistic lock. Every UPDATE is conditional on this value.
    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    history: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)

    __table_args__ = (Index("ix_conversation_state_state", "state"),)


class RequirementRow(Base, TimestampMixin):
    __tablename__ = "match_requirement"

    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(8), nullable=False)
    lead_id: Mapped[str | None] = mapped_column(String(128), index=True)
    #: The full `MatchRequirementV1`, including per-field confidence and
    #: provenance — the audit trail for why a filter was applied.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)


class MatchDecisionRow(Base, TimestampMixin):
    """The auditable record of one matching run."""

    __tablename__ = "match_decision"

    match_session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    policy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Detects an unversioned edit to a live policy after the fact.
    policy_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    matched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    no_match_reason: Mapped[str | None] = mapped_column(String(120))
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    candidate_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    rejections: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    score_snapshot: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    shortlist: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    degraded_sources: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    oldest_source_data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_decision_conversation_generated", "conversation_id", "generated_at"),
    )


class MatchFeedbackRow(Base, TimestampMixin):
    __tablename__ = "match_feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tutor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(400))

    __table_args__ = (
        UniqueConstraint("match_session_id", "tutor_id", "outcome", name="uq_feedback"),
    )


class IdempotencyRecord(Base):
    """Durable dedup. The authoritative layer behind the timestamp window."""

    __tablename__ = "idempotency_record"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_idempotency_expires", "expires_at"),)


class OutboxEvent(Base, TimestampMixin):
    """Transactional outbox: written with the state change that caused it."""

    __tablename__ = "outbox_event"

    dedup_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Lease timestamp. Set when a relay claims the row; cleared on failure or
    #: by `reclaim_stale` when the claiming relay died mid-batch.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(240))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_outbox_status_available", "status", "available_at"),
        # Finds abandoned leases without scanning delivered history.
        Index("ix_outbox_claimed", "claimed_at"),
        CheckConstraint(
            "status IN ('pending','claiming','delivered','failed','dead')",
            name="ck_outbox_status",
        ),
    )


class ApprovalRow(Base, TimestampMixin):
    """HITL queue for commands the agent may not execute on its own."""

    __tablename__ = "approval_request"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    command: Mapped[str] = mapped_column(String(64), nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(120))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_approval_status", "status"),)


class LLMUsageRow(Base):
    """Cost telemetry. Tokens and estimated spend per call, never prompt text."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    conversation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_llm_usage_created", "created_at"),)


class GeoPointRow(Base, TimestampMixin):
    """Pincode/locality -> coordinates. Never a street address (A5)."""

    __tablename__ = "geo_point"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    granularity: Mapped[str] = mapped_column(String(16), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)


class RagDocument(Base, TimestampMixin):
    __tablename__ = "rag_document"

    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    source: Mapped[str] = mapped_column(String(240), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(24), default="public", nullable=False)
    access_scope: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class RagChunk(Base, TimestampMixin):
    """A chunk with full provenance. Content is DATA, never instructions."""

    __tablename__ = "rag_chunk"

    chunk_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(24), default="public", nullable=False)
    access_scope: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    #: pgvector column added by the migration when the extension is available;
    #: retrieval degrades to lexical search when it is not.
    embedding_model: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("document_id", "document_version", "chunk_number", name="uq_chunk"),
    )


class SyncCheckpoint(Base, TimestampMixin):
    """Resume point for the incremental projection sync."""

    __tablename__ = "sync_checkpoint"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_offset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(240))


# ----------------------------------------------------------- shared store
# The serverless replacement for ElastiCache. See cache/postgres_store.py.


class KvEntry(Base):
    """L2 cache. An in-process L1 fronts this, so reads are rare."""

    __tablename__ = "kv_entry"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_kv_entry_expires", "expires_at"),)


class RateBucket(Base):
    """Token bucket shared across Lambda containers.

    Updated by one atomic statement; never read-modify-written in Python.
    """

    __tablename__ = "rate_bucket"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    tokens: Mapped[float] = mapped_column(Float, nullable=False)
    #: The refill is derived from this, so it is load-bearing, not metadata.
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_rate_bucket_expires", "expires_at"),)


class KillSwitchRow(Base):
    """Operator emergency flags. `actor` and `reason` are mandatory."""

    __tablename__ = "kill_switch"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PromptVersionRow(Base):
    """Prompt registry. A prompt change is a production change."""

    __tablename__ = "prompt_version"

    prompt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    model_compatibility: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), default="system", nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnalyticsEvent(Base):
    """Sanitised analytics. Aggregatable dimensions only, never message text."""

    __tablename__ = "analytics_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_name: Mapped[str] = mapped_column(String(48), nullable=False)
    conversation_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    match_session_id: Mapped[str | None] = mapped_column(String(64))
    policy_ref: Mapped[str | None] = mapped_column(String(64))
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_analytics_export", "exported_at", "occurred_at"),
        Index("ix_analytics_name", "event_name"),
    )
