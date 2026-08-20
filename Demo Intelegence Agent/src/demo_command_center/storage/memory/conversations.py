"""In-memory conversation state, idempotency and outbox.

Not "just for tests". These implement the *same* concurrency contract as the
Data API repositories — optimistic version checks and single-winner claims — so
the concurrency tests exercise real behaviour rather than a simplification that
happens to pass. An `asyncio.Lock` stands in for the database's row lock; the
observable contract (`ConcurrencyConflict`, `claim()` returning False to the
loser) is identical.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from demo_command_center.contracts.ownership import Ownership, unowned
from demo_command_center.shared.clock import ensure_utc
from demo_command_center.shared.ids import new_ulid
from demo_command_center.state.machine import (
    ConcurrencyConflict,
    StateSnapshot,
    TransitionResult,
)
from demo_command_center.state.states import DemoState


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._states: dict[str, StateSnapshot] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._ownership: dict[str, Ownership] = {}
        self._last_inbound: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def load(self, conversation_ref: str) -> StateSnapshot:
        return self._states.get(
            conversation_ref,
            StateSnapshot(conversation_ref=conversation_ref, state=DemoState.NEW, version=0),
        )

    async def save_transition(
        self, result: TransitionResult, *, now: datetime, facts: dict[str, Any] | None = None
    ) -> StateSnapshot:
        async with self._lock:
            current = await self.load(result.conversation_ref)
            if current.version != result.expected_version:
                raise ConcurrencyConflict(
                    result.conversation_ref,
                    expected=result.expected_version,
                    actual=current.version,
                )
            updated = StateSnapshot(
                conversation_ref=result.conversation_ref,
                state=result.to_state,
                version=current.version + 1,
                demo_id=(facts or {}).get("demo_id", current.demo_id),
                updated_at=ensure_utc(now),
                facts={**current.facts, **(facts or {})},
            )
            self._states[result.conversation_ref] = updated
            self._history.setdefault(result.conversation_ref, []).append(
                {
                    "transition_id": new_ulid(now=now),
                    "from_state": result.from_state.value,
                    "to_state": result.to_state.value,
                    "trigger": result.trigger.value,
                    "actor": result.actor.value,
                    "command": result.command.value,
                    "reason": result.reason,
                    "version": updated.version,
                    "occurred_at": ensure_utc(now).isoformat(),
                }
            )
            return updated

    async def history(self, conversation_ref: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._history.get(conversation_ref, []))[-limit:]

    async def load_ownership(self, conversation_ref: str, *, now: datetime) -> Ownership:
        return self._ownership.get(conversation_ref) or unowned(conversation_ref, now=now)

    async def save_ownership(self, ownership: Ownership) -> None:
        self._ownership[ownership.conversation_ref] = ownership

    async def touch_inbound(self, conversation_ref: str, *, at: datetime) -> None:
        self._last_inbound[conversation_ref] = ensure_utc(at)

    async def last_inbound_at(self, conversation_ref: str) -> datetime | None:
        return self._last_inbound.get(conversation_ref)


class InMemoryIdempotencyRepository:
    """Single-winner claim with a TTL.

    The TTL is what makes a redelivery weeks later behave like a new event
    rather than being silently dropped by a key nobody ever cleaned up.
    """

    def __init__(self) -> None:
        self._claims: dict[tuple[str, str], datetime] = {}
        self._results: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def claim(self, key: str, *, scope: str, now: datetime, ttl_seconds: int) -> bool:
        async with self._lock:
            bucket = (scope, key)
            expiry = self._claims.get(bucket)
            moment = ensure_utc(now)
            if expiry is not None and moment < expiry:
                return False
            self._claims[bucket] = moment + timedelta(seconds=ttl_seconds)
            return True

    async def result_for(self, key: str, *, scope: str) -> dict[str, Any] | None:
        return self._results.get((scope, key))

    async def record_result(self, key: str, *, scope: str, result: dict[str, Any]) -> None:
        self._results[(scope, key)] = result


class InMemoryOutboxRepository:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._keys: set[str] = set()
        self._lock = asyncio.Lock()

    async def enqueue(
        self, *, event: str, payload: dict[str, Any], idempotency_key: str, now: datetime
    ) -> bool:
        async with self._lock:
            if idempotency_key in self._keys:
                return False
            self._keys.add(idempotency_key)
            self._rows.append(
                {
                    "outbox_id": new_ulid(now=now),
                    "event": event,
                    "payload": payload,
                    "idempotency_key": idempotency_key,
                    "created_at": ensure_utc(now).isoformat(),
                    "published_at": None,
                }
            )
            return True

    async def unpublished(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [row for row in self._rows if row["published_at"] is None][:limit]

    async def mark_published(self, outbox_id: str, *, now: datetime) -> None:
        for row in self._rows:
            if row["outbox_id"] == outbox_id:
                row["published_at"] = ensure_utc(now).isoformat()
                return

    # Test affordance. Not part of the protocol.
    def events(self) -> list[str]:
        return [row["event"] for row in self._rows]
