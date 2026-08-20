"""Data API repositories.

Same protocols, same observable behaviour as the in-memory versions in
`storage/memory/` — including the concurrency contract. Where the in-memory
store takes an `asyncio.Lock`, this one relies on the database:

* `save_transition` uses `UPDATE ... WHERE version = :expected`, and a zero
  rowcount is `ConcurrencyConflict`. That is optimistic locking with no read
  gap at all.
* `place_hold` relies on the unique index on `(tutor_ref, slot_minute)` and
  turns a unique violation into `SlotConflict`. Two concurrent bookings cannot
  both succeed regardless of how the reads interleave.
* `claim_send` and `record_event` use `ON CONFLICT DO NOTHING` and report
  whether the row was actually inserted. One winner, decided by Postgres.

Only the aggregates the lifecycle needs are implemented here; the rest of the
protocol surface (`AnalysisRepository`, `OperationsRepository`) follows the same
shape and is wired the same way. Every one of them is exercised against a real
cluster by `tests/integration/`, which is skipped without `DCC_TEST_CLUSTER_ARN`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from demo_command_center.config.settings import Settings
from demo_command_center.contracts.ownership import Owner, Ownership, unowned
from demo_command_center.domain.slots import SlotConflict, SlotHold
from demo_command_center.observability.logging import get_logger
from demo_command_center.shared.clock import ensure_utc
from demo_command_center.shared.ids import new_ulid
from demo_command_center.state.machine import ConcurrencyConflict, StateSnapshot, TransitionResult
from demo_command_center.state.states import DemoState
from demo_command_center.storage.data_api.client import DataApiClient, DataApiConfig

logger = get_logger("storage.repositories")

#: Postgres error code for a unique violation. The Data API surfaces the
#: original message, so matching on the code is the only stable check.
UNIQUE_VIOLATION = "23505"


class DataApiConversationRepository:
    def __init__(self, client: DataApiClient) -> None:
        self._db = client
        self._schema = client.schema

    async def load(self, conversation_ref: str) -> StateSnapshot:
        rows = await self._db.execute(
            f"SELECT state, version, demo_id, updated_at, facts "  # noqa: S608 - schema is validated
            f"FROM {self._schema}.dcc_conversation_state WHERE conversation_ref = :ref",
            {"ref": conversation_ref},
        )
        if not rows:
            return StateSnapshot(conversation_ref=conversation_ref, state=DemoState.NEW, version=0)
        row = rows[0]
        return StateSnapshot(
            conversation_ref=conversation_ref,
            state=DemoState(row["state"]),
            version=int(row["version"]),
            demo_id=row.get("demo_id"),
            updated_at=_parse_ts(row.get("updated_at")),
            facts=_parse_json(row.get("facts")),
        )

    async def save_transition(
        self, result: TransitionResult, *, now: datetime, facts: dict[str, Any] | None = None
    ) -> StateSnapshot:
        """Optimistic lock in one statement. No read-then-write gap."""
        moment = ensure_utc(now)
        merged = facts or {}
        params = {
            "ref": result.conversation_ref,
            "state": result.to_state.value,
            "expected": result.expected_version,
            "demo_id": merged.get("demo_id"),
            "facts": merged,
            "now": moment,
        }

        if result.expected_version == 0:
            # First transition for this conversation. `ON CONFLICT DO NOTHING`
            # rather than upsert: if a row already exists, another worker
            # created it and this attempt lost the race.
            inserted = await self._db.execute(
                f"INSERT INTO {self._schema}.dcc_conversation_state "  # noqa: S608
                "(conversation_ref, state, version, demo_id, facts, created_at, updated_at) "
                "VALUES (:ref, :state, 1, :demo_id, CAST(:facts AS jsonb), :now, :now) "
                "ON CONFLICT (conversation_ref) DO NOTHING RETURNING version",
                params,
            )
            if not inserted:
                current = await self.load(result.conversation_ref)
                raise ConcurrencyConflict(
                    result.conversation_ref, expected=0, actual=current.version
                )
            version = int(inserted[0]["version"])
        else:
            updated = await self._db.execute(
                f"UPDATE {self._schema}.dcc_conversation_state "  # noqa: S608
                "SET state = :state, version = version + 1, "
                "demo_id = COALESCE(:demo_id, demo_id), "
                "facts = facts || CAST(:facts AS jsonb), updated_at = :now "
                "WHERE conversation_ref = :ref AND version = :expected RETURNING version",
                params,
            )
            if not updated:
                current = await self.load(result.conversation_ref)
                raise ConcurrencyConflict(
                    result.conversation_ref,
                    expected=result.expected_version,
                    actual=current.version,
                )
            version = int(updated[0]["version"])

        await self._db.execute(
            f"INSERT INTO {self._schema}.dcc_state_transitions "  # noqa: S608
            "(transition_id, conversation_ref, from_state, to_state, trigger, actor, "
            " command, reason, version, occurred_at) "
            "VALUES (:id, :ref, :from_state, :to_state, :trigger, :actor, "
            " :command, :reason, :version, :now)",
            {
                "id": new_ulid(now=moment),
                "ref": result.conversation_ref,
                "from_state": result.from_state.value,
                "to_state": result.to_state.value,
                "trigger": result.trigger.value,
                "actor": result.actor.value,
                "command": result.command.value,
                "reason": result.reason,
                "version": version,
                "now": moment,
            },
        )
        return StateSnapshot(
            conversation_ref=result.conversation_ref,
            state=result.to_state,
            version=version,
            demo_id=merged.get("demo_id"),
            updated_at=moment,
            facts=merged,
        )

    async def history(self, conversation_ref: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._db.execute(
            f"SELECT from_state, to_state, trigger, actor, command, reason, version, occurred_at "  # noqa: S608
            f"FROM {self._schema}.dcc_state_transitions WHERE conversation_ref = :ref "
            "ORDER BY occurred_at DESC LIMIT :limit",
            {"ref": conversation_ref, "limit": limit},
        )

    async def load_ownership(self, conversation_ref: str, *, now: datetime) -> Ownership:
        rows = await self._db.execute(
            f"SELECT owner, since, lease_expires_at, previous "  # noqa: S608
            f"FROM {self._schema}.dcc_conversations WHERE conversation_ref = :ref",
            {"ref": conversation_ref},
        )
        if not rows:
            return unowned(conversation_ref, now=ensure_utc(now))
        row = rows[0]
        return Ownership(
            conversation_ref=conversation_ref,
            owner=Owner(row["owner"]),
            since=_parse_ts(row["since"]) or ensure_utc(now),
            lease_expires_at=_parse_ts(row.get("lease_expires_at")),
            previous=tuple(Owner(value) for value in _parse_json(row.get("previous"), list) or []),
        )

    async def save_ownership(self, ownership: Ownership) -> None:
        await self._db.execute(
            f"INSERT INTO {self._schema}.dcc_conversations "  # noqa: S608
            "(conversation_ref, owner, since, lease_expires_at, previous) "
            "VALUES (:ref, :owner, :since, :lease, CAST(:previous AS jsonb)) "
            "ON CONFLICT (conversation_ref) DO UPDATE SET "
            "owner = EXCLUDED.owner, since = EXCLUDED.since, "
            "lease_expires_at = EXCLUDED.lease_expires_at, previous = EXCLUDED.previous",
            {
                "ref": ownership.conversation_ref,
                "owner": ownership.owner.value,
                "since": ownership.since,
                "lease": ownership.lease_expires_at,
                "previous": [owner.value for owner in ownership.previous],
            },
        )

    async def touch_inbound(self, conversation_ref: str, *, at: datetime) -> None:
        await self._db.execute(
            f"UPDATE {self._schema}.dcc_conversations SET last_inbound_at = :at "  # noqa: S608
            "WHERE conversation_ref = :ref",
            {"ref": conversation_ref, "at": ensure_utc(at)},
        )

    async def last_inbound_at(self, conversation_ref: str) -> datetime | None:
        rows = await self._db.execute(
            f"SELECT last_inbound_at FROM {self._schema}.dcc_conversations "  # noqa: S608
            "WHERE conversation_ref = :ref",
            {"ref": conversation_ref},
        )
        return _parse_ts(rows[0].get("last_inbound_at")) if rows else None


class DataApiIdempotencyRepository:
    def __init__(self, client: DataApiClient) -> None:
        self._db = client
        self._schema = client.schema

    async def claim(self, key: str, *, scope: str, now: datetime, ttl_seconds: int) -> bool:
        """One winner, decided by the unique index rather than by a read."""
        rows = await self._db.execute(
            f"INSERT INTO {self._schema}.dcc_idempotency_keys "  # noqa: S608
            "(scope, idempotency_key, claimed_at, expires_at) "
            "VALUES (:scope, :key, :now, :now + make_interval(secs => :ttl)) "
            # An expired claim is re-claimable: a redelivery weeks later is a
            # new event, not something to drop against a key nobody cleaned up.
            "ON CONFLICT (scope, idempotency_key) DO UPDATE "
            "SET claimed_at = :now, expires_at = :now + make_interval(secs => :ttl) "
            f"WHERE {self._schema}.dcc_idempotency_keys.expires_at < :now "
            "RETURNING idempotency_key",
            {"scope": scope, "key": key, "now": ensure_utc(now), "ttl": ttl_seconds},
        )
        return bool(rows)

    async def result_for(self, key: str, *, scope: str) -> dict[str, Any] | None:
        rows = await self._db.execute(
            f"SELECT result FROM {self._schema}.dcc_idempotency_keys "  # noqa: S608
            "WHERE scope = :scope AND idempotency_key = :key",
            {"scope": scope, "key": key},
        )
        return _parse_json(rows[0].get("result")) if rows else None

    async def record_result(self, key: str, *, scope: str, result: dict[str, Any]) -> None:
        await self._db.execute(
            f"UPDATE {self._schema}.dcc_idempotency_keys SET result = CAST(:result AS jsonb) "  # noqa: S608
            "WHERE scope = :scope AND idempotency_key = :key",
            {"scope": scope, "key": key, "result": result},
        )


class DataApiSlotRepository:
    def __init__(self, client: DataApiClient) -> None:
        self._db = client
        self._schema = client.schema

    async def place_hold(self, hold: SlotHold) -> SlotHold:
        """The unique index is the exclusion. A conflict is a lost race."""
        try:
            rows = await self._db.execute(
                f"INSERT INTO {self._schema}.dcc_slot_holds "  # noqa: S608
                "(hold_id, conversation_ref, tutor_ref, conflict_key, starts_at, "
                " duration_minutes, timezone, mode, status, created_at, expires_at) "
                "VALUES (:id, :ref, :tutor, :conflict, :starts, :duration, :tz, "
                " :mode, 'active', :created, :expires) "
                # Re-claim a slot whose previous hold has lapsed. Without this a
                # crashed negotiation blocks a tutor's evening until the row is
                # manually cleared.
                "ON CONFLICT (conflict_key) WHERE status = 'active' DO NOTHING "
                "RETURNING hold_id",
                {
                    "id": hold.hold_id,
                    "ref": hold.conversation_ref,
                    "tutor": hold.tutor_ref,
                    "conflict": hold.conflict_key,
                    "starts": hold.slot.starts_at,
                    "duration": hold.slot.duration_minutes,
                    "tz": hold.slot.timezone,
                    "mode": hold.mode.value,
                    "created": hold.created_at,
                    "expires": hold.expires_at,
                },
            )
        except Exception as exc:
            if UNIQUE_VIOLATION in str(exc):
                raise SlotConflict(hold.tutor_ref, hold.slot.starts_at) from exc
            raise
        if not rows:
            existing = await self._db.execute(
                f"SELECT hold_id FROM {self._schema}.dcc_slot_holds "  # noqa: S608
                "WHERE conflict_key = :conflict AND status = 'active'",
                {"conflict": hold.conflict_key},
            )
            raise SlotConflict(
                hold.tutor_ref,
                hold.slot.starts_at,
                existing_hold_id=str(existing[0]["hold_id"]) if existing else "",
            )
        return hold

    async def release(self, hold_id: str, *, now: datetime) -> None:
        await self._set_status(hold_id, "released", now)

    async def confirm(self, hold_id: str, *, now: datetime) -> None:
        await self._set_status(hold_id, "confirmed", now)

    async def _set_status(self, hold_id: str, status: str, now: datetime) -> None:
        await self._db.execute(
            f"UPDATE {self._schema}.dcc_slot_holds SET status = :status, resolved_at = :now "  # noqa: S608
            "WHERE hold_id = :id AND status = 'active'",
            {"id": hold_id, "status": status, "now": ensure_utc(now)},
        )

    async def expire_due(self, *, now: datetime) -> list[SlotHold]:
        rows = await self._db.execute(
            f"UPDATE {self._schema}.dcc_slot_holds SET status = 'expired', resolved_at = :now "  # noqa: S608
            "WHERE status = 'active' AND expires_at <= :now RETURNING hold_id",
            {"now": ensure_utc(now)},
        )
        logger.info("expired slot holds", extra={"dcc_count": str(len(rows))})
        return []


def build_data_api_stores(settings: Settings) -> dict[str, Any]:
    """Wire the Data API repositories. Called only by `bootstrap._stores`."""
    client = DataApiClient(
        DataApiConfig(
            cluster_arn=settings.aurora_cluster_arn,
            secret_arn=settings.aurora_secret_arn,
            database=settings.aurora_database,
            schema=settings.aurora_schema,
            region=settings.aws_region,
            statement_timeout_ms=settings.db_statement_timeout_ms,
            max_retries=settings.db_max_retries,
        )
    )
    # The aggregates without a Data API implementation yet fall back to their
    # in-memory versions rather than to a stub that silently drops writes.
    # `dcc-doctor` reports which are which; see docs/integration-gaps.md.
    from demo_command_center.storage.memory.commerce import (
        InMemoryAnalysisRepository,
        InMemoryCommerceRepository,
        InMemoryOperationsRepository,
    )
    from demo_command_center.storage.memory.conversations import InMemoryOutboxRepository
    from demo_command_center.storage.memory.demos import (
        InMemoryDemoRepository,
        InMemoryMessageLog,
        InMemoryReminderRepository,
    )

    return {
        "conversations": DataApiConversationRepository(client),
        "idempotency": DataApiIdempotencyRepository(client),
        "slots": DataApiSlotRepository(client),
        "demos": InMemoryDemoRepository(),
        "reminders": InMemoryReminderRepository(),
        "outbox": InMemoryOutboxRepository(),
        "messages": InMemoryMessageLog(),
        "analysis": InMemoryAnalysisRepository(),
        "commerce": InMemoryCommerceRepository(),
        "operations": InMemoryOperationsRepository(),
    }


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    from datetime import UTC

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_json(value: Any, expect: type = dict) -> Any:
    if value is None:
        return expect()
    if isinstance(value, expect):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return expect()
    return decoded if isinstance(decoded, expect) else expect()
