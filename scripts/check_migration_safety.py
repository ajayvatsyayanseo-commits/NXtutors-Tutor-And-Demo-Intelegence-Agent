"""Migration safety gate. Fails the deploy on a destructive change.

Additive-only in the automated pipeline (docs/runbooks/migrations.md). Anything
that drops, narrows or takes a long lock needs a human to run it deliberately.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"

FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bop\.drop_table\b"), "drops a table"),
    (re.compile(r"\bop\.drop_column\b"), "drops a column"),
    (re.compile(r"\bDROP\s+TABLE\b", re.I), "raw DROP TABLE"),
    (re.compile(r"\bDROP\s+COLUMN\b", re.I), "raw DROP COLUMN"),
    (re.compile(r"\bTRUNCATE\b", re.I), "truncates data"),
    (
        re.compile(r"add_column\([^)]*nullable\s*=\s*False", re.S),
        "adds a NOT NULL column; needs a server_default and a backfill",
    ),
)

#: A plain CREATE INDEX locks writes on these tables, which stalls the sync job
#: and ages the projection out of the recommendable window.
LARGE_TABLES = ("tutor_projection", "match_decision", "llm_usage")


_CREATE_TABLE = re.compile(r'op\.create_table\(\s*["\'](\w+)["\']')
_CREATE_INDEX = re.compile(r'op\.create_index\(\s*(?:[^,]+),\s*["\'](\w+)["\']')
#: Raw SQL index builds. Checked separately: routing an index through
#: `op.execute` used to slip straight past the `op.create_index` check, which
#: made the gate trivially bypassable by whoever most wanted to bypass it.
_RAW_CREATE_INDEX = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX(?P<concurrently>\s+CONCURRENTLY)?"
    r"(?:\s+IF\s+NOT\s+EXISTS)?\s+\w+\s+ON\s+(?:\w+\.)?(?P<table>\w+)",
    re.I,
)


def _index_problems(name: str, upgrade: str) -> list[str]:
    """Flag a blocking index build on a table that already holds data.

    An index on a table created in the *same* migration is safe: the table is
    empty and invisible to other transactions, and `CREATE INDEX CONCURRENTLY`
    cannot run inside a transaction anyway — so requiring it there would make
    the initial migration impossible to write.
    """
    created_here = set(_CREATE_TABLE.findall(upgrade))
    problems: list[str] = []

    for target in _CREATE_INDEX.findall(upgrade):
        if target in created_here or target not in LARGE_TABLES:
            continue
        if "postgresql_concurrently=True" not in upgrade:
            problems.append(
                f"{name}: index on existing large table {target!r} must use "
                "postgresql_concurrently=True (and run outside a transaction)"
            )

    for match in _RAW_CREATE_INDEX.finditer(upgrade):
        target = match.group("table")
        if target in created_here or target not in LARGE_TABLES:
            continue
        if not match.group("concurrently"):
            problems.append(
                f"{name}: raw CREATE INDEX on existing large table {target!r} must be "
                "CONCURRENTLY, inside op.get_context().autocommit_block()"
            )
        elif "autocommit_block" not in upgrade:
            problems.append(
                f"{name}: CREATE INDEX CONCURRENTLY on {target!r} must run inside "
                "op.get_context().autocommit_block(); it cannot run in a transaction"
            )
    return problems


def main() -> int:
    if not VERSIONS.is_dir():
        print("no migrations directory; nothing to check")
        return 0

    migrations = sorted(VERSIONS.glob("*.py"))
    problems: list[str] = []
    for path in migrations:
        source = path.read_text(encoding="utf-8")
        # Only the upgrade path runs automatically; downgrade is manual.
        upgrade = source.split("def downgrade")[0]
        for pattern, reason in FORBIDDEN:
            if pattern.search(upgrade):
                problems.append(f"{path.name}: {reason}")
        problems.extend(_index_problems(path.name, upgrade))

    if problems:
        print("MIGRATION SAFETY GATE FAILED\n")
        for problem in problems:
            print(f"  - {problem}")
        print("\nSee docs/runbooks/migrations.md. Destructive changes are run by a human.")
        return 1

    print(f"migration safety gate passed ({len(migrations)} migration(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
