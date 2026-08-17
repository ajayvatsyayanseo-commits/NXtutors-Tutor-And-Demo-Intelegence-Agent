"""`AgentEnvelopeV1` — the versioned envelope for every cross-agent event.

Wire-compatible with the Tutor Intelligence Agent's envelope
(`tutor_match_meta/contracts/envelope.py`) on purpose: the two services already
agree on `AgentId.DEMO_COMMAND_CENTER`, so routing to this agent needs no change
upstream. Field names, the hop budget and the header set are all pinned by
`tests/contract/test_envelope_compatibility.py`.

Three properties that are expensive to retrofit:

* **Causation vs correlation.** `correlation_id` groups everything descending
  from one parent message; `causation_id` names the single event that directly
  produced this one. With only one of them you can group a trace but cannot
  reconstruct who called whom.
* **Loop prevention travels in the envelope**, not in a service. `hop_count` and
  `visited_agents` move with the event, so a cycle is caught at the receiving
  edge even when the two agents in the loop know nothing about each other.
* **No PII in routing metadata.** `conversation_ref` is a peppered hash. That is
  what makes an envelope safe to log in full.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENVELOPE_VERSION = "1.0"

#: A handoff chain longer than this is a routing bug, not a deep conversation.
MAX_HOPS = 6


class AgentId(StrEnum):
    """Every agent that can appear in a handoff chain. A closed set: an unknown
    destination is rejected at the edge rather than routed somewhere odd."""

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


#: Which handoffs this agent is permitted to make. An allowlist rather than a
#: free-for-all: a bug that hands a paying customer to `lead_intake` restarts
#: their funnel, and the cheapest place to make that impossible is here.
ALLOWED_EDGES: frozenset[tuple[AgentId, AgentId]] = frozenset(
    {
        (AgentId.LEAD_INTAKE, AgentId.DEMO_COMMAND_CENTER),
        (AgentId.TUTOR_MATCH, AgentId.DEMO_COMMAND_CENTER),
        (AgentId.DEMO_COMMAND_CENTER, AgentId.ONBOARDING),
        (AgentId.DEMO_COMMAND_CENTER, AgentId.HUMAN),
        (AgentId.DEMO_COMMAND_CENTER, AgentId.WEBSITE),
        (AgentId.DEMO_COMMAND_CENTER, AgentId.NOTIFICATION),
        (AgentId.DEMO_COMMAND_CENTER, AgentId.CHITRAGUPTA),
        (AgentId.DEMO_COMMAND_CENTER, AgentId.TUTOR_MATCH),
    }
)


class EdgeNotAllowed(Exception):
    def __init__(self, source: AgentId, destination: AgentId) -> None:
        super().__init__(f"handoff {source.value} -> {destination.value} is not permitted")
        self.source = source
        self.destination = destination


def assert_edge_allowed(source: AgentId, destination: AgentId) -> None:
    if (source, destination) not in ALLOWED_EDGES:
        raise EdgeNotAllowed(source, destination)


class LoopDetected(Exception):
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
    trace_id: str = Field(max_length=64)
    causation_id: str | None = Field(default=None, max_length=128)
    correlation_id: str = Field(max_length=128)

    source_agent: AgentId
    destination_agent: AgentId
    event_type: str = Field(max_length=64)
    payload_version: str = Field(default="1", max_length=8)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: When this event stops being actionable. A handoff nobody accepted inside
    #: its TTL is a failure to surface, not a message to deliver late.
    expires_at: datetime | None = None

    #: Peppered hash, never the raw conversation id.
    conversation_ref: str = Field(max_length=128)
    tenant_id: str = Field(default="nxtutors", max_length=64)
    entities: tuple[EntityRef, ...] = ()
    purpose: str = Field(max_length=64)

    idempotency_key: str = Field(max_length=128)

    hop_count: int = Field(default=0, ge=0, le=MAX_HOPS)
    #: Ordered. A set would lose the path, which is what you need for diagnosis.
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

    def expired(self, *, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def next_hop(
        self,
        *,
        destination: AgentId,
        event_type: str,
        payload: dict[str, Any] | None = None,
        purpose: str | None = None,
    ) -> AgentEnvelopeV1:
        """Build the follow-on envelope, enforcing the handoff graph.

        Raises rather than returning an error value: a caller that ignores a
        returned error object would send the loop anyway.
        """
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
            tenant_id=self.tenant_id,
            entities=self.entities,
            purpose=purpose or self.purpose,
            idempotency_key=f"{self.idempotency_key}:{destination.value}",
            hop_count=self.hop_count + 1,
            visited_agents=chain,
            payload=payload or {},
        )

    def headers(self) -> dict[str, str]:
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
    expires_at: datetime | None = None,
) -> AgentEnvelopeV1:
    """First envelope in a chain — hop 0, nobody visited yet."""
    assert_edge_allowed(source, destination)
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
        expires_at=expires_at,
    )
