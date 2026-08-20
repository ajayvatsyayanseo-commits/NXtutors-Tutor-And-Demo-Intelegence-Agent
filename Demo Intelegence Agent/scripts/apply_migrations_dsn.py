"""Apply Demo migrations over a direct Postgres DSN. An OPERATOR tool.

**Why this exists separately from `dcc-migrate`.**

`demo_command_center.cli.migrate` speaks the Aurora Data API, which is what the
deployed Lambdas use and what removes the need for a VPC-attached function. It
cannot use a DSN, and Demo deliberately ships **no Postgres driver** — that
absence is enforced by `scripts/scan_prohibited.py`.

This script is for the case where the target is reachable only by DSN: a plain
RDS instance, or a local/bootstrap database. It lives in `scripts/` (which the
prohibited-resource scan does not cover) and imports `asyncpg` from the ambient
environment. It is **not** a Demo runtime dependency and is never packaged.

Safety properties:

* Read-only by default. `--apply` is required to write anything.
* One transaction: either all 36 tables exist afterwards, or none were created.
* Every statement is `CREATE ... IF NOT EXISTS`, so a re-run is a no-op.
* It writes only inside the Demo schema and never touches another schema.
* A checksum is recorded in `<schema>.schema_migrations`; a changed file is refused.
* `--drop-existing` is DESTRUCTIVE and refuses to run against `public`,
  `tutor_match` or `information_schema`. The drop and the re-create share one
  transaction, so a failure leaves the old schema intact.

    python scripts/apply_migrations_dsn.py            # inspect only
    python scripts/apply_migrations_dsn.py --apply    # create the schema
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
MIGRATIONS = ROOT / "migrations"

#: The schema Demo owns. Nothing outside it is ever written.
DEMO_SCHEMA = "demo_agent"


def read_dsn() -> str:
    """Pull the DSN out of the repository `.env` and normalise it for asyncpg.

    The value is SQLAlchemy-shaped (`postgresql+asyncpg://…?ssl=require`).
    asyncpg wants a bare `postgresql://` URL and takes TLS as a keyword, so the
    driver suffix and the query string are stripped here.
    """
    env = REPO_ROOT / ".env"
    if not env.exists():
        raise SystemExit(f"no .env at {env}")

    for line in env.read_text(encoding="utf-8").splitlines():
        if not line.startswith("TMM_POSTGRES_DSN="):
            continue
        raw = line.split("=", 1)[1].strip().strip('"').strip("'")
        raw = raw.replace("postgresql+asyncpg://", "postgresql://")
        return re.sub(r"\?.*$", "", raw)

    raise SystemExit("TMM_POSTGRES_DSN not found in .env")


def masked(dsn: str) -> str:
    return re.sub(r"://[^:@/]+:[^@]+@", "://***:***@", dsn)


def statements(sql: str) -> list[str]:
    """Split on `;`, dropping comments. These files contain no function bodies,
    so a naive split is correct here and a SQL parser would be overkill."""
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def discovered() -> list[tuple[str, Path, str]]:
    out: list[tuple[str, Path, str]] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        raw = path.read_bytes()
        out.append((path.stem, path, hashlib.sha256(raw).hexdigest()))
    return out


async def inspect(connection) -> None:  # type: ignore[no-untyped-def]
    """Read-only. Says exactly what is there before anything is changed."""
    version = await connection.fetchval("SELECT version()")
    print(f"server      : {version.split(',')[0]}")

    database = await connection.fetchval("SELECT current_database()")
    user = await connection.fetchval("SELECT current_user")
    print(f"database    : {database}")
    print(f"user        : {user}")

    schemas = await connection.fetch(
        "SELECT nspname FROM pg_namespace "
        "WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema' "
        "ORDER BY nspname"
    )
    print(f"schemas     : {', '.join(row['nspname'] for row in schemas) or '(none)'}")

    for row in schemas:
        name = row["nspname"]
        count = await connection.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = $1", name
        )
        marker = "  <- Demo owns this" if name == DEMO_SCHEMA else ""
        print(f"  {name:<24} {count:>4} tables{marker}")


async def apply(connection, dry_run: bool, drop_first: bool = False) -> int:  # type: ignore[no-untyped-def]
    migrations = discovered()
    print(f"\nmigrations found: {len(migrations)}")

    async with connection.transaction():
        if drop_first and not dry_run:
            # Guarded three ways: the flag must be passed explicitly, the name
            # is the module constant (never an argument), and the whole thing
            # runs inside the same transaction as the re-create — so a failure
            # anywhere leaves the old schema intact.
            if DEMO_SCHEMA in {"public", "tutor_match", "information_schema"}:
                print(f"REFUSING to drop {DEMO_SCHEMA}: not a Demo-owned schema.")
                return 1
            print(f"  dropping schema {DEMO_SCHEMA} ...", end=" ", flush=True)
            await connection.execute(f"DROP SCHEMA IF EXISTS {DEMO_SCHEMA} CASCADE")
            print("done")

        await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {DEMO_SCHEMA}")
        await connection.execute(
            f"CREATE TABLE IF NOT EXISTS {DEMO_SCHEMA}.schema_migrations ("
            " version text PRIMARY KEY,"
            " checksum text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {
            row["version"]: row["checksum"]
            for row in await connection.fetch(
                f"SELECT version, checksum FROM {DEMO_SCHEMA}.schema_migrations"  # noqa: S608
            )
        }

        pending: list[tuple[str, Path, str]] = []
        for version, path, checksum in migrations:
            previous = applied.get(version)
            if previous is None:
                pending.append((version, path, checksum))
            elif previous != checksum:
                print(
                    f"ERROR: {version} was applied with checksum {previous[:12]} but the "
                    f"file now hashes to {checksum[:12]}.\n"
                    "       Editing an applied migration is not supported. Add a new one."
                )
                return 1
            else:
                print(f"  already applied: {version}")

        if not pending:
            print("schema is up to date.")
            return 0

        for version, path, checksum in pending:
            parts = statements(path.read_text(encoding="utf-8"))
            print(f"  {version}: {len(parts)} statements ...", end=" ", flush=True)
            if dry_run:
                print("(dry run — nothing written)")
                continue
            for statement in parts:
                await connection.execute(statement)
            await connection.execute(
                f"INSERT INTO {DEMO_SCHEMA}.schema_migrations (version, checksum) "  # noqa: S608
                "VALUES ($1, $2)",
                version,
                checksum,
            )
            print("done")

        if dry_run:
            # Roll the transaction back so a dry run leaves nothing behind —
            # not even the schema_migrations table.
            raise _DryRun

    return 0


class _DryRun(Exception):
    """Rolls the inspection transaction back. Never surfaces to the caller."""


async def verify(connection) -> int:  # type: ignore[no-untyped-def]
    """Count what actually exists now, and compare against the migration."""
    expected = set()
    for _, path, _ in discovered():
        expected.update(
            re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", path.read_text(encoding="utf-8"))
        )

    rows = await connection.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = $1 ORDER BY table_name",
        DEMO_SCHEMA,
    )
    actual = {row["table_name"] for row in rows}

    indexes = await connection.fetchval(
        "SELECT count(*) FROM pg_indexes WHERE schemaname = $1", DEMO_SCHEMA
    )

    print(f"\n{DEMO_SCHEMA}.* tables : {len(actual & expected)}/{len(expected)}")
    print(f"{DEMO_SCHEMA}.* indexes: {indexes}")

    missing = sorted(expected - actual)
    if missing:
        print(f"MISSING ({len(missing)}): {', '.join(missing)}")
        return 1

    for name in sorted(actual & expected):
        print(f"  {name}")
    print("\nOK: every table in the migration exists.")
    return 0


async def run(do_apply: bool, drop_first: bool = False) -> int:
    try:
        import asyncpg
    except ImportError:
        print(
            "asyncpg is not importable.\n"
            "This is an operator tool; Demo itself ships no Postgres driver by design.\n"
            "Run it with an interpreter that has asyncpg (the repository venv does)."
        )
        return 1

    dsn = read_dsn()
    print(f"target      : {masked(dsn)}")

    try:
        connection = await asyncio.wait_for(asyncpg.connect(dsn, ssl="require"), timeout=20)
    except TimeoutError:
        print(
            "\nFAIL: timed out connecting.\n"
            "      The instance is most likely not publicly reachable from here —\n"
            "      it is in a VPC, or its security group does not allow this address."
        )
        return 1
    except Exception as error:  # report, never raise a stack trace
        print(f"\nFAIL: could not connect ({type(error).__name__}).")
        return 1

    try:
        await inspect(connection)
        if not do_apply:
            print("\n--- DRY RUN: nothing was written. Re-run with --apply. ---")
            try:
                await apply(connection, dry_run=True)
            except _DryRun:
                pass
            return 0

        code = await apply(connection, dry_run=False, drop_first=drop_first)
        if code != 0:
            return code
        return await verify(connection)
    finally:
        await connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Demo migrations over a DSN")
    parser.add_argument("--apply", action="store_true", help="actually write (default: inspect)")
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="DESTRUCTIVE: drop the Demo schema and everything in it, then recreate",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.apply, args.drop_existing))


if __name__ == "__main__":
    sys.exit(main())
