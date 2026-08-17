"""Does the database actually match what this build expects?

The failure this prevents is specific and nasty: a table or column that the ORM
references but the schema does not have. Nothing detects it at deploy time, so
it surfaces as `relation "tutor_match.x" does not exist` **in the middle of a
parent's conversation** — one code path at a time, on whichever turn happens to
touch that table first. A rarely-used table (`sync_checkpoint`, `geo_point`)
can be missing for days before anyone notices.

That is not hypothetical here. `sync_checkpoint` was created without a schema
qualifier for the entire life of migration 0001; the ORM looked for
`tutor_match.sync_checkpoint` and the table was in `public`. The projection
sync would have failed on its first run and every run after.

So the check runs where it can stop a bad deployment:

    GET /internal/v1/ready   →  503 with the missing names
    tutor-match-doctor       →  a FAIL line

It is a catalogue read — a handful of rows from `information_schema`, no table
scans — so it is cheap enough to run on every readiness probe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.version import EXPECTED_SCHEMA_REVISION

logger = get_logger("schema_check")

#: Tables the running code reads or writes. Derived from the ORM models plus
#: the raw-SQL stores, and asserted against the migrations by
#: `tests/unit/test_migrations.py` — so this list cannot drift from reality
#: without a test failing.
REQUIRED_TABLES: frozenset[str] = frozenset(
    {
        # matching path
        "tutor_projection",
        "tutor_availability",
        "conversation_state",
        "match_requirement",
        "match_decision",
        "idempotency_record",
        "outbox_event",
        # shared store — rate limits and kill switches are correctness-critical
        "kv_entry",
        "rate_bucket",
        "kill_switch",
        # offline and observability
        "sync_checkpoint",
        "llm_usage",
        "analytics_event",
        "geo_point",
        "embedding_ledger",
        # declared but not yet wired; still expected to exist so a later
        # release does not discover them missing at runtime
        "match_feedback",
        "approval_request",
        "approval_audit",
        "prompt_version",
        "rag_document",
        "rag_chunk",
        "alembic_version",
    }
)

#: Columns added after the initial release. A table can exist while a migration
#: that widened it has not run, and that is exactly as broken as a missing
#: table — `outbox_event` without `claimed_at` means the relay lease silently
#: does nothing and two relays can deliver the same reply.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "outbox_event": frozenset({"claimed_at", "status", "attempts", "dedup_key"}),
    "tutor_projection": frozenset({"synced_at", "source_checksum", "public_ref"}),
    "conversation_state": frozenset({"lock_version"}),
    "rate_bucket": frozenset({"tokens", "updated_at", "expires_at"}),
    "embedding_ledger": frozenset({"content_checksum", "embedding_model"}),
}


@dataclass(slots=True)
class SchemaReport:
    schema: str
    reachable: bool = False
    revision: str | None = None
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.reachable
            and not self.missing_tables
            and not self.missing_columns
            and self.revision == EXPECTED_SCHEMA_REVISION
        )

    def problems(self) -> list[str]:
        if not self.reachable:
            return [f"database_unreachable:{self.error or 'unknown'}"]
        found: list[str] = []
        if self.missing_tables:
            found.append(f"missing_tables:{','.join(self.missing_tables)}")
        if self.missing_columns:
            found.append(f"missing_columns:{','.join(self.missing_columns)}")
        if self.revision != EXPECTED_SCHEMA_REVISION:
            # Reported, never auto-corrected. Running migrations from a
            # readiness probe is how two containers race each other.
            found.append(
                f"schema_revision_mismatch:found={self.revision or 'none'},"
                f"expected={EXPECTED_SCHEMA_REVISION}"
            )
        return found


#: A PostgreSQL identifier. SQL cannot parameterise a table name, so the one
#: place a name must be interpolated it is validated first — and validated
#: here rather than trusted from the caller, so the guarantee is local.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class SchemaVerifier:
    """Compares the live catalogue with what this build needs.

    A class rather than a bare function purely so the one interpolated table
    name lives on `self`, set once in `__init__` from validated configuration.
    That is the convention every other SQL-touching module here follows
    (`PostgresKeyValueStore`, `PostgresRateLimitStore`, `StoredGeocoder`), and
    `tests/security/test_sql_injection.py` enforces it: a query may interpolate
    `self.<attr>` or a module constant, never a parameter.
    """

    def __init__(self, sessions: Any, *, schema: str) -> None:
        if not _IDENTIFIER.match(schema):
            raise ValueError(f"refusing to query an invalid schema identifier: {schema!r}")
        self._sessions = sessions
        self._schema = schema
        self._version_table = f"{schema}.alembic_version"

    async def run(self) -> SchemaReport:
        report = SchemaReport(schema=self._schema)
        try:
            async with self._sessions() as session:
                present = {
                    row[0]
                    for row in (
                        await session.execute(
                            text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
                            ),
                            {"s": self._schema},
                        )
                    ).all()
                }
                report.reachable = True
                report.missing_tables = sorted(REQUIRED_TABLES - present)

                if "alembic_version" in present:
                    report.revision = await session.scalar(
                        text(f"SELECT version_num FROM {self._version_table} LIMIT 1")  # noqa: S608
                    )

                columns = {
                    (row[0], row[1])
                    for row in (
                        await session.execute(
                            text(
                                "SELECT table_name, column_name FROM information_schema.columns "
                                "WHERE table_schema = :s"
                            ),
                            {"s": self._schema},
                        )
                    ).all()
                }
                report.missing_columns = sorted(
                    f"{table}.{column}"
                    for table, required in REQUIRED_COLUMNS.items()
                    if table in present
                    for column in required
                    if (table, column) not in columns
                )
        except Exception as exc:
            report.error = type(exc).__name__
            logger.error(
                "schema verification failed",
                extra={"tmm_error": type(exc).__name__, "tmm_detail": str(exc)[:200]},
            )

        if report.ok:
            logger.info(
                "schema verified",
                extra={"tmm_schema": self._schema, "tmm_revision": report.revision},
            )
        else:
            logger.error(
                "schema verification found problems", extra={"tmm_problems": report.problems()}
            )
        return report


async def verify(sessions: Any, *, schema: str) -> SchemaReport:
    """Read the catalogue and compare it with what this build needs."""
    return await SchemaVerifier(sessions, schema=schema).run()


__all__ = [
    "REQUIRED_COLUMNS",
    "REQUIRED_TABLES",
    "SchemaReport",
    "SchemaVerifier",
    "verify",
]
