"""Read-only inventory of what already exists in the target database.

Written because the target turned out to contain a `demo_agent` schema with 48
tables that did not come from `migrations/0001_dcc_schema.sql`. Creating a
parallel `dcc` schema without first understanding that would leave two
half-populated schemas for the same service.

Strictly read-only: it opens no transaction and issues no DDL.

    python scripts/inspect_existing_schema.py [schema ...]
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent


def read_dsn() -> str:
    env = REPO_ROOT / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("TMM_POSTGRES_DSN="):
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            return re.sub(r"\?.*$", "", raw.replace("postgresql+asyncpg://", "postgresql://"))
    raise SystemExit("TMM_POSTGRES_DSN not found in .env")


def migration_tables() -> set[str]:
    tables: set[str] = set()
    for path in (ROOT / "migrations").glob("*.sql"):
        tables.update(
            re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", path.read_text(encoding="utf-8"))
        )
    return tables


async def main() -> int:
    import asyncpg

    targets = sys.argv[1:] or ["demo_agent", "dcc"]
    connection = await asyncpg.connect(read_dsn(), ssl="require")
    expected = migration_tables()

    try:
        for schema in targets:
            rows = await connection.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = $1 ORDER BY table_name",
                schema,
            )
            if not rows:
                print(f"\n=== {schema} === (absent or empty)")
                continue

            names = [row["table_name"] for row in rows]
            print(f"\n=== {schema} === {len(names)} tables")

            for name in names:
                count = await connection.fetchval(
                    f'SELECT count(*) FROM "{schema}"."{name}"'  # noqa: S608 - identifiers from catalogue
                )
                columns = await connection.fetchval(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = $1 AND table_name = $2",
                    schema,
                    name,
                )
                print(f"  {name:<44} {columns:>3} cols {count:>8} rows")

            # How much of this schema corresponds to the committed migration,
            # allowing for a `dcc_` prefix that a different tool may have dropped.
            stripped = {re.sub(r"^dcc_", "", n) for n in names}
            wanted = {re.sub(r"^dcc_", "", n) for n in expected}
            print(f"\n  overlap with migrations/ : {len(stripped & wanted)}/{len(wanted)}")
            extra = sorted(stripped - wanted)
            if extra:
                print(f"  present but NOT in the migration ({len(extra)}):")
                for name in extra:
                    print(f"    - {name}")
            missing = sorted(wanted - stripped)
            if missing:
                print(f"  in the migration but NOT present ({len(missing)}):")
                for name in missing:
                    print(f"    - {name}")
    finally:
        await connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
