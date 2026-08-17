"""PostgreSQL-backed shared store. The serverless replacement for Redis.

Redis was removed deliberately. ElastiCache is the only always-on, hourly-billed
component this architecture would have had — roughly the cost of everything else
combined at low traffic — and it exists in a VPC, which with no NAT Gateway
means more networking, not less. PostgreSQL is already on the critical path,
already has a connection pool through RDS Proxy, and already scales to zero
between messages.

What genuinely needs a *shared* store is small:

* **rate limits** — a per-conversation bucket must be shared across Lambda
  containers or the effective limit is `N_containers × limit`;
* **kill switches** — an operator flipping `LLM_PAUSED` must affect warm
  containers within seconds, not on their next cold start.

Both are low-volume, and both are correctness-critical rather than
latency-critical. The token bucket is a single atomic `INSERT … ON CONFLICT DO
UPDATE` that computes the refill inside SQL, so two concurrent Lambdas cannot
both see a full bucket — the read-modify-write happens in one statement under a
row lock.

An in-process L1 still fronts this for read-mostly data (`TieredCache`), so the
hot path is unchanged; only the shared state pays a round trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutor_match_meta.cache.base import CacheStats
from tutor_match_meta.observability.context import get_logger

logger = get_logger("cache.postgres")

#: Bucket rows older than this are dead weight; the retention job sweeps them.
BUCKET_TTL_SECONDS = 3_600


class PostgresKeyValueStore:
    """`Cache` implementation over a single key/value table.

    Fails **open** on read (returns a miss) and swallows write errors, matching
    the Redis implementation it replaces: a store blip must degrade the service,
    not take it down. The one exception is the rate limiter, which fails
    *closed* — see `PostgresRateLimitStore.consume`.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, schema: str) -> None:
        self._sessions = sessions
        self._table = f"{schema}.kv_entry" if schema else "kv_entry"
        self.stats = CacheStats()
        self.failures = 0

    async def get(self, key: str) -> str | None:
        try:
            async with self._sessions() as session:
                row = await session.scalar(
                    text(
                        f"SELECT value FROM {self._table} "  # noqa: S608 - fixed table name
                        "WHERE key = :key AND expires_at > now()"
                    ),
                    {"key": key},
                )
        except Exception:
            self.failures += 1
            logger.warning("kv get failed")
            return None
        if row is None:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return str(row)

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        try:
            async with self._sessions() as session, session.begin():
                await session.execute(
                    text(
                        f"INSERT INTO {self._table} (key, value, expires_at) "  # noqa: S608
                        "VALUES (:key, :value, now() + make_interval(secs => :ttl)) "
                        "ON CONFLICT (key) DO UPDATE SET "
                        "value = EXCLUDED.value, expires_at = EXCLUDED.expires_at"
                    ),
                    {"key": key, "value": value, "ttl": ttl_seconds},
                )
        except Exception:
            self.failures += 1
            logger.warning("kv set failed")

    async def delete(self, key: str) -> None:
        try:
            async with self._sessions() as session, session.begin():
                await session.execute(
                    text(f"DELETE FROM {self._table} WHERE key = :key"),  # noqa: S608
                    {"key": key},
                )
        except Exception:
            self.failures += 1

    async def clear_prefix(self, prefix: str) -> int:
        try:
            async with self._sessions() as session, session.begin():
                result = await session.execute(
                    text(f"DELETE FROM {self._table} WHERE key LIKE :pattern"),  # noqa: S608
                    {"pattern": f"{prefix}%"},
                )
                return int(getattr(result, "rowcount", 0) or 0)
        except Exception:
            self.failures += 1
            return 0


@dataclass(frozen=True, slots=True)
class BucketState:
    tokens: float
    allowed: bool
    retry_after_seconds: float


