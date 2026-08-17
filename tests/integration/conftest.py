"""Integration fixtures — a real PostgreSQL, in a throwaway schema.

**These do not run by default.** They are gated behind `-m integration` *and*
an explicit `TMM_INTEGRATION_DSN`, which is deliberately a different variable
from the application's `TMM_POSTGRES_DSN`. Pointing the suite at a shared
database by inheriting the app's configuration is exactly the accident this
separation prevents.

Two things here are load-bearing, and both were wrong on the first run.

**The schema name is exported before `repositories.models` is imported.**
`Base.metadata` reads `TMM_POSTGRES_SCHEMA` at *import* time and burns it into
every ORM table. A per-test schema therefore had no effect on
`PostgresIdempotencyStore`, `PostgresOutbox` or `PostgresConversationStore` —
they kept writing to the real `tutor_match` schema while the raw-SQL stores
(which take `schema=` explicitly) correctly used the throwaway one. Tests then
failed on state left by their siblings, and rows leaked into the live schema.

**No fixture here is session-scoped and async.** `pyproject.toml` sets
`asyncio_default_fixture_loop_scope = "function"`, so a session-scoped async
fixture raises `ScopeMismatch`. Setup is done once behind a module-level flag
from a function-scoped fixture, and teardown runs from `pytest_sessionfinish`,
which is synchronous.

Bring one up locally with:

    docker run --rm -e POSTGRES_PASSWORD=tmm -e POSTGRES_USER=tmm \\
        -e POSTGRES_DB=tmm -p 5433:5432 pgvector/pgvector:pg16
    export TMM_INTEGRATION_DSN=postgresql+asyncpg://tmm:tmm@localhost:5433/tmm
    uv run pytest -m integration
"""

from __future__ import annotations

import os
import secrets

import pytest

DSN_VAR = "TMM_INTEGRATION_DSN"

#: Fixed for the whole session, and exported *at import* so the ORM binds to it.
SCHEMA_NAME = f"tmm_it_{secrets.token_hex(6)}"
if os.getenv(DSN_VAR):
    os.environ["TMM_POSTGRES_SCHEMA"] = SCHEMA_NAME

#: DDL is expensive against a remote instance, so it runs once.
_prepared = False

#: One engine for the whole run, not one per fixture per test.
#:
#: Two engines × eighteen tests is thirty-six TLS handshakes to a remote
#: instance, which is both slow and — over any link that is not perfect —
#: unreliable: the first attempt died with `WinError 121` (socket timeout)
#: partway through. Reusing a pooled engine turns that into a handful of
#: connections held open for the run.
_engine: object | None = None

DDL = (
    "CREATE TABLE {s}.rate_bucket ("
    " key varchar(200) PRIMARY KEY,"
    " tokens double precision NOT NULL,"
    " updated_at timestamptz NOT NULL,"
    " expires_at timestamptz NOT NULL)",
    "CREATE TABLE {s}.kv_entry ("
    " key varchar(200) PRIMARY KEY,"
    " value text NOT NULL,"
    " expires_at timestamptz NOT NULL,"
    " created_at timestamptz NOT NULL DEFAULT now())",
    "CREATE TABLE {s}.kill_switch ("
    " name varchar(64) PRIMARY KEY,"
    " paused boolean NOT NULL DEFAULT false,"
    " actor varchar(120) NOT NULL,"
    " reason varchar(240) NOT NULL,"
    " changed_at timestamptz NOT NULL DEFAULT now())",
    "CREATE TABLE {s}.outbox_event ("
    " dedup_key varchar(128) PRIMARY KEY,"
    " kind varchar(48) NOT NULL,"
    " conversation_id varchar(128) NOT NULL,"
    " trace_id varchar(64) NOT NULL,"
    " payload jsonb NOT NULL,"
    " status varchar(16) NOT NULL DEFAULT 'pending',"
    " attempts integer NOT NULL DEFAULT 0,"
    " available_at timestamptz,"
    " claimed_at timestamptz,"
    " last_error varchar(240),"
    " delivered_at timestamptz,"
    " created_at timestamptz NOT NULL DEFAULT now(),"
    " updated_at timestamptz NOT NULL DEFAULT now(),"
    " CONSTRAINT ck_outbox_status CHECK (status IN"
    " ('pending','claiming','delivered','failed','dead')))",
    "CREATE TABLE {s}.idempotency_record ("
    " key varchar(128) PRIMARY KEY,"
    " claimed_at timestamptz NOT NULL DEFAULT now(),"
    " expires_at timestamptz NOT NULL)",
)

TABLES = ("rate_bucket", "kv_entry", "kill_switch", "outbox_event", "idempotency_record")


def integration_dsn() -> str | None:
    return (os.getenv(DSN_VAR) or "").strip() or None


@pytest.fixture
def require_postgres() -> str:
    dsn = integration_dsn()
    if not dsn:
        pytest.skip(
            f"{DSN_VAR} is not set. These tests need a disposable PostgreSQL; "
            "see the module docstring. Skipped is NOT a pass — the release-gate "
            "report records it as NOT EXECUTED."
        )
    return dsn


def _shared_engine(dsn: str) -> object:
    """The one engine, created lazily and reused for the whole run.

    **`NullPool` is required, not a tuning choice.** pytest-asyncio gives each
    test its own event loop, and a pooled asyncpg connection belongs to the
    loop that opened it. Reusing one across tests produces
    `coroutine 'Connection._cancel' was never awaited` and a cascade of setup
    errors. `NullPool` opens and closes a connection per checkout, so nothing
    ever crosses a loop boundary — while still keeping a single engine object
    rather than the thirty-six the first version created.
    """
    global _engine
    if _engine is None:
        from sqlalchemy import NullPool
        from sqlalchemy.ext.asyncio import create_async_engine

        _engine = create_async_engine(
            dsn,
            connect_args={"server_settings": {"search_path": SCHEMA_NAME}},
            poolclass=NullPool,
        )
    return _engine


@pytest.fixture
async def schema(require_postgres: str) -> str:
    """The throwaway schema, prepared once and truncated before each test."""
    global _prepared
    from sqlalchemy import text

    engine = _shared_engine(require_postgres)
    if not _prepared:
        async with engine.begin() as conn:  # type: ignore[attr-defined]
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {SCHEMA_NAME}"))
            for statement in DDL:
                await conn.execute(text(statement.format(s=SCHEMA_NAME)))
        _prepared = True
    else:
        joined = ", ".join(f"{SCHEMA_NAME}.{t}" for t in TABLES)
        async with engine.begin() as conn:  # type: ignore[attr-defined]
            await conn.execute(text(f"TRUNCATE {joined} RESTART IDENTITY CASCADE"))
    return SCHEMA_NAME


@pytest.fixture
async def sessions(require_postgres: str, schema: str) -> object:
    """A sessionmaker over the shared engine, bound to the throwaway schema."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(
        _shared_engine(require_postgres), expire_on_commit=False, autoflush=False
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drop the throwaway schema. Synchronous, so no loop-scope conflict."""
    if not _prepared or not integration_dsn():
        return
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def drop() -> None:
        # A fresh engine: the shared one belongs to a loop that has now closed.
        engine = create_async_engine(integration_dsn(), isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(drop())
    except Exception as exc:
        print(  # noqa: T201
            f"\nWARNING: could not drop {SCHEMA_NAME}: {type(exc).__name__}. "
            f"Drop it by hand: DROP SCHEMA {SCHEMA_NAME} CASCADE;"
        )
