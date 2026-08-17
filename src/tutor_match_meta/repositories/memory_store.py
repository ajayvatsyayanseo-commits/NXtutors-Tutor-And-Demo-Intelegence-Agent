"""In-memory implementations of every repository port.

These are not test doubles in the "cheap fake" sense — they implement the same
filtering semantics as the PostgreSQL projection, so `make e2e` exercises real
matching behaviour without a database. Where the semantics would differ, the
docstring says so.

Used by: local E2E, unit tests, and the `doctor` command.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from tutor_match_meta.contracts.common import Freshness, TuitionMode
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.contracts.scoring import MatchDecisionV1
from tutor_match_meta.contracts.tutor import GeoPoint, TutorCandidate
from tutor_match_meta.domain import academics, subjects
from tutor_match_meta.repositories.ports import (
    CandidateQuery,
    MemoryPacket,
    OutboxMessage,
)
from tutor_match_meta.state.machine import (
    ConversationSnapshot,
    OptimisticLockError,
)


class InMemoryTutorRepository:
    """Mirrors the projection query semantics of the SQL repository.

    Subject/class matching happens in Python here and in SQL there, but both use
    the same `domain` predicates, so a tutor that survives one survives the other.
    """

    def __init__(self, tutors: list[TutorCandidate] | None = None) -> None:
        self._tutors: dict[str, TutorCandidate] = {t.tutor_id: t for t in (tutors or [])}

    def add(self, tutor: TutorCandidate) -> None:
        self._tutors[tutor.tutor_id] = tutor

    def replace_all(self, tutors: list[TutorCandidate]) -> None:
        self._tutors = {t.tutor_id: t for t in tutors}

    async def search(self, query: CandidateQuery) -> list[TutorCandidate]:
        if query.public_ref:
            found = await self.get_by_public_ref(query.public_ref)
            return [found] if found else []

        matches = [t for t in self._tutors.values() if self._matches(t, query)]
        # Same ordering as the SQL projection: strongest social proof first, then
        # a stable id tiebreak so repeated runs return the same pool.
        matches.sort(key=lambda t: (-t.reviews.count, -(t.reviews.rating_avg or 0.0), t.tutor_id))
        return matches[: query.limit]

    def _matches(self, tutor: TutorCandidate, query: CandidateQuery) -> bool:
        if tutor.freshness not in query.min_freshness:
            return False
        if query.subjects and tutor.capabilities.subjects:
            if not any(
                subjects.matches_any(want, list(tutor.capabilities.subjects))
                for want in query.subjects
            ):
                return False
        if query.class_labels and tutor.capabilities.classes:
            if not academics.teaches_class(query.class_labels[0], list(tutor.capabilities.classes)):
                return False
        if query.city_aliases and tutor.city:
            if tutor.city.lower() not in query.city_aliases:
                # Pincode equality is an accepted alternative to a city match,
                # exactly as in the SQL `WHERE (pincode = ? OR city IN (...))`.
                if not (query.pincode and tutor.pincode == query.pincode):
                    return False
        if isinstance(query.mode, TuitionMode) and not tutor.supports_mode(query.mode):
            return False
        if query.gender and tutor.gender and tutor.gender.lower() != query.gender:
            return False
        if query.max_fee is not None and tutor.fee.minimum is not None:
            # Generous: the negotiation hard filter applies the policy ratio.
            # Filtering tightly here would hide tutors a package rate could reach.
            if tutor.fee.minimum > query.max_fee * 2:
                return False
        return True

    async def get(self, tutor_id: str) -> TutorCandidate | None:
        return self._tutors.get(tutor_id)

    async def get_by_public_ref(self, public_ref: str) -> TutorCandidate | None:
        return next((t for t in self._tutors.values() if t.public_ref == public_ref), None)


class InMemoryConversationStore:
    """Conversation state with real optimistic-lock semantics."""

    def __init__(self) -> None:
        self._states: dict[str, ConversationSnapshot] = {}
        self._lock = asyncio.Lock()

    async def load(self, conversation_id: str) -> ConversationSnapshot:
        return self._states.get(conversation_id) or ConversationSnapshot(
            conversation_id=conversation_id
        )

    async def save(self, snapshot: ConversationSnapshot, *, expected_version: int) -> None:
        async with self._lock:
            current = self._states.get(snapshot.conversation_id)
            actual = current.lock_version if current else 0
            if actual != expected_version:
                raise OptimisticLockError(snapshot.conversation_id, expected_version, actual)
            self._states[snapshot.conversation_id] = snapshot


class InMemoryRequirementStore:
    def __init__(self) -> None:
        self._items: dict[str, MatchRequirementV1] = {}

    async def load(self, conversation_id: str) -> MatchRequirementV1 | None:
        return self._items.get(conversation_id)

    async def save(self, requirement: MatchRequirementV1) -> None:
        self._items[requirement.conversation_id] = requirement


class InMemoryDecisionStore:
    def __init__(self) -> None:
        self._by_conversation: dict[str, list[MatchDecisionV1]] = {}

    async def save(self, decision: MatchDecisionV1) -> None:
        self._by_conversation.setdefault(decision.conversation_id, []).append(decision)

    async def latest(self, conversation_id: str) -> MatchDecisionV1 | None:
        items = self._by_conversation.get(conversation_id)
        return items[-1] if items else None

    def all_for(self, conversation_id: str) -> list[MatchDecisionV1]:
        return list(self._by_conversation.get(conversation_id, []))


class InMemoryIdempotencyStore:
    """First claim wins; later claims for the same key are duplicates."""

    def __init__(self) -> None:
        self._claims: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def claim(self, key: str, *, ttl_seconds: int) -> bool:
        async with self._lock:
            now = datetime.now(UTC)
            expiry = self._claims.get(key)
            if expiry is not None and expiry > now:
                return False
            self._claims[key] = now + timedelta(seconds=ttl_seconds)
            return True

    async def release(self, key: str) -> None:
        async with self._lock:
            self._claims.pop(key, None)


class InMemoryOutbox:
    """Models the SQL outbox's *lease* semantics, not just its storage.

    `claim_batch` moves rows out of `pending` under a lock, exactly as the
    PostgreSQL version flips `status` to `claiming` inside the same transaction
    that takes the row lock. That fidelity is the point: an earlier in-memory
    version returned rows without claiming them, which made a genuine
    double-delivery bug in the SQL store invisible to the test suite.
    """

    def __init__(self) -> None:
        self.pending: list[OutboxMessage] = []
        self.claimed: dict[str, tuple[OutboxMessage, datetime]] = {}
        self.delivered: list[OutboxMessage] = []
        self.dead: list[OutboxMessage] = []
        self.failed: list[tuple[str, str]] = []
        self._lock = asyncio.Lock()

    async def enqueue(self, message: OutboxMessage) -> None:
        async with self._lock:
            known = (
                [m.dedup_key for m in self.pending]
                + list(self.claimed)
                + [m.dedup_key for m in self.delivered]
                + [m.dedup_key for m in self.dead]
            )
            if message.dedup_key in known:
                return  # Idempotent by dedup key, like the SQL unique index.
            self.pending.append(message)

    async def claim_batch(self, *, limit: int, now: datetime) -> list[OutboxMessage]:
        async with self._lock:
            ready = [m for m in self.pending if m.available_at is None or m.available_at <= now]
            taken = ready[:limit]
            for message in taken:
                self.pending.remove(message)
                self.claimed[message.dedup_key] = (message, now)
            return taken

    async def mark_delivered(self, dedup_key: str) -> None:
        async with self._lock:
            claimed = self.claimed.pop(dedup_key, None)
            if claimed is not None:
                self.delivered.append(claimed[0])
                return
            for message in list(self.pending):
                if message.dedup_key == dedup_key:
                    self.pending.remove(message)
                    self.delivered.append(message)

    async def mark_failed(self, dedup_key: str, *, error: str, retry_at: datetime) -> None:
        async with self._lock:
            self.failed.append((dedup_key, error))
            claimed = self.claimed.pop(dedup_key, None)
            message = claimed[0] if claimed else None
            if message is None:
                message = next((m for m in self.pending if m.dedup_key == dedup_key), None)
                if message is None:
                    return
                self.pending.remove(message)
            retried = OutboxMessage(
                kind=message.kind,
                conversation_id=message.conversation_id,
                payload=message.payload,
                dedup_key=message.dedup_key,
                trace_id=message.trace_id,
                available_at=retry_at,
                attempts=message.attempts + 1,
            )
            if retried.attempts >= 5:
                self.dead.append(retried)
            else:
                self.pending.append(retried)

    async def mark_dead(self, dedup_key: str, *, error: str) -> None:
        async with self._lock:
            self.failed.append((dedup_key, error))
            claimed = self.claimed.pop(dedup_key, None)
            if claimed is not None:
                self.dead.append(claimed[0])
                return
            for message in list(self.pending):
                if message.dedup_key == dedup_key:
                    self.pending.remove(message)
                    self.dead.append(message)

    async def reclaim_stale(self, *, older_than: datetime) -> int:
        async with self._lock:
            stale = [key for key, (_, at) in self.claimed.items() if at < older_than]
            for key in stale:
                message, _ = self.claimed.pop(key)
                self.pending.append(message)
            return len(stale)


class StaticGeocoder:
    """Offline pincode/locality geocoder.

    Only ever receives a pincode, locality label or city — never a street
    address (docs/assumptions.md A5). Unknown inputs return None rather than a
    city-centre guess, so the proximity evaluator falls back to a coarse tier
    it will honestly label as such.
    """

    def __init__(self, points: dict[str, tuple[float, float]] | None = None) -> None:
        self._points = points or {}

    def add(self, key: str, latitude: float, longitude: float) -> None:
        self._points[key.lower()] = (latitude, longitude)

    async def resolve(
        self, *, pincode: str | None, locality: str | None, city: str | None
    ) -> GeoPoint | None:
        for key, granularity in (
            (pincode, "pincode"),
            (locality, "locality"),
            (city, "city"),
        ):
            if not key:
                continue
            point = self._points.get(key.lower())
            if point:
                return GeoPoint(
                    latitude=point[0],
                    longitude=point[1],
                    granularity=granularity,
                    resolved_at=datetime.now(UTC),
                )
        return None


class NullMemory:
    """Memory port that is always unavailable.

    Used to prove the match path never depends on Chitragupta being up —
    `recall` returns a degraded packet and `record` reports failure, and the
    pipeline must still produce a shortlist.
    """

    def __init__(self) -> None:
        self.recorded: list[dict[str, object]] = []

    async def recall(
        self, *, entity_type: str, entity_id: str, purpose: str, trace_id: str
    ) -> MemoryPacket:
        return MemoryPacket(available=False, warnings=("memory_disabled",))

    async def record(
        self,
        *,
        deed_type: str,
        purpose: str,
        trace_id: str,
        entity_scope: list[dict[str, str]],
        summary: str | None = None,
        lifecycle_status: str = "completed",
        extra: dict[str, object] | None = None,
    ) -> bool:
        self.recorded.append({"deed_type": deed_type, "trace_id": trace_id})
        return False


def freshness_at(hours_old: float, *, fresh_hours: int = 6, aging_hours: int = 24) -> Freshness:
    if hours_old <= fresh_hours:
        return Freshness.FRESH
    if hours_old <= aging_hours:
        return Freshness.AGING
    return Freshness.STALE
