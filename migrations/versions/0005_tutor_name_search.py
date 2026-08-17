"""Trigram index for tutor name lookup.

Revision ID: 0005
Revises: 0004

`LOWER(name) LIKE '%term%'` is a leading-wildcard match, and a btree cannot help
with one — not even a functional btree on `lower(name)`, because btrees only
answer prefix queries. Without an index this is a sequential scan of the whole
projection on every "tell me about Sneha Joshi".

`pg_trgm` is the right tool: a GIN index over trigrams answers contains-queries
directly. The extension is created here rather than assumed, and the whole
migration degrades gracefully if the role cannot create extensions — a
sequential scan over ~2k rows is a few milliseconds, so a missing index is a
performance note, not an outage. That matters because this service runs in a
shared database where we may not be superuser.

Leading-wildcard matching is deliberate. Parents write "joshi" for
"Sneha Joshi", and a prefix-only index would never find her.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from tutor_match_meta.repositories.models import SCHEMA

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_PROJECTION = f"{SCHEMA}.tutor_projection" if SCHEMA else "tutor_projection"
_QUALIFIED = f"{SCHEMA}." if SCHEMA else ""


def upgrade() -> None:
    connection = op.get_bind()

    try:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        has_trgm = True
    except Exception:  # pragma: no cover - depends on the grant, not the code
        # No CREATE privilege on extensions in this database. Say so loudly in
        # the migration log and carry on; the lookup still works, unindexed.
        has_trgm = False

    if has_trgm:
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tutor_projection_name_trgm "
                f"ON {_PROJECTION} USING gin (lower(name) gin_trgm_ops)"
            )
    else:
        # A plain functional index still helps the planner's estimate for
        # `lower(name)` even though it cannot serve the wildcard itself — the
        # same statistics effect measured in migration 0004.
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tutor_projection_lower_name "
                f"ON {_PROJECTION} (lower(name))"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_QUALIFIED}ix_tutor_projection_name_trgm")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_QUALIFIED}ix_tutor_projection_lower_name")
