"""Production hardening: outbox leases, hot-path indexes, embedding ledger.

Three unrelated-looking changes that all came out of the same pre-release
adversarial pass:

1. **`outbox_event.claimed_at` plus a `claiming` status.** The relay used to
   `SELECT … FOR UPDATE SKIP LOCKED` in an autocommit session and never write
   the row, so the lock died with the session and two overlapping relays could
   both deliver the same reply. The claim is now a real state transition, and
   `claimed_at` is the lease that lets a crashed relay's rows be recovered.

2. **Indexes for the queries that actually run.** `EXPLAIN ANALYZE` on the
   candidate query (docs/production-control-matrix.md, control 20) shows the
   planner reaching for `synced_at` first on every request; the composite
   `(synced_at, city)` and the partial fee index turn two of the three hot
   shapes into index-only scans. `ix_outbox_relay` is partial on the two
   statuses the relay polls, so it stays small however much delivered history
   accumulates.

3. **`embedding_ledger`.** Content-hash bookkeeping so unchanged RAG chunks are
   never re-embedded. Without it every ingestion run pays the full embedding
   bill again for a corpus that did not change.

Additive only — no column is dropped and no existing row is rewritten, so this
runs online. The `CHECK` swap is the one statement that takes a brief ACCESS
EXCLUSIVE lock; `outbox_event` is small by construction (delivered rows are
purged daily) so the lock is measured in milliseconds.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from tutor_match_meta.repositories.models import SCHEMA

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_OUTBOX = f"{SCHEMA}.outbox_event" if SCHEMA else "outbox_event"
_PROJECTION = f"{SCHEMA}.tutor_projection" if SCHEMA else "tutor_projection"

#: Built CONCURRENTLY so the 15-minute projection sync is never blocked.
#: Each is `IF NOT EXISTS` so a retry after a partial run is a no-op.
_CONCURRENT_INDEXES: tuple[str, ...] = (
    # Freshness leads every non-`public_ref` search, so it leads the composite.
    # City is second: next-most selective, and present on most requirements.
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tutor_projection_synced_city "
    f"ON {_PROJECTION} (synced_at, city)",
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tutor_projection_synced_pincode "
    f"ON {_PROJECTION} (synced_at, pincode)",
    # The ORDER BY that decides the bounded pool. Without it the planner sorts
    # the whole fresh set on every single request.
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tutor_projection_rank "
    f"ON {_PROJECTION} (review_count DESC, rating_avg DESC NULLS LAST, tutor_id)",
    # Fee filtering only ever applies to rows that declare a floor.
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tutor_projection_fee_min "
    f"ON {_PROJECTION} (fee_min) WHERE fee_min IS NOT NULL",
    # The explicit "this tutor" lookup must not table-scan.
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tutor_projection_public_ref "
    f"ON {_PROJECTION} (public_ref)",
)


def upgrade() -> None:
    # ------------------------------------------------- 1. outbox lease
    op.add_column(
        "outbox_event",
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        schema=SCHEMA,
    )
    op.create_index("ix_outbox_claimed", "outbox_event", ["claimed_at"], schema=SCHEMA)

    # Widen the status domain before any row can carry the new value.
    op.execute(f"ALTER TABLE {_OUTBOX} DROP CONSTRAINT IF EXISTS ck_outbox_status")
    op.create_check_constraint(
        "ck_outbox_status",
        "outbox_event",
        "status IN ('pending','claiming','delivered','failed','dead')",
        schema=SCHEMA,
    )

    # The relay only ever polls these two states. A partial index keeps the
    # scan proportional to the backlog rather than to delivered history.
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ix_outbox_relay
            ON {_OUTBOX} (available_at NULLS FIRST, created_at)
         WHERE status IN ('pending', 'claiming')
        """
    )

    # ------------------------------------------- 2. candidate-query indexes
    #
    # `tutor_projection` is live and continuously written by the 15-minute sync
    # job. A plain CREATE INDEX takes a SHARE lock, which blocks that job for
    # the duration of the build and ages the projection out of the recommendable
    # window — exactly the staleness the freshness alarm exists to catch. Every
    # one of these is therefore CONCURRENTLY, inside an autocommit block because
    # CONCURRENTLY cannot run in a transaction.
    #
    # Trade-off accepted: a CONCURRENTLY build that fails leaves an INVALID
    # index behind. docs/runbooks/migrations.md has the drop-and-retry step.
    with op.get_context().autocommit_block():
        for statement in _CONCURRENT_INDEXES:
            op.execute(statement)

    # ------------------------------------------------ 3. embedding ledger
    op.create_table(
        "embedding_ledger",
        sa.Column("chunk_id", sa.String(160), primary_key=True),
        # The content hash at the time of embedding. Unchanged hash => skip.
        sa.Column("content_checksum", sa.String(64), nullable=False),
        sa.Column("embedding_model", sa.String(64), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_micros", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "embedded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_embedding_ledger_checksum",
        "embedding_ledger",
        ["content_checksum", "embedding_model"],
        schema=SCHEMA,
    )

    # ------------------------------------------- 4. HITL approval audit trail
    # "Who approved what, and what did it change" is a compliance requirement
    # (docs/production-control-matrix.md, control 12), and the approval row
    # alone cannot answer it — it holds the current state, not the transition.
    op.create_table(
        "approval_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("reason", sa.String(400), nullable=False, server_default=""),
        sa.Column("before_state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("after_state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("trace_id", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_approval_audit_key", "approval_audit", ["idempotency_key"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_table("approval_audit", schema=SCHEMA)
    op.drop_table("embedding_ledger", schema=SCHEMA)
    for name in (
        "ix_tutor_projection_public_ref",
        "ix_tutor_projection_fee_min",
        "ix_tutor_projection_rank",
        "ix_tutor_projection_synced_pincode",
        "ix_tutor_projection_synced_city",
    ):
        op.execute(f"DROP INDEX IF EXISTS {SCHEMA + '.' if SCHEMA else ''}{name}")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA + '.' if SCHEMA else ''}ix_outbox_relay")
    op.execute(f"ALTER TABLE {_OUTBOX} DROP CONSTRAINT IF EXISTS ck_outbox_status")
    op.create_check_constraint(
        "ck_outbox_status",
        "outbox_event",
        "status IN ('pending','delivered','failed','dead')",
        schema=SCHEMA,
    )
    op.drop_index("ix_outbox_claimed", "outbox_event", schema=SCHEMA)
    op.drop_column("outbox_event", "claimed_at", schema=SCHEMA)
