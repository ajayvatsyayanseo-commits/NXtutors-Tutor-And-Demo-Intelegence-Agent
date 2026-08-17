"""PostgreSQL implementations of the repository ports.

Two things here are load-bearing and worth reading before changing:

* **The optimistic lock is a conditional UPDATE**, not a read-then-write. The
  `WHERE lock_version = :expected` clause is what makes concurrent turns safe;
  checking in Python first would leave a window between the check and the write.
* **Idempotency uses `ON CONFLICT DO NOTHING` and inspects `rowcount`.** The
  database decides who won the race, so two Lambdas processing the same
  redelivery cannot both proceed.

Connections go through RDS Proxy, so the engine pool is deliberately tiny —
Lambda concurrency multiplies it, and the proxy does the real multiplexing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tutor_match_meta.config.settings import Settings
from tutor_match_meta.contracts.common import Freshness, TuitionMode
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.contracts.schedule import TimeWindow, Weekday, WeeklySchedule
from tutor_match_meta.contracts.scoring import MatchDecisionV1
from tutor_match_meta.contracts.tutor import (
    FeeBand,
    GeoPoint,
    ReviewAggregate,
    TutorCandidate,
    TutorCapabilities,
)
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.orchestration.orchestrator import freshness_for
from tutor_match_meta.repositories.models import (
    ConversationStateRow,
    IdempotencyRecord,
    MatchDecisionRow,
    OutboxEvent,
    RequirementRow,
    TutorAvailability,
    TutorProjection,
)
from tutor_match_meta.repositories.ports import CandidateQuery, OutboxMessage
from tutor_match_meta.state.machine import (
    ConversationSnapshot,
    ConversationState,
    OptimisticLockError,
)

logger = get_logger("postgres")

MINUTES_PER_DAY = 24 * 60


def create_engine(settings: Settings) -> AsyncEngine:
    """Engine sized for Lambda behind RDS Proxy.

    `pool_size=1` per container is intentional: with reserved concurrency of 20
    that is 20 connections, which the proxy multiplexes down. A larger pool per
    container multiplies straight into the database's connection limit.
    """
    server_settings = {
        "application_name": f"tutor-match-meta-{settings.environment.value}",
        # A runaway query must not hold a proxy connection open forever.
        "statement_timeout": str(settings.postgres_statement_timeout_ms),
    }
    if settings.postgres_schema and settings.postgres_schema != "public":
        # Anything unqualified resolves inside our schema, so a stray query can
        # never touch another service's tables in the shared database.
        server_settings["search_path"] = f"{settings.postgres_schema},public"

    connect_args: dict[str, Any] = {"server_settings": server_settings}
    # asyncpg speaks `ssl`, not libpq's `sslmode`. Only inject it when the DSN
    # is silent: SQLAlchemy lets connect_args overwrite the URL, so setting it
    # unconditionally would silently downgrade an explicit `?ssl=verify-full`
    # to plain `require`.
    if settings.postgres_require_tls and "ssl" not in settings.postgres_dsn:
        connect_args["ssl"] = "require"

    return create_async_engine(
        settings.postgres_dsn,
        pool_size=settings.postgres_pool_size,
        max_overflow=settings.postgres_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
    )


def _escape_like(term: str) -> str:
    """Neutralise LIKE metacharacters so a term is matched literally.

    PostgreSQL treats `%` and `_` as wildcards inside LIKE, and backslash as the
    escape character. A tutor searching for "100%" — or a caller probing with
    "%" — would otherwise match every row in the table.
    """
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


class PostgresTutorRepository:
    """Reads the search projection. Structured filters only — never free text."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, fresh_hours: int, aging_hours: int
    ) -> None:
        self._sessions = sessions
        self._fresh_hours = fresh_hours
        self._aging_hours = aging_hours

    async def search(self, query: CandidateQuery) -> list[TutorCandidate]:
        now = datetime.now(UTC)
        statement = select(TutorProjection)

        if query.public_ref:
            statement = statement.where(TutorProjection.public_ref == query.public_ref)
        else:
            # Freshness is a WHERE clause, not a post-filter: a stale row must
            # never even enter the pool (docs/assumptions.md A16).
            statement = statement.where(
                TutorProjection.synced_at >= now - timedelta(hours=self._aging_hours)
            )
            if query.city_aliases or query.pincode:
                conditions: list[Any] = []
                if query.city_aliases:
                    conditions.append(
                        text("LOWER(tutor_projection.city) = ANY(:city_aliases)").bindparams(
                            city_aliases=list(query.city_aliases)
                        )
                    )
                if query.pincode:
                    conditions.append(TutorProjection.pincode == query.pincode)
                statement = statement.where(or_(*conditions))
            if query.name_terms:
                # Every term must appear in the name, each bound as a parameter.
                # `LIKE :term` with the wildcards added in Python keeps the
                # pattern a *value*, so a name containing % or _ is matched
                # literally rather than becoming a wildcard of its own — and
                # there is still no path by which caller text becomes SQL.
                for position, term in enumerate(query.name_terms[:4]):
                    statement = statement.where(
                        text(f"LOWER(tutor_projection.name) LIKE :name_{position}").bindparams(
                            **{f"name_{position}": f"%{_escape_like(term)}%"}
                        )
                    )
            if query.gender:
                statement = statement.where(
                    text("LOWER(tutor_projection.gender) = :gender").bindparams(gender=query.gender)
                )
            if query.max_fee is not None:
                # Generous: the negotiation hard filter applies the policy ratio.
                statement = statement.where(
                    (TutorProjection.fee_min.is_(None))
                    | (TutorProjection.fee_min <= query.max_fee * 2)
                )

        statement = statement.order_by(
            TutorProjection.review_count.desc(),
            TutorProjection.rating_avg.desc().nulls_last(),
            TutorProjection.tutor_id,
        ).limit(query.limit * 2 if not query.public_ref else 1)

        async with self._sessions() as session:
            rows = list((await session.scalars(statement)).all())
            availability = await self._availability({row.tutor_id for row in rows}, session)

        candidates = [self._to_candidate(row, availability.get(row.tutor_id), now) for row in rows]

        # Subject and class matching stay in Python: both live across two source
        # schemas as free text, and the domain predicates (umbrella coverage,
        # class ranges) are not expressible in a portable SQL clause.
        from tutor_match_meta.domain import academics, subjects

        if query.subjects:
            candidates = [
                c
                for c in candidates
                if not c.capabilities.subjects
                or any(
                    subjects.matches_any(want, list(c.capabilities.subjects))
                    for want in query.subjects
                )
            ]
        if query.class_labels:
            candidates = [
                c
                for c in candidates
                if not c.capabilities.classes
                or academics.teaches_class(query.class_labels[0], list(c.capabilities.classes))
            ]
        if query.mode is not None:
            candidates = [c for c in candidates if c.supports_mode(query.mode)]

        return candidates[: query.limit]

    async def _availability(
        self, tutor_ids: set[str], session: AsyncSession
    ) -> dict[str, WeeklySchedule]:
        if not tutor_ids:
            return {}
        rows = list(
            (
                await session.scalars(
                    select(TutorAvailability).where(TutorAvailability.tutor_id.in_(tutor_ids))
                )
            ).all()
        )
        grouped: dict[str, list[TimeWindow]] = {}
        zones: dict[str, str] = {}
        for row in rows:
            start_day = row.start_minute // 60, row.start_minute % 60
            end_total = row.end_minute
            end = (0, 0) if end_total == MINUTES_PER_DAY else (end_total // 60, end_total % 60)
            from datetime import time

            grouped.setdefault(row.tutor_id, []).append(
                TimeWindow(
                    weekday=Weekday(row.weekday),
                    start=time(*start_day),
                    end=time(*end),
                )
            )
            zones[row.tutor_id] = row.timezone
        return {
            tutor_id: WeeklySchedule(timezone=zones[tutor_id], windows=tuple(windows))
            for tutor_id, windows in grouped.items()
        }

    def _to_candidate(
        self, row: TutorProjection, availability: WeeklySchedule | None, now: datetime
    ) -> TutorCandidate:
        geo = None
        if row.latitude is not None and row.longitude is not None:
            geo = GeoPoint(
                latitude=row.latitude,
                longitude=row.longitude,
                granularity=row.geo_granularity or "pincode",
                resolved_at=row.updated_at,
            )
        return TutorCandidate(
            tutor_id=row.tutor_id,
            public_ref=row.public_ref,
            name=row.name,
            gender=row.gender,
            avatar_url=row.avatar_url,
            city=row.city,
            locality=row.locality,
            district=row.district,
            state=row.state,
            pincode=row.pincode,
            geo=geo,
            travel_radius_km=row.travel_radius_km,
            capabilities=TutorCapabilities(
                subjects=tuple(row.subjects),
                boards=tuple(row.boards),
                classes=tuple(row.classes),
                modes=tuple(TuitionMode(m) for m in row.modes if m in set(TuitionMode)),
            ),
            experience_years=row.experience_years,
            education=row.education,
            profile_summary=row.profile_summary,
            fee=FeeBand(minimum=row.fee_min, maximum=row.fee_max, label=row.fee_label),
            reviews=ReviewAggregate(
                count=row.review_count,
                rating_avg=row.rating_avg,
                expertise_avg=row.expertise_avg,
                patience_avg=row.patience_avg,
                reliability_avg=row.reliability_avg,
                communication_avg=row.communication_avg,
                latest_review_at=row.latest_review_at,
            ),
            availability=availability,
            active_students=row.active_students,
            source_updated_at=row.source_updated_at,
            synced_at=row.synced_at,
            freshness=freshness_for(
                row.synced_at,
                now=now,
                fresh_hours=self._fresh_hours,
                aging_hours=self._aging_hours,
            ),
            projection_version=row.projection_version,
        )

    async def get(self, tutor_id: str) -> TutorCandidate | None:
        async with self._sessions() as session:
            row = await session.get(TutorProjection, tutor_id)
            if row is None:
                return None
            availability = await self._availability({tutor_id}, session)
        return self._to_candidate(row, availability.get(tutor_id), datetime.now(UTC))

    async def get_by_public_ref(self, public_ref: str) -> TutorCandidate | None:
        results = await self.search(CandidateQuery(limit=1, public_ref=public_ref))
        return results[0] if results else None


class PostgresConversationStore:
    """Conversation state under a conditional-UPDATE optimistic lock."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load(self, conversation_id: str) -> ConversationSnapshot:
        async with self._sessions() as session:
            row = await session.get(ConversationStateRow, conversation_id)
        if row is None:
            return ConversationSnapshot(conversation_id=conversation_id)
        return ConversationSnapshot(
            conversation_id=row.conversation_id,
            state=ConversationState(row.state),
            fsm_version=row.fsm_version,
            lock_version=row.lock_version,
            turn_count=row.turn_count,
            updated_at=row.updated_at,
        )

    async def save(self, snapshot: ConversationSnapshot, *, expected_version: int) -> None:
        history = [
            {
                "state_before": record.state_before.value,
                "state_after": record.state_after.value,
                "trigger": record.trigger.value,
                "at": record.at.isoformat(),
                "trace_id": record.trace_id,
            }
            for record in snapshot.history[-20:]
        ]
        async with self._sessions() as session, session.begin():
            if expected_version == 0:
                # First write. ON CONFLICT DO NOTHING makes the insert lose
                # cleanly if another worker created the row first.
                result = await session.execute(
                    pg_insert(ConversationStateRow)
                    .values(
                        conversation_id=snapshot.conversation_id,
                        state=snapshot.state.value,
                        fsm_version=snapshot.fsm_version,
                        lock_version=snapshot.lock_version,
                        turn_count=snapshot.turn_count,
                        history=history,
                    )
                    .on_conflict_do_nothing(index_elements=[ConversationStateRow.conversation_id])
                )
                if result.rowcount == 0:
                    raise OptimisticLockError(snapshot.conversation_id, expected_version, -1)
                return

            # The WHERE clause is the lock. Checking in Python would leave a
            # window between the read and the write.
            result = await session.execute(
                update(ConversationStateRow)
                .where(
                    ConversationStateRow.conversation_id == snapshot.conversation_id,
                    ConversationStateRow.lock_version == expected_version,
                )
                .values(
                    state=snapshot.state.value,
                    fsm_version=snapshot.fsm_version,
                    lock_version=snapshot.lock_version,
                    turn_count=snapshot.turn_count,
                    history=history,
                )
            )
            if result.rowcount == 0:
                raise OptimisticLockError(snapshot.conversation_id, expected_version, -1)


class PostgresRequirementStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load(self, conversation_id: str) -> MatchRequirementV1 | None:
        async with self._sessions() as session:
            row = await session.get(RequirementRow, conversation_id)
        return MatchRequirementV1.model_validate(row.payload) if row else None

    async def save(self, requirement: MatchRequirementV1) -> None:
        payload = requirement.model_dump(mode="json")
        async with self._sessions() as session, session.begin():
            await session.execute(
                pg_insert(RequirementRow)
                .values(
                    conversation_id=requirement.conversation_id,
                    schema_version=requirement.schema_version,
                    lead_id=requirement.lead_id,
                    payload=payload,
                )
                .on_conflict_do_update(
                    index_elements=[RequirementRow.conversation_id],
                    set_={"payload": payload, "lead_id": requirement.lead_id},
                )
            )


class PostgresDecisionStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(self, decision: MatchDecisionV1) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                pg_insert(MatchDecisionRow)
                .values(
                    match_session_id=decision.match_session_id,
                    conversation_id=decision.conversation_id,
                    trace_id=decision.trace_id,
                    policy_id=decision.policy_id,
                    policy_version=decision.policy_version,
                    policy_checksum=decision.policy_checksum,
                    matched=decision.matched,
                    no_match_reason=decision.no_match_reason,
                    requires_human_review=decision.requires_human_review,
                    candidate_ids=list(decision.candidate_ids),
                    rejections=[r.model_dump(mode="json") for r in decision.rejections],
                    score_snapshot=[c.model_dump(mode="json") for c in decision.scored],
                    shortlist=[e.model_dump(mode="json") for e in decision.shortlist],
                    degraded_sources=list(decision.degraded_sources),
                    oldest_source_data_at=decision.oldest_source_data_at,
                    generated_at=decision.generated_at,
                )
                .on_conflict_do_nothing(index_elements=[MatchDecisionRow.match_session_id])
            )

    async def latest(self, conversation_id: str) -> MatchDecisionV1 | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(MatchDecisionRow)
                .where(MatchDecisionRow.conversation_id == conversation_id)
                .order_by(MatchDecisionRow.generated_at.desc())
                .limit(1)
            )
        if row is None:
            return None
        return MatchDecisionV1.model_validate(
            {
                "match_session_id": row.match_session_id,
                "conversation_id": row.conversation_id,
                "trace_id": row.trace_id,
                "policy_id": row.policy_id,
                "policy_version": row.policy_version,
                "policy_checksum": row.policy_checksum,
                "candidate_ids": row.candidate_ids,
                "rejections": row.rejections,
                "scored": row.score_snapshot,
                "shortlist": row.shortlist,
                "no_match_reason": row.no_match_reason,
                "requires_human_review": row.requires_human_review,
                "degraded_sources": row.degraded_sources,
                "oldest_source_data_at": row.oldest_source_data_at,
                "generated_at": row.generated_at,
            }
        )


class PostgresIdempotencyStore:
    """The database decides who wins a redelivery race, not the application."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim(self, key: str, *, ttl_seconds: int) -> bool:
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            # Reclaim an expired key first so a genuine retry after the window
            # is not permanently blocked by its own earlier attempt.
            await session.execute(
                delete(IdempotencyRecord).where(
                    IdempotencyRecord.key == key, IdempotencyRecord.expires_at < now
                )
            )
            result = await session.execute(
                pg_insert(IdempotencyRecord)
                .values(key=key, claimed_at=now, expires_at=now + timedelta(seconds=ttl_seconds))
                .on_conflict_do_nothing(index_elements=[IdempotencyRecord.key])
            )
            return bool(result.rowcount)

    async def release(self, key: str) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.key == key))


