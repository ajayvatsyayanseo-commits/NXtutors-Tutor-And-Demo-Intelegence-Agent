"""Dump a schema's data to a replayable SQL file. Read-only against the database.

Written to preserve the previous `demo_agent` implementation's rows before that
schema is dropped. It emits `CREATE TABLE` DDL reconstructed from the catalogue
plus `INSERT` statements for every non-empty table, so the file can be replayed
into a scratch database if anything in it turns out to matter.

Deliberately not `pg_dump`: that binary is not installed here, and shelling out
to a tool that may not exist would fail at exactly the wrong moment.

    python scripts/backup_schema.py demo_agent
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
OUT_DIR = ROOT / "dist"


def read_dsn() -> str:
    for line in (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("TMM_POSTGRES_DSN="):
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            return re.sub(r"\?.*$", "", raw.replace("postgresql+asyncpg://", "postgresql://"))
    raise SystemExit("TMM_POSTGRES_DSN not found in .env")


def literal(value: Any) -> str:
    """A SQL literal for one Python value, quoted safely.

    Single quotes are doubled, which is the SQL standard escape. This output is
    a backup artefact replayed by a human, never executed against a live
    system by this program.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, datetime):
        return "'" + value.isoformat() + "'"
    if isinstance(value, dict | list):
        import json

        return "'" + json.dumps(value).replace("'", "''") + "'::jsonb"
    if isinstance(value, bytes):
        return "'\\x" + value.hex() + "'::bytea"
    return "'" + str(value).replace("'", "''") + "'"


async def main() -> int:
    import asyncpg

    schema = sys.argv[1] if len(sys.argv) > 1 else "demo_agent"
    connection = await asyncpg.connect(read_dsn(), ssl="require")

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = OUT_DIR / f"{schema}_backup_{stamp}.sql"

    lines: list[str] = [
        f"-- Backup of schema `{schema}` taken {datetime.now(UTC).isoformat()}",
        "-- Reconstructed from the system catalogue: DDL plus data for every",
        "-- non-empty table. Replay into a scratch database, never into production.",
        "",
        f"CREATE SCHEMA IF NOT EXISTS {schema};",
        "",
    ]

    try:
        tables = [
            row["table_name"]
            for row in await connection.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = $1 ORDER BY table_name",
                schema,
            )
        ]
        if not tables:
            print(f"schema {schema} is absent or empty — nothing to back up")
            return 0

        total_rows = 0
        populated = 0

        for table in tables:
            columns = await connection.fetch(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = $2 "
                "ORDER BY ordinal_position",
                schema,
                table,
            )
            definitions = []
            for column in columns:
                piece = f'  "{column["column_name"]}" {column["data_type"]}'
                if column["column_default"]:
                    piece += f" DEFAULT {column['column_default']}"
                if column["is_nullable"] == "NO":
                    piece += " NOT NULL"
                definitions.append(piece)

            lines.append(f'CREATE TABLE IF NOT EXISTS {schema}."{table}" (')
            lines.append(",\n".join(definitions))
            lines.append(");")
            lines.append("")

            rows = await connection.fetch(
                f'SELECT * FROM "{schema}"."{table}"'  # noqa: S608 - identifiers from the catalogue
            )
            if not rows:
                continue

            populated += 1
            total_rows += len(rows)
            names = ", ".join(f'"{key}"' for key in rows[0].keys())
            lines.append(f"-- {table}: {len(rows)} row(s)")
            for row in rows:
                values = ", ".join(literal(value) for value in row.values())
                lines.append(f'INSERT INTO {schema}."{table}" ({names}) VALUES ({values});')  # noqa: S608
            lines.append("")

        destination.write_text("\n".join(lines), encoding="utf-8")

        print(f"schema      : {schema}")
        print(f"tables      : {len(tables)} ({populated} with data)")
        print(f"rows        : {total_rows}")
        print(f"written to  : {destination}")
        print(f"size        : {destination.stat().st_size / 1024:.1f} KB")
        return 0
    finally:
        await connection.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
