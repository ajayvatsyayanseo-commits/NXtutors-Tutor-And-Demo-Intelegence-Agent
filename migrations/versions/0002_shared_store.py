"""Shared store: the serverless replacement for ElastiCache.

Three small tables carrying the state that genuinely has to be shared across
Lambda containers. Adding them let ElastiCache be removed entirely — see
`cache/postgres_store.py` for the reasoning.

`rate_bucket` is the one that matters for correctness: the token bucket refill
and decrement happen inside a single `INSERT … ON CONFLICT DO UPDATE`, so
concurrent Lambdas serialise on the row lock instead of racing a
read-modify-write. A per-container in-memory bucket gives an effective limit of
`N_containers × limit`, which is not a limit at all.

Additive only. No index here is on a large table, so nothing needs
`CONCURRENTLY`.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from tutor_match_meta.repositories.models import SCHEMA

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- L2 cache. Read-mostly; an in-process L1 fronts it.
    op.create_table(
        "kv_entry",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=SCHEMA,
    )
    # Sweeping expired rows is the retention job's whole query.
    op.create_index("ix_kv_entry_expires", "kv_entry", ["expires_at"], schema=SCHEMA)

    # --- Token buckets. Written on every rate-limit check, so kept narrow.
    op.create_table(
        "rate_bucket",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("tokens", sa.Float(), nullable=False),
        # The refill is computed from this, so it is load-bearing, not metadata.
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_rate_bucket_expires", "rate_bucket", ["expires_at"], schema=SCHEMA)

    # --- Operator kill switches. Tiny table, read via a short-TTL cache.
    op.create_table(
        "kill_switch",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Who and why are mandatory: an unexplained pause during an incident is
        # nearly as bad as no pause at all.
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("reason", sa.String(240), nullable=False),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=SCHEMA,
    )

    # --- Prompt registry. A prompt change is a production change, so it needs
    #     the same versioning and rollback story as code.
    op.create_table(
        "prompt_version",
        sa.Column("prompt_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.String(16), primary_key=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("model_compatibility", sa.String(240), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(120), nullable=False, server_default="system"),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=SCHEMA,
    )

    # --- Sanitised analytics. The Glue/S3 export reads from here, never from
    #     the conversation transcript.
    op.create_table(
        "analytics_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_name", sa.String(48), nullable=False),
        sa.Column("conversation_ref", sa.String(64), nullable=False),
        sa.Column("match_session_id", sa.String(64)),
        sa.Column("policy_ref", sa.String(64)),
        # Aggregatable dimensions only. No message text, no identifiers.
        sa.Column("dimensions", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("exported_at", sa.DateTime(timezone=True)),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_analytics_export", "analytics_event", ["exported_at", "occurred_at"], schema=SCHEMA
    )
    op.create_index("ix_analytics_name", "analytics_event", ["event_name"], schema=SCHEMA)


def downgrade() -> None:
    for table in (
        "analytics_event",
        "prompt_version",
        "kill_switch",
        "rate_bucket",
        "kv_entry",
    ):
        op.drop_table(table, schema=SCHEMA)