class PostgresOutbox:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def enqueue(self, message: OutboxMessage) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                pg_insert(OutboxEvent)
                .values(
                    dedup_key=message.dedup_key,
                    kind=message.kind,
                    conversation_id=message.conversation_id,
                    trace_id=message.trace_id,
                    payload=dict(message.payload),
                    status="pending",
                    available_at=message.available_at,
                )
                .on_conflict_do_nothing(index_elements=[OutboxEvent.dedup_key])
            )

    async def claim_batch(self, *, limit: int, now: datetime) -> list[OutboxMessage]:
        """Take exclusive ownership of up to `limit` pending rows.

        The row lock and the status flip happen in **one transaction**. An
        earlier version selected `FOR UPDATE SKIP LOCKED` in an autocommit
        session and never changed `status`, so the lock was released the moment
        the session closed and the rows stayed `pending` — two overlapping relay
        invocations (the 5-minute schedule plus a manual redrive) would both
        claim the same reply and the parent received it twice.

        `claimed_at` doubles as the lease: a relay that dies mid-batch leaves
        rows in `claiming`, and `reclaim_stale` puts them back.
        """
        async with self._sessions() as session, session.begin():
            rows = list(
                (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.status == "pending",
                            (OutboxEvent.available_at.is_(None))
                            | (OutboxEvent.available_at <= now),
                        )
                        .order_by(OutboxEvent.created_at)
                        .limit(limit)
                        # Skip rows another relay already holds, rather than
                        # blocking behind them.
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not rows:
                return []
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.dedup_key.in_([r.dedup_key for r in rows]))
                .values(status="claiming", claimed_at=now)
            )
        return [
            OutboxMessage(
                kind=row.kind,
                conversation_id=row.conversation_id,
                payload=row.payload,
                dedup_key=row.dedup_key,
                trace_id=row.trace_id,
                available_at=row.available_at,
                attempts=row.attempts,
            )
            for row in rows
        ]

    async def mark_delivered(self, dedup_key: str) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.dedup_key == dedup_key)
                .values(status="delivered", delivered_at=datetime.now(UTC))
            )

    async def mark_failed(self, dedup_key: str, *, error: str, retry_at: datetime) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.dedup_key == dedup_key)
                .values(
                    attempts=OutboxEvent.attempts + 1,
                    last_error=error[:240],
                    available_at=retry_at,
                    claimed_at=None,
                    # Stop retrying after five attempts; the DLQ alarm and the
                    # runbook take over from here.
                    status=text(
                        "CASE WHEN outbox_event.attempts + 1 >= 5 THEN 'dead' ELSE 'pending' END"
                    ),
                )
            )

    async def mark_dead(self, dedup_key: str, *, error: str) -> None:
        """Terminal failure. No retry can help — alarm and let a human decide."""
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.dedup_key == dedup_key)
                .values(status="dead", last_error=error[:240], claimed_at=None)
            )

    async def reclaim_stale(self, *, older_than: datetime) -> int:
        """Return abandoned `claiming` rows to `pending`.

        A relay Lambda that times out mid-batch leaves its rows claimed. Without
        this the reply is never delivered and never alarms, because the row is
        neither pending nor dead. Called by the relay job before each batch.
        """
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.status == "claiming",
                    OutboxEvent.claimed_at.is_not(None),
                    OutboxEvent.claimed_at < older_than,
                )
                .values(status="pending", claimed_at=None)
            )
        return int(getattr(result, "rowcount", 0) or 0)


