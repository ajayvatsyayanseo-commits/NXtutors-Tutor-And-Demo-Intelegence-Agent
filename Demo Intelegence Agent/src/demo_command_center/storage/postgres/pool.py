"""asyncpg connection pool, shared per container.

One pool per process, built lazily. A pool per request would exhaust the
database's connection limit under any real concurrency — the same failure the
Tutor Intelligence bootstrap documents at length, and the reason that service
caches its sessionmaker.

Every statement goes through `execute`/`fetch` here, which:

* set the Demo schema on the `search_path` per acquisition, so no query ever
  has to qualify a table name and no query can accidentally reach another
  schema;
* apply a statement timeout, so one pathological query cannot hold a
  connection for the whole Lambda duration;
* classify failures into the shared taxonomy rather than leaking `asyncpg`
  types upward.

`asyncpg` is an **optional** dependency (`pip install '.[postgres]'`). It is
imported inside the functions that need it so the package still loads — and
`make demo`, the tests and the Data API path still work — when it is absent.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from demo_command_center.contracts.ports import (
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)
from demo_command_center.observability.logging import get_logger

logger = get_logger("storage.postgres")

PROVIDER = "postgres"

#: A schema name is an identifier, never user input. Validated anyway, because
#: it is the one value that reaches SQL by interpolation.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

#: Postgres SQLSTATEs worth translating rather than passing through raw.
UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"
CHECK_VIOLATION = "23514"


class SchemaNameInvalid(ValueError):
    pass


def validate_schema(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise SchemaNameInvalid(f"not a valid postgres identifier: {name!r}")
    return name


def normalise_dsn(dsn: str) -> str:
    """SQLAlchemy-shaped DSN → the bare URL asyncpg wants."""
    return re.sub(r"\?.*$", "", dsn.replace("postgresql+asyncpg://", "postgresql://"))


class PostgresPool:
    """Container-lifetime pool with schema and timeout applied per connection."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str,
        min_size: int = 1,
        max_size: int = 5,
        statement_timeout_ms: int = 5_000,
        require_tls: bool = True,
    ) -> None:
        self._dsn = normalise_dsn(dsn)
        self._schema = validate_schema(schema)
        self._min = min_size
        self._max = max_size
        self._timeout_ms = statement_timeout_ms
        self._tls = "require" if require_tls else None
        self._pool: Any = None
        #: The loop `self._pool` was created on. See `pool()`.
        self._loop: Any = None

    @property
    def schema(self) -> str:
        return self._schema

    async def pool(self) -> Any:
        # Rebuilt when the running loop is not the one the pool was created on.
        #
        # A Lambda handler calls `asyncio.run()` per invocation, which creates a
        # fresh loop every time, while `build_dependencies` caches this object
        # for the life of the container. asyncpg binds its connections and their
        # futures to the loop that opened them, so a pool carried across gives
        # "got Future attached to a different loop" on the *second* invocation
        # of every warm container — the cold-start request succeeds, and each
        # one after it fails until the container is recycled.
        #
        # Closing the old pool is deliberately not attempted: its transport
        # belongs to a loop that is already closed, so `close()` would raise. The
        # sockets are released when the dead loop is collected.
        loop = asyncio.get_running_loop()
        if self._pool is not None and self._loop is loop:
            return self._pool
        if self._pool is not None:
            logger.info("event loop changed; rebuilding the postgres pool")
            self._pool = None
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover - dependency-gated path
            raise ProviderUnavailable(
                PROVIDER, "asyncpg is not installed; pip install '.[postgres]'"
            ) from exc

        async def _per_acquisition(connection: Any) -> None:
            """Session state, re-applied on every acquire.

            This MUST be `setup=`, not `init=`. asyncpg runs `init` once when a
            connection is created, then issues `RESET ALL` each time one is
            released back to the pool — which wipes `search_path`. With `init`
            the first query after pool creation resolves and every later one
            fails with `UndefinedTableError`, which is a genuinely confusing
            symptom: the schema is right, the grant is right, and the table is
            there.

            `setup` runs after the reset on every acquisition, so the
            `search_path` is always present and no repository statement has to
            qualify a table name.
            """
            await connection.execute(f"SET search_path TO {self._schema}")
            await connection.execute(f"SET statement_timeout = {self._timeout_ms}")

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min,
            max_size=self._max,
            ssl=self._tls,
            setup=_per_acquisition,
            # Server-side prepared statements are disabled: RDS Proxy and
            # PgBouncer in transaction mode both break them, and the cost of
            # re-planning these small statements is negligible.
            statement_cache_size=0,
        )
        self._loop = loop
        logger.info("postgres pool ready", extra={"dcc_schema": self._schema})
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._loop = None

    async def execute(self, sql: str, *args: Any) -> str:
        async with (await self.pool()).acquire() as connection:
            try:
                result: str = await connection.execute(sql, *args)
            except Exception as error:
                raise _translate(error) from error
            return result

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        async with (await self.pool()).acquire() as connection:
            try:
                rows = await connection.fetch(sql, *args)
            except Exception as error:
                raise _translate(error) from error
            return [dict(row) for row in rows]

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        return next(iter(row.values())) if row else None

    def transaction(self) -> Any:
        """A context manager yielding a connection inside a transaction.

        Used where two writes must land together — a state transition and its
        audit row, or an outbox insert beside the row that caused it.
        """
        return _Transaction(self)


class _Transaction:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool
        self._connection: Any = None
        self._transaction: Any = None

    async def __aenter__(self) -> Any:
        self._connection = await (await self._pool.pool()).acquire()
        self._transaction = self._connection.transaction()
        await self._transaction.start()
        return self._connection

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is None:
                await self._transaction.commit()
            else:
                await self._transaction.rollback()
        finally:
            await (await self._pool.pool()).release(self._connection)


def _translate(error: Exception) -> Exception:
    """asyncpg error → the shared taxonomy, preserving what matters.

    A unique violation becomes `ProviderRejected` with the SQLSTATE as its
    code, so `SlotRepository.place_hold` can turn exactly that into a
    `SlotConflict` without importing asyncpg.
    """
    code = getattr(error, "sqlstate", "") or ""
    name = type(error).__name__

    if code in (UNIQUE_VIOLATION, FOREIGN_KEY_VIOLATION, CHECK_VIOLATION):
        return ProviderRejected(PROVIDER, name, status_code=409, code=code)
    if "Timeout" in name or code == "57014":  # query_canceled
        return ProviderTimeout(PROVIDER, 0.0)
    if code.startswith("08") or "Connection" in name:  # connection exceptions
        return ProviderUnavailable(PROVIDER, name)
    # Everything else is unavailable rather than rejected: an unclassified
    # database error is more often a blip than a permanently bad statement.
    return ProviderUnavailable(PROVIDER, f"{name}:{code}" if code else name)
