"""`AgentEnvelopeV1` — the versioned envelope for every cross-agent event.

Three things live here that are easy to get wrong and expensive to retrofit:

* **Causation vs correlation.** `correlation_id` groups everything that came
  from one parent message; `causation_id` names the single event that directly
  produced this one. Without both you can group a trace but cannot reconstruct
  who called whom.
* **Loop prevention is carried in the envelope, not in a service.** `hop_count`
  and `visited_agents` travel with the event, so a cycle is detected at the
  receiving edge even when the two agents in the loop know nothing about each
  other.
* **No PII in the envelope.** Entity references are pseudonymous. The payload
  may carry business data, but the routing metadata never carries a phone
  number — that is what makes envelopes safe to log in full.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENVELOPE_VERSION = "1.0"

#: A handoff chain longer than this is a routing bug, not a deep conversation.
#: Lead Intake -> TutorMatch -> Onboarding -> Lead Intake -> TutorMatch is 5.
MAX_HOPS = 6


class AgentId(StrEnum):
    """Every agent that can appear in a handoff chain.

    A closed set on purpose: an unknown `destination_agent` is rejected at the
    edge rather than routed somewhere unexpected.
    """

    LEAD_INTAKE = "lead_intake_agent"
    ONBOARDING = "onboarding_agent"
    TUTOR_MATCH = "tutor_match_meta"
    DEMO_COMMAND_CENTER = "demo_command_center_agent"
    CHITRAGUPTA = "chitragupta_memory"
    WEBSITE = "nxtutors_website"
    COUNSELOR = "counselor_agent"
    CRM = "crm_agent"
    NOTIFICATION = "notification_agent"
    SCHEDULING = "scheduling_agent"
    HUMAN = "human_operator"


class LoopDetected(Exception):
    """A handoff would revisit an agent, or the chain is too long."""

    def __init__(self, reason: str, chain: tuple[str, ...]) -> None:
        super().__init__(f"{reason} (chain: {' -> '.join(chain)})")
        self.reason = reason
        self.chain = chain


class EntityRef(BaseModel):
    """A pseudonymous reference. Never a phone number, email or name."""

    model_config = ConfigDict(frozen=True)

    entity_type: str = Field(max_length=32)
    entity_id: str = Field(max_length=128)


class AgentEnvelopeV1(BaseModel):
    """The wire format for an agent-to-agent event."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = ENVELOPE_VERSION
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=128)
    #: Constant for everything descending from one parent WhatsApp message.
    trace_id: str = Field(max_length=64)
    #: The event that directly caused this one. None for a chain root.
    causation_id: str | None = Field(default=None, max_length=128)
    #: Business grouping — usually the conversation.
    correlation_id: str = Field(max_length=128)

    source_agent: AgentId
    destination_agent: AgentId
    event_type: str = Field(max_length=64)
    payload_version: str = Field(default="1", max_length=8)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    #: Peppered hash, never the raw conversation id.
    conversation_ref: str = Field(max_length=128)
    entities: tuple[EntityRef, ...] = ()
    #: Why this event is being sent — the scope for memory and data access.
    purpose: str = Field(max_length=64)

    #: Deterministic; the receiver dedupes on it.
    idempotency_key: str = Field(max_length=128)

    # ------------------------------------------------------- loop control
    hop_count: int = Field(default=0, ge=0, le=MAX_HOPS)
    #: Ordered chain. Order matters for diagnosis: a set would lose the path.
    visited_agents: tuple[AgentId, ...] = ()

    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_chain(self) -> Self:
        if self.hop_count > MAX_HOPS:
            raise LoopDetected("hop budget exceeded", tuple(a.value for a in self.visited_agents))
        if self.source_agent is self.destination_agent:
            raise ValueError(f"agent {self.source_agent} cannot hand off to itself")
        return self

    @property
    def chain(self) -> tuple[str, ...]:
        return tuple(agent.value for agent in self.visited_agents)

    def next_hop(
        self,
        *,
        destination: AgentId,
        event_type: str,
        payload: dict[str, Any] | None = None,
        purpose: str | None = None,
    ) -> AgentEnvelopeV1:
        """Build the follow-on envelope, enforcing the handoff graph.

        Raises `LoopDetected` rather than returning something invalid: a caller
        that ignores a returned error object would send the loop anyway.
        """
        from tutor_match_meta.integrations.agents.graph import assert_edge_allowed

        chain = (*self.visited_agents, self.destination_agent)
        if destination in chain:
            raise LoopDetected(
                f"{destination.value} already appears in this chain",
                tuple(a.value for a in chain),
            )
        if self.hop_count + 1 > MAX_HOPS:
            raise LoopDetected("hop budget exceeded", tuple(a.value for a in chain))

        assert_edge_allowed(self.destination_agent, destination)

        return AgentEnvelopeV1(
            trace_id=self.trace_id,
            causation_id=self.event_id,
            correlation_id=self.correlation_id,
            source_agent=self.destination_agent,
            destination_agent=destination,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
            conversation_ref=self.conversation_ref,
            entities=self.entities,
            purpose=purpose or self.purpose,
            idempotency_key=f"{self.idempotency_key}:{destination.value}",
            hop_count=self.hop_count + 1,
            visited_agents=chain,
            payload=payload or {},
        )

    def headers(self) -> dict[str, str]:
        """Trace headers for the outgoing HTTP call."""
        return {
            "X-Trace-Id": self.trace_id,
            "X-Correlation-Id": self.correlation_id,
            "X-Causation-Id": self.causation_id or "",
            "X-Nxt-Agent": self.source_agent.value,
            "X-Nxt-Hop-Count": str(self.hop_count),
            "X-Idempotency-Key": self.idempotency_key,
        }


def root_envelope(
    *,
    trace_id: str,
    correlation_id: str,
    conversation_ref: str,
    source: AgentId,
    destination: AgentId,
    event_type: str,
    purpose: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    entities: tuple[EntityRef, ...] = (),
) -> AgentEnvelopeV1:
    """First envelope in a chain — hop 0, nobody visited yet."""
    return AgentEnvelopeV1(
        trace_id=trace_id,
        causation_id=None,
        correlation_id=correlation_id,
        source_agent=source,
        destination_agent=destination,
        event_type=event_type,
        conversation_ref=conversation_ref,
        entities=entities,
        purpose=purpose,
        idempotency_key=idempotency_key,
        hop_count=0,
        visited_agents=(source,),
        payload=payload or {},
    )
