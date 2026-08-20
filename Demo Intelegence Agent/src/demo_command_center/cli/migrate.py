"""`dcc-migrate` — apply the numbered SQL migrations through the Data API.

No Alembic. The Tutor Intelligence service owns the Alembic environment and its
own schema; running a second Alembic against the same database would give one
cluster two migration histories that each believe they are authoritative.

Instead: numbered `.sql` files, applied in order, recorded in
`dcc.schema_migrations` with a checksum. A file that changes after being applied
is a hard error — editing an applied migration is how two environments end up
with silently different schemas.

`--dry-run` prints the plan without touching the cluster, and is what CI runs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

from demo_command_center.config.settings import PersistenceMode, get_settings
from demo_command_center.storage.data_api.client import (
    DataApiClient,
    DataApiConfig,
    validate_schema,
)

MIGRATIONS_DIRNAME = "migrations"


def _migration_dir() -> Path:
    for base in (Path.cwd(), *Path(__file__).resolve().parents):
        candidate = base / MIGRATIONS_DIRNAME
        if candidate.is_dir() and any(candidate.glob("*.sql")):
            return candidate
    raise FileNotFoundError("no migrations directory with .sql files was found")


def _discover() -> list[tuple[str, Path, str]]:
    """`(version, path, checksum)` sorted by filename."""
    out: list[tuple[str, Path, str]] = []
    for path in sorted(_migration_dir().glob("*.sql")):
        raw = path.read_bytes()
        out.append((path.stem, path, hashlib.sha256(raw).hexdigest()))
    return out


async def _apply(dry_run: bool) -> int:
    settings = get_settings()
    migrations = _discover()
    print(f"found {len(migrations)} migration(s) in {_migration_dir()}")

    if settings.persistence_mode is PersistenceMode.MEMORY:
        print("persistence_mode=memory — nothing to migrate.")
        for version, _, checksum in migrations:
            print(f"  would apply {version} ({checksum[:12]})")
        return 0

    schema = validate_schema(settings.aurora_schema)
    client = DataApiClient(
        DataApiConfig(
            cluster_arn=settings.aurora_cluster_arn,
            secret_arn=settings.aurora_secret_arn,
            database=settings.aurora_database,
            schema=schema,
            region=settings.aws_region,
        )
    )

    await client.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    await client.execute(
        f"CREATE TABLE IF NOT EXISTS {schema}.schema_migrations ("
        " version text PRIMARY KEY,"
        " checksum text NOT NULL,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied = {
        str(row["version"]): str(row["checksum"])
        for row in await client.execute(
            f"SELECT version, checksum FROM {schema}.schema_migrations"  # noqa: S608
        )
    }

    pending: list[tuple[str, Path, str]] = []
    for version, path, checksum in migrations:
        previous = applied.get(version)
        if previous is None:
            pending.append((version, path, checksum))
        elif previous != checksum:
            print(
                f"ERROR: {version} was applied with checksum {previous[:12]} but the file "
                f"now hashes to {checksum[:12]}. Editing an applied migration is not "
                "supported; add a new one."
            )
            return 1
        else:
            print(f"  already applied: {version}")

    if not pending:
        print("schema is up to date.")
        return 0

    for version, path, checksum in pending:
        print(f"  applying {version} ...", end=" ", flush=True)
        if dry_run:
            print("(dry run)")
            continue
        # Statements are split on `;` at line ends: the Data API executes one
        # statement per call, and these files contain no functions or DO blocks
        # where a semicolon could appear inside a body.
        for statement in _statements(path.read_text(encoding="utf-8")):
            await client.execute(statement)
        await client.execute(
            f"INSERT INTO {schema}.schema_migrations (version, checksum, applied_at) "  # noqa: S608
            "VALUES (:version, :checksum, :now)",
            {"version": version, "checksum": checksum, "now": datetime.now(UTC)},
        )
        print("done")

    print(f"applied {len(pending)} migration(s).")
    return 0


def _statements(sql: str) -> list[str]:
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Demo Command Center migrations")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = parser.parse_args()
    return asyncio.run(_apply(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