class PostgresRateLimitStore:
    """Atomic token bucket in one SQL statement.

    The whole refill-check-decrement cycle happens inside a single
    `INSERT … ON CONFLICT DO UPDATE`, so concurrent Lambdas serialise on the row
    lock rather than racing a read-modify-write. Doing this in Python would let
    two containers both read a full bucket and both allow the request.

    Fails **closed**: if the store is unreachable we cannot prove the caller is
    under their limit, and a rate limiter that fails open under database stress
    is precisely a rate limiter that does not work when it is needed.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, schema: str) -> None:
        self._sessions = sessions
        self._table = f"{schema}.rate_bucket" if schema else "rate_bucket"
        #: Non-zero means the limiter is refusing traffic it cannot evaluate.
        #: Worth an alarm: it is indistinguishable from working, from outside.
        self.failures = 0

    #: `LEAST(capacity, stored + elapsed * refill)` — the bucket level *now*,
    #: computed inside SQL so two containers cannot both read a stale level.
    #:
    #: **Every parameter is explicitly cast.** PostgreSQL types a bare
    #: placeholder as `unknown`, and an expression built only from placeholders
    #: — `:capacity - :cost` — has nothing to resolve against, so the server
    #: answers "could not determine data type of parameter". The driver raises,
    #: `consume` catches it, and the limiter fails **closed**: it refuses every
    #: request, on every key, forever. That is a total outage wearing a working
    #: limiter's clothes, and it is invisible to any test that does not execute
    #: the statement against a real server — which is exactly how it survived
    #: until the integration run.
    _REFILLED = (
        "LEAST(CAST(:capacity AS double precision),"
        " b.tokens + EXTRACT(EPOCH FROM (now() - b.updated_at))"
        " * CAST(:refill AS double precision))"
    )
    #: Class attributes rather than locals so the SQL-injection AST check can
    #: prove they are constant — `tests/security/test_sql_injection.py` allows
    #: only `self.<attr>` or a module constant inside an interpolated query,
    #: and it was right to reject the local variables these replaced.
    _COST = "CAST(:cost AS double precision)"
    _TTL = "make_interval(secs => CAST(:ttl AS double precision))"

    async def consume(
        self, key: str, *, capacity: float, refill_per_second: float, cost: float = 1.0
    ) -> BucketState:
        if cost > capacity:
            # A request larger than the bucket can ever hold. Refusing is the
            # only honest answer; the old code inserted a negative level and
            # then reported it as allowed.
            return BucketState(tokens=0.0, allowed=False, retry_after_seconds=60.0)

        # The decrement is expressed as a *conditional* ON CONFLICT UPDATE. When
        # the bucket is short, the WHERE excludes the row, the statement returns
        # zero rows, and that absence IS the refusal.
        #
        # The previous form always wrote a non-negative level and then returned
        # `tokens >= 0` as `allowed`, which is a tautology — the limiter allowed
        # every request it ever saw. There is no in-process test that catches
        # that, because the in-memory store is a separate implementation; see
        # tests/unit/test_rate_limit_sql.py, which asserts against the SQL text.
        consume = text(
            f"""
            INSERT INTO {self._table} AS b (key, tokens, updated_at, expires_at)
            VALUES (
                :key,
                CAST(:capacity AS double precision) - {self._COST},
                now(),
                now() + {self._TTL}
            )
            ON CONFLICT (key) DO UPDATE SET
                tokens      = {self._REFILLED} - {self._COST},
                updated_at  = now(),
                expires_at  = now() + {self._TTL}
            WHERE {self._REFILLED} >= {self._COST}
            RETURNING tokens
            """  # noqa: S608 - fixed table name, every value is bound
        )
        # Deny path: refresh the TTL so a bucket under sustained refusal is not
        # swept by the retention job (which would silently reset it to full),
        # and read back the level to compute an honest Retry-After.
        touch = text(
            f"""
            UPDATE {self._table} AS b
               SET expires_at = now() + {self._TTL}
             WHERE key = :key
            RETURNING {self._REFILLED} AS level
            """  # noqa: S608 - fixed table name, every value is bound
        )
        params = {
            "key": key,
            "capacity": capacity,
            "cost": cost,
            "refill": refill_per_second,
            "ttl": BUCKET_TTL_SECONDS,
        }
        try:
            async with self._sessions() as session, session.begin():
                granted = (await session.execute(consume, params)).one_or_none()
                if granted is not None:
                    return BucketState(
                        tokens=float(granted.tokens), allowed=True, retry_after_seconds=0.0
                    )
                refused = (await session.execute(touch, params)).one_or_none()
        except Exception as exc:
            # The error class matters and used to be discarded. Failing closed
            # looks identical whether the database is unreachable or the
            # statement is malformed — and the second is a total, silent outage
            # that no amount of staring at connectivity graphs would explain.
            # `ProgrammingError` here means the SQL is wrong, not the network.
            self.failures += 1
            logger.error(
                "rate limit store unavailable; failing closed",
                extra={"tmm_error": type(exc).__name__, "tmm_detail": str(exc)[:200]},
            )
            return BucketState(tokens=0.0, allowed=False, retry_after_seconds=1.0)

        level = float(refused.level) if refused is not None else 0.0
        deficit = max(cost - level, 0.0)
        retry = deficit / refill_per_second if refill_per_second > 0 else 60.0
        return BucketState(tokens=level, allowed=False, retry_after_seconds=retry)

    async def peek(self, key: str) -> float | None:
        """Current tokens without consuming. Diagnostics only."""
        async with self._sessions() as session:
            value = await session.scalar(
                text(f"SELECT tokens FROM {self._table} WHERE key = :key"),  # noqa: S608
                {"key": key},
            )
        return float(value) if value is not None else None


class PostgresKillSwitchStore:
    """Operator kill switches, readable by every container within seconds.

    Cached in-process for `ttl_seconds` so a flag read does not add a database
    round trip to every turn. The TTL is the blast-radius knob: at 10 seconds an
    operator pausing the LLM sees it take effect across all warm containers
    within 10 seconds, which is fast enough for an incident and cheap enough for
    steady state.

    Fails **safe by flag**: if the store is unreachable we keep serving the last
    known state rather than assuming everything is paused (which would be an
    outage) or assuming nothing is paused (which would ignore an operator).
    """

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, schema: str, ttl_seconds: int = 10
    ) -> None:
        self._sessions = sessions
        self._table = f"{schema}.kill_switch" if schema else "kill_switch"
        self._ttl = ttl_seconds
        self._cache: dict[str, bool] = {}
        self._fetched_at: datetime | None = None

    async def paused(self, switch: str) -> bool:
        await self._refresh()
        return self._cache.get(switch, False)

    async def all_paused(self) -> dict[str, bool]:
        await self._refresh()
        return dict(self._cache)

    async def set(self, switch: str, *, paused: bool, actor: str, reason: str) -> None:
        """Flip a switch. Always records who and why — an unexplained pause in
        an incident is nearly as bad as no pause at all."""
        async with self._sessions() as session, session.begin():
            await session.execute(
                text(
                    f"INSERT INTO {self._table} "  # noqa: S608
                    "(name, paused, actor, reason, changed_at) "
                    "VALUES (:name, :paused, :actor, :reason, now()) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "paused = EXCLUDED.paused, actor = EXCLUDED.actor, "
                    "reason = EXCLUDED.reason, changed_at = now()"
                ),
                {"name": switch, "paused": paused, "actor": actor[:120], "reason": reason[:240]},
            )
        self._fetched_at = None  # force a re-read on the next call

    async def _refresh(self) -> None:
        now = datetime.now(UTC)
        if self._fetched_at and now - self._fetched_at < timedelta(seconds=self._ttl):
            return
        try:
            async with self._sessions() as session:
                rows = (
                    await session.execute(text(f"SELECT name, paused FROM {self._table}"))  # noqa: S608
                ).all()
            self._cache = {str(r.name): bool(r.paused) for r in rows}
            self._fetched_at = now
        except Exception:
            # Keep the last known state. Losing the store must not silently
            # un-pause something an operator paused during an incident.
            logger.warning("kill switch read failed; using last known state")
            self._fetched_at = now


def build_shared_stores(
    sessions: async_sessionmaker[AsyncSession], *, schema: str
) -> tuple[PostgresKeyValueStore, PostgresRateLimitStore, PostgresKillSwitchStore]:
    return (
        PostgresKeyValueStore(sessions, schema=schema),
        PostgresRateLimitStore(sessions, schema=schema),
        PostgresKillSwitchStore(sessions, schema=schema),
    )


__all__: list[str] = [
    "BUCKET_TTL_SECONDS",
    "BucketState",
    "PostgresKeyValueStore",
    "PostgresKillSwitchStore",
    "PostgresRateLimitStore",
    "build_shared_stores",
]
