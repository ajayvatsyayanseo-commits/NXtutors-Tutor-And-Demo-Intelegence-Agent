"""Repository ports.

The orchestrator depends on these Protocols, never on SQLAlchemy, asyncpg, HTTP
or boto3. That is what lets the full matching pipeline run in `make e2e` with no
database and no credentials, and it is why the unit tests are fast.

`CandidateQuery` is deliberately a **structured** query object. There is no
free-text search entry point and no place to pass SQL, so no model output can
reach the database as a query — the strongest form of the "never let an LLM
generate SQL" rule is not having an API that would accept it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from tutor_match_meta.contracts.common import Freshness, TuitionMode
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.contracts.scoring import MatchDecisionV1
from tutor_match_meta.contracts.tutor import GeoPoint, TutorCandidate
from tutor_match_meta.domain import academics, localities, subjects
from tutor_match_meta.state.machine import ConversationSnapshot


@dataclass(frozen=True, slots=True)
class CandidateQuery:
    """Structured filters for the projection query. Every field is typed."""

    limit: int
    subjects: tuple[str, ...] = ()
    boards: tuple[str, ...] = ()
    class_number: int | None = None
    class_labels: tuple[str, ...] = ()
    city_aliases: tuple[str, ...] = ()
    pincode: str | None = None
    mode: TuitionMode | None = None
    gender: str | None = None
    max_fee: int | None = None
    #: Only rows at least this fresh are returned (docs/assumptions.md A16).
    min_freshness: tuple[Freshness, ...] = (Freshness.FRESH, Freshness.AGING)
    public_ref: str | None = None
    #: Tutor-name terms, already tokenised. Every term must appear in the
    #: name. Kept as a tuple of typed values rather than a free-text string
    #: so this stays what `tests/security/test_sql_injection.py` asserts the
    #: whole port is: structured filters, never a search box.
    name_terms: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        """Stable cache key for this query shape."""
        import hashlib

        parts = (
            ",".join(sorted(self.subjects)),
            ",".join(sorted(self.boards)),
            str(self.class_number),
            ",".join(sorted(self.city_aliases)),
            ",".join(self.name_terms),
            self.pincode or "",
            self.mode.value if self.mode else "",
            self.gender or "",
            str(self.max_fee),
            str(self.limit),
            self.public_ref or "",
        )
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def query_from_requirement(requirement: MatchRequirementV1, *, limit: int) -> CandidateQuery:
    """Translate a requirement into structured retrieval filters.

    Only *trustworthy* fields become filters. A low-confidence model guess is
    excluded here for the same reason it is excluded from hard filters: a wrong
    guess that narrows the SQL produces an empty pool and a "no tutors found"
    message for a parent whose request was perfectly matchable.

    Board is deliberately NOT a SQL filter. `teacher_courses.board` is sparsely
    populated free text, so filtering on it in SQL would drop tutors whose board
    column is simply blank. The board hard filter runs afterwards, in Python,
    where "unknown" can be distinguished from "different".
    """
    subject_terms: list[str] = []
    for subject in requirement.all_subjects:
        canonical = subjects.normalize(subject)
        if canonical:
            subject_terms.append(canonical)
            # An umbrella request should also retrieve its component teachers.
            subject_terms.extend(sorted(subjects.parts_of(canonical)))

    city = requirement.location.city
    class_label = academics.normalize_class(requirement.value_of("student_class"))

    return CandidateQuery(
        limit=limit,
        subjects=tuple(dict.fromkeys(subject_terms))
        if requirement.usable_for_hard_filter("subject")
        else (),
        boards=(),
        class_number=academics.class_number(class_label),
        class_labels=(class_label,) if class_label else (),
        city_aliases=tuple(localities.city_aliases(city)) if city else (),
        pincode=requirement.location.pincode,
        mode=requirement.value_of("mode") if requirement.usable_for_hard_filter("mode") else None,
        gender=(requirement.value_of("tutor_gender_preference") or "").lower() or None
        if requirement.usable_for_hard_filter("tutor_gender_preference")
        else None,
        max_fee=requirement.budget.maximum,
        public_ref=requirement.explicit_tutor_ref,
    )


@runtime_checkable
class TutorSearchPort(Protocol):
    """Reads the tutor search projection."""

    async def search(self, query: CandidateQuery) -> list[TutorCandidate]:
        """Bounded candidate pool. Never unbounded, never a full table scan."""
        ...

    async def get(self, tutor_id: str) -> TutorCandidate | None: ...

    async def get_by_public_ref(self, public_ref: str) -> TutorCandidate | None: ...


@runtime_checkable
class ConversationStatePort(Protocol):
    """Persists conversation state under an optimistic lock."""

    async def load(self, conversation_id: str) -> ConversationSnapshot: ...

    async def save(self, snapshot: ConversationSnapshot, *, expected_version: int) -> None:
        """Raises `OptimisticLockError` when another writer won."""
        ...


@runtime_checkable
class RequirementPort(Protocol):
    async def load(self, conversation_id: str) -> MatchRequirementV1 | None: ...

    async def save(self, requirement: MatchRequirementV1) -> None: ...


@runtime_checkable
class DecisionPort(Protocol):
    async def save(self, decision: MatchDecisionV1) -> None: ...

    async def latest(self, conversation_id: str) -> MatchDecisionV1 | None: ...


@runtime_checkable
class IdempotencyPort(Protocol):
    """Durable dedup, the authoritative layer behind the timestamp window."""

    async def claim(self, key: str, *, ttl_seconds: int) -> bool:
        """True when this caller won the claim; False when it is a duplicate."""
        ...

    async def release(self, key: str) -> None:
        """Release a claim so a failed attempt can be retried legitimately."""
        ...


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """A side effect to be delivered exactly once, eventually.

    Written in the same transaction as the state change that caused it, so a
    crash between "decided" and "sent" cannot lose the message or duplicate the
    decision.
    """

    kind: str
    conversation_id: str
    payload: dict[str, object]
    dedup_key: str
    trace_id: str
    available_at: datetime | None = None
    attempts: int = 0


@runtime_checkable
class OutboxPort(Protocol):
    async def enqueue(self, message: OutboxMessage) -> None: ...

    async def claim_batch(self, *, limit: int, now: datetime) -> list[OutboxMessage]:
        """Take an exclusive lease on pending rows.

        Implementations must make the lease atomic with the read. A claim that
        only reads leaves two relays free to deliver the same message.
        """
        ...

    async def mark_delivered(self, dedup_key: str) -> None: ...

    async def mark_failed(self, dedup_key: str, *, error: str, retry_at: datetime) -> None: ...

    async def mark_dead(self, dedup_key: str, *, error: str) -> None:
        """Terminal: no retry can succeed. Alarms rather than looping."""
        ...

    async def reclaim_stale(self, *, older_than: datetime) -> int:
        """Return leases abandoned by a crashed relay to `pending`."""
        ...


@runtime_checkable
class GeoPort(Protocol):
    """Pincode/locality → coordinates, cached. Never geocodes a street address."""

    async def resolve(
        self, *, pincode: str | None, locality: str | None, city: str | None
    ) -> GeoPoint | None: ...


@dataclass(frozen=True, slots=True)
class MemoryFact:
    """One recalled fact, with everything needed to decide whether to trust it."""

    key: str
    value: str
    confidence: float
    source: str
    observed_at: datetime | None = None
    #: Chitragupta may deny a field; the packet says so explicitly rather than
    #: returning an empty result that looks like "nothing known".
    denied: bool = False


@dataclass(frozen=True, slots=True)
class MemoryPacket:
    facts: tuple[MemoryFact, ...] = ()
    session_summary: str | None = None
    denied_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    available: bool = True

    @property
    def degraded(self) -> bool:
        return not self.available


@runtime_checkable
class MemoryPort(Protocol):
    """Chitragupta. Purpose-scoped reads, deed writes, never blocking."""

    async def recall(
        self, *, entity_type: str, entity_id: str, purpose: str, trace_id: str
    ) -> MemoryPacket: ...

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
        """False when memory was unavailable. Never raises into the match path."""
        ...


@dataclass(slots=True)
class DegradedSources:
    """Which dependencies were down during a run. Recorded on the decision."""

    names: set[str] = field(default_factory=set)

    def mark(self, name: str) -> None:
        self.names.add(name)

    def as_tuple(self) -> tuple[str, ...]:
        return tuple(sorted(self.names))
