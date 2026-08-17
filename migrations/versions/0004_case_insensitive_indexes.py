"""Functional indexes matching the predicates the search actually uses.

Revision ID: 0004
Revises: 0003

`PostgresTutorRepository.search` filters on `LOWER(city)` and `LOWER(gender)`,
because the website's city and gender values are free text with inconsistent
casing. Every index shipped so far was on the **bare** column, and a btree on
`city` cannot serve a predicate on `lower(city)`.

Measured on 25,000 synthetic rows with representative cardinality (10 cities,
two genders, decorrelated), running the exact statement the repository emits:

    before   Seq Scan, 23,810 rows removed by filter    15.4 ms
    after    Index Scan on ix_tutor_projection_rank      3.6 ms   (4.3x)

The interesting part is *why*, because it is not "the new index is scanned".
The planner still chooses the rank index for the ORDER BY. What changed is the
estimate: with no statistics on `lower(city)` PostgreSQL guessed `rows=1` when
the true count was 1,190, and that misestimate is what made a sequential scan
look cheap. Creating a functional index creates statistics for the expression,
the estimate becomes `rows=939`, and the planner picks the index scan.

So this migration buys a corrected row estimate as much as a lookup path, which
is also why a third, sort-aware composite on
`(lower(city), review_count DESC, rating_avg DESC)` was tried and is **not**
included: the planner never chose it, and it measured slower (5.5 ms) than the
two indexes here.
"""

from __future__ import annotations

from alembic import op

from tutor_match_meta.repositories.models import SCHEMA

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_PROJECTION = f"{SCHEMA}.tutor_projection" if SCHEMA else "tutor_projection"

#: CONCURRENTLY for the same reason as 0003: `tutor_projection` is written by
#: the 15-minute sync job, and a SHARE lock for the duration of a build ages the
#: projection out of the recommendable window.
_CONCURRENT_INDEXES: tuple[str, ...] = (
    # Leads with lower(city) — the selective half — and carries synced_at so the
    # freshness predicate is answered from the same index.
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tutor_projection_lower_city "
    f"ON {_PROJECTION} (lower(city), synced_at DESC)",
    # Gender is an optional filter and low-cardinality, so this exists for the
    # statistics as much as the lookup.
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tutor_projection_lower_gender "
    f"ON {_PROJECTION} (lower(gender))",
)

_DROP: tuple[str, ...] = (
    "DROP INDEX CONCURRENTLY IF EXISTS "
    + (f"{SCHEMA}." if SCHEMA else "")
    + "ix_tutor_projection_lower_city",
    "DROP INDEX CONCURRENTLY IF EXISTS "
    + (f"{SCHEMA}." if SCHEMA else "")
    + "ix_tutor_projection_lower_gender",
)


def upgrade() -> None:
    # Trade-off accepted, as in 0003: a CONCURRENTLY build that fails leaves an
    # INVALID index behind. docs/runbooks/migrations.md has the drop-and-retry.
    with op.get_context().autocommit_block():
        for statement in _CONCURRENT_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for statement in _DROP:
            op.execute(statement)