def build_sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def build_postgres_stores(
    settings: Settings, *, sessions: async_sessionmaker[AsyncSession] | None = None
) -> tuple[Any, ...]:
    """The six stores the match worker needs, in the order it expects them.

    `sessions` is injected by the composition root so every store in a
    container shares one engine. Creating one here per call is what previously
    leaked a connection pool on every request.
    """
    sessions = sessions or build_sessions(create_engine(settings))
    return (
        PostgresTutorRepository(
            sessions,
            fresh_hours=settings.projection_fresh_hours,
            aging_hours=settings.projection_aging_hours,
        ),
        PostgresConversationStore(sessions),
        PostgresRequirementStore(sessions),
        PostgresDecisionStore(sessions),
        PostgresIdempotencyStore(sessions),
        PostgresOutbox(sessions),
    )


def freshness_of(row: TutorProjection, *, now: datetime, fresh: int, aging: int) -> Freshness:
    return freshness_for(row.synced_at, now=now, fresh_hours=fresh, aging_hours=aging)


__all__ = [
    "PostgresConversationStore",
    "PostgresDecisionStore",
    "PostgresIdempotencyStore",
    "PostgresOutbox",
    "PostgresRequirementStore",
    "PostgresTutorRepository",
    "build_postgres_stores",
    "build_sessions",
    "create_engine",
]
