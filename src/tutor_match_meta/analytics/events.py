"""Sanitised product analytics.

The funnel questions this service has to answer — how many requirements
complete, how many shortlists convert to a demo, where parents drop out — need
per-event data. None of them need a parent's message, phone number, name, or
locality at street granularity.

So the event is a **closed shape**: a name from a fixed enum, a pseudonymous
conversation ref, and a dimension bag whose keys are allowlisted and whose
values are coerced to low-cardinality buckets. Anything not on the allowlist is
dropped, loudly, at construction. That is deliberately stricter than a regex
scrub: the field types already tell us what is sensitive, so we do not need to
go looking for phone numbers in a field that should never have contained free
text in the first place (§3).

The write path is fire-and-forget. Analytics must never fail a parent's turn,
and an analytics outage is not an incident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.security.pii import FORBIDDEN_IN_METRIC_LABELS, contains_pii

logger = get_logger("analytics")


class AnalyticsEventName(StrEnum):
    """The funnel. Adding a name here is a deliberate schema change."""

    MATCH_STARTED = "match_started"
    REQUIREMENT_UPDATED = "requirement_updated"
    REQUIREMENT_COMPLETED = "requirement_completed"
    CLARIFICATION_ASKED = "clarification_asked"
    SHORTLIST_GENERATED = "shortlist_generated"
    NO_CANDIDATE = "no_candidate"
    CANDIDATE_CLICKED = "candidate_clicked"
    DEMO_REQUESTED = "demo_requested"
    MATCH_ABANDONED = "match_abandoned"
    HUMAN_HANDOFF = "human_handoff"
    REPLACEMENT_REQUESTED = "replacement_requested"
    CONSTRAINT_RELAXED = "constraint_relaxed"
    ABUSE_ENFORCED = "abuse_enforced"


#: Dimensions an event may carry. Everything else is dropped.
#:
#: Every one of these is either a bounded enum, a bucket label, or a small
#: integer. There is no free-text dimension and no identifier, which is what
#: makes the export safe to hand to Glue and to a BI tool without a further
#: review (§23, §31).
ALLOWED_DIMENSIONS: frozenset[str] = frozenset(
    {
        "subject",  # canonical subject name, not the parent's wording
        "board",
        "class_band",  # "primary" | "middle" | "secondary" | "senior"
        "mode",
        "city",  # city only — never locality, never pincode
        "urgency",
        "policy_ref",
        "shortlist_size",
        "candidate_pool_size",
        "turn_index",
        "no_match_reason",
        "degraded",  # comma-joined dependency names
        "weight_coverage_band",  # "low" | "medium" | "high"
        "requires_human_review",
        "asked_for",  # the missing field name, not its value
        "enforcement",  # abuse ladder rung
        "relaxation",  # which constraint was relaxed
        "source_agent",
        "llm_used",
        "app_version",
    }
)


#: Bands rather than raw floats: a coverage of 0.4137 is an identifier-shaped
#: number in a small population, and nobody has ever needed that precision to
#: read a funnel.
def coverage_band(value: float) -> str:
    if value >= 0.7:
        return "high"
    if value >= 0.35:
        return "medium"
    return "low"


def class_band(class_label: str | None) -> str:
    """Bucket a class label. Individual classes are fine; this is for grouping."""
    from tutor_match_meta.domain import academics

    number = academics.class_number(class_label) if class_label else None
    if number is None:
        return "unknown"
    if number <= 5:
        return "primary"
    if number <= 8:
        return "middle"
    if number <= 10:
        return "secondary"
    return "senior"


class UnsafeDimension(ValueError):
    """A dimension that would have leaked. Raised at construction, never sent."""


@dataclass(frozen=True, slots=True)
class AnalyticsEventV1:
    """One sanitised funnel event."""

    name: AnalyticsEventName
    #: Peppered pseudonym (`cv_…`). Never the conversation id.
    conversation_ref: str
    match_session_id: str | None = None
    policy_ref: str | None = None
    dimensions: dict[str, str | int | bool] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        unknown = sorted(set(self.dimensions) - ALLOWED_DIMENSIONS)
        if unknown:
            raise UnsafeDimension(f"dimensions not on the analytics allowlist: {unknown}")
        banned = sorted(set(self.dimensions) & FORBIDDEN_IN_METRIC_LABELS)
        if banned:
            raise UnsafeDimension(f"identifying dimensions are never exported: {banned}")
        for key, value in self.dimensions.items():
            # Belt and braces. The allowlist should already make this
            # impossible; if a bounded field ever starts carrying free text,
            # this is the layer that notices before the export does.
            if isinstance(value, str) and contains_pii(value):
                raise UnsafeDimension(f"dimension {key!r} contains a direct identifier")
        if self.conversation_ref and not self.conversation_ref.startswith(("cv_", "pt_", "ph_")):
            raise UnsafeDimension(
                "conversation_ref must be a pseudonym; a raw conversation id is not exportable"
            )

    def as_row(self) -> dict[str, Any]:
        return {
            "event_name": self.name.value,
            "conversation_ref": self.conversation_ref,
            "match_session_id": self.match_session_id,
            "policy_ref": self.policy_ref,
            "dimensions": dict(self.dimensions),
            "occurred_at": self.occurred_at,
        }


@runtime_checkable
class AnalyticsSink(Protocol):
    async def emit(self, event: AnalyticsEventV1) -> None: ...


class NullAnalytics:
    """Records nothing. The default when analytics is disabled."""

    def __init__(self) -> None:
        self.events: list[AnalyticsEventV1] = []

    async def emit(self, event: AnalyticsEventV1) -> None:
        return None


class RecordingAnalytics:
    """In-process sink for tests, `make e2e` and the doctor command."""

    def __init__(self) -> None:
        self.events: list[AnalyticsEventV1] = []

    async def emit(self, event: AnalyticsEventV1) -> None:
        self.events.append(event)

    def names(self) -> list[str]:
        return [event.name.value for event in self.events]


class LoggingAnalytics:
    """Writes one structured line per event.

    Useful before the database sink is wired, and as the fallback when the
    PostgreSQL sink is degraded — the funnel is worth keeping even when the
    table is unreachable, because CloudWatch Logs Insights can reconstruct it.
    """

    async def emit(self, event: AnalyticsEventV1) -> None:
        logger.info(
            "analytics",
            extra={
                "tmm_event": event.name.value,
                "tmm_conversation_ref": event.conversation_ref,
                "tmm_dimensions": event.dimensions,
            },
        )


class PostgresAnalytics:
    """Appends to `analytics_event`, from which Glue exports to S3.

    Swallows every failure by design. A funnel event is worth strictly less
    than the parent's reply, and an analytics write must never be the reason a
    turn fails.
    """

    def __init__(self, sessions: Any, *, schema: str) -> None:
        self._sessions = sessions
        self._table = f"{schema}.analytics_event" if schema else "analytics_event"
        self.failures = 0

    async def emit(self, event: AnalyticsEventV1) -> None:
        import json

        from sqlalchemy import text

        try:
            async with self._sessions() as session, session.begin():
                await session.execute(
                    text(
                        f"INSERT INTO {self._table} "  # noqa: S608 - fixed table name
                        "(event_name, conversation_ref, match_session_id, policy_ref, "
                        " dimensions, occurred_at) "
                        "VALUES (:name, :ref, :session, :policy, "
                        "        CAST(:dimensions AS jsonb), :at)"
                    ),
                    {
                        "name": event.name.value,
                        "ref": event.conversation_ref,
                        "session": event.match_session_id,
                        "policy": event.policy_ref,
                        "dimensions": json.dumps(event.dimensions),
                        "at": event.occurred_at,
                    },
                )
        except Exception:
            self.failures += 1
            logger.warning("analytics write failed", extra={"tmm_event": event.name.value})


__all__ = [
    "ALLOWED_DIMENSIONS",
    "AnalyticsEventName",
    "AnalyticsEventV1",
    "AnalyticsSink",
    "LoggingAnalytics",
    "NullAnalytics",
    "PostgresAnalytics",
    "RecordingAnalytics",
    "UnsafeDimension",
    "class_band",
    "coverage_band",
]
