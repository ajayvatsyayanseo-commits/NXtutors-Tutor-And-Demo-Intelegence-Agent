"""Typed write-back commands to the NXTutors website.

The rule this module exists to enforce: **the agent never issues arbitrary
database mutations.** Every write is one of four named commands with a fixed
shape, an idempotency key, an audit trail and an authorisation scope. There is no
generic `execute(sql)` and no place to put one.

Preferred transport is a signed internal Laravel API, so the website's own
service layer applies its business rules. It is the only writer: there is no
a compatibility fallback, is feature-flagged off, and is restricted to two tables.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from tutor_match_meta.security.signing import idempotency_key

SCHEMA_VERSION = "1.0"
SOURCE_AGENT = "tutor-match-meta"


class CommandRisk(StrEnum):
    """Drives whether a command may auto-execute or needs a human first."""

    LOW = "low"  # writes an agent-owned record
    MEDIUM = "medium"  # visible to a tutor or parent
    HIGH = "high"  # money, or a commitment made on NXTutors' behalf


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    """What actually goes on the wire, for any command."""

    command: str
    schema_version: str
    source_agent: str
    trace_id: str
    idempotency_key: str
    issued_at: str
    risk: str
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {
                "command": self.command,
                "schema_version": self.schema_version,
                "source_agent": self.source_agent,
                "trace_id": self.trace_id,
                "idempotency_key": self.idempotency_key,
                "issued_at": self.issued_at,
                "risk": self.risk,
                "payload": self.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class WebsiteCommand(ABC):
    """Base for every write-back. Subclasses declare shape, risk and identity."""

    name: str
    risk: CommandRisk = CommandRisk.MEDIUM

    @abstractmethod
    def payload(self) -> dict[str, Any]:
        """The validated body. Must contain no secrets and no raw phone number."""

    @abstractmethod
    def identity(self) -> tuple[str, ...]:
        """The parts that make this command unique, for idempotency."""

    def envelope(self, *, trace_id: str, now: datetime | None = None) -> CommandEnvelope:
        return CommandEnvelope(
            command=self.name,
            schema_version=SCHEMA_VERSION,
            source_agent=SOURCE_AGENT,
            trace_id=trace_id,
            idempotency_key=idempotency_key(self.name, *self.identity()),
            issued_at=(now or datetime.now(UTC)).isoformat(),
            risk=self.risk.value,
            payload=self.payload(),
        )


@dataclass(frozen=True, slots=True)
class CreateTutorMatchCommand(WebsiteCommand):
    """Record a generated shortlist against the lead, for the dashboard."""

    name = "CreateTutorMatch"
    risk = CommandRisk.LOW

    match_session_id: str
    conversation_hash: str
    tutor_ids: tuple[str, ...]
    policy_ref: str
    lead_id: str | None = None
    generated_at: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "match_session_id": self.match_session_id,
            "conversation_hash": self.conversation_hash,
            "lead_id": self.lead_id,
            "tutor_ids": list(self.tutor_ids),
            "policy_ref": self.policy_ref,
            "generated_at": self.generated_at,
        }

    def identity(self) -> tuple[str, ...]:
        return (self.match_session_id,)


@dataclass(frozen=True, slots=True)
class RecordParentSelectionCommand(WebsiteCommand):
    """The parent chose a tutor from a shortlist we sent."""

    name = "RecordParentSelection"
    risk = CommandRisk.MEDIUM

    match_session_id: str
    tutor_id: str
    conversation_hash: str
    selected_at: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "match_session_id": self.match_session_id,
            "tutor_id": self.tutor_id,
            "conversation_hash": self.conversation_hash,
            "selected_at": self.selected_at,
        }

    def identity(self) -> tuple[str, ...]:
        return (self.match_session_id, self.tutor_id)


@dataclass(frozen=True, slots=True)
class CreateDemoRequestCommand(WebsiteCommand):
    """Create a `demo_leads` row. Visible to the tutor — medium risk."""

    name = "CreateDemoRequest"
    risk = CommandRisk.MEDIUM

    match_session_id: str
    tutor_id: str
    conversation_hash: str
    subject: str | None = None
    child_class: str | None = None
    preferred_time: str | None = None
    mode: str | None = None
    location: str | None = None
    #: Parent contact is resolved website-side from the lead, so the agent never
    #: transmits a phone number to create a demo.
    lead_id: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "match_session_id": self.match_session_id,
            "tutor_id": self.tutor_id,
            "conversation_hash": self.conversation_hash,
            "lead_id": self.lead_id,
            "subject": self.subject,
            "child_class": self.child_class,
            "preferred_time": self.preferred_time,
            "mode": self.mode,
            "location": self.location,
            "source_page": "whatsapp:tutor-match-meta",
        }

    def identity(self) -> tuple[str, ...]:
        return (self.match_session_id, self.tutor_id, "demo")


@dataclass(frozen=True, slots=True)
class PublishTutorLeadCommand(WebsiteCommand):
    """Push the enquiry into the tutor's dashboard workflow."""

    name = "PublishTutorLead"
    risk = CommandRisk.MEDIUM

    match_session_id: str
    tutor_id: str
    conversation_hash: str
    subject: str | None = None
    child_class: str | None = None
    board: str | None = None
    city: str | None = None
    locality: str | None = None
    budget_label: str | None = None
    lead_id: str | None = None
    notes: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "match_session_id": self.match_session_id,
            "tutor_id": self.tutor_id,
            "conversation_hash": self.conversation_hash,
            "lead_id": self.lead_id,
            "subject": self.subject,
            "child_class": self.child_class,
            "board": self.board,
            "city": self.city,
            "locality": self.locality,
            "budget_label": self.budget_label,
            "notes": self.notes[:500],
        }

    def identity(self) -> tuple[str, ...]:
        return (self.match_session_id, self.tutor_id, "lead")


#: Commands the agent may execute without a human. Anything HIGH-risk, or any
#: command not listed here, routes through HITL approval.
AUTO_EXECUTABLE: frozenset[str] = frozenset(
    {
        CreateTutorMatchCommand.name,
        RecordParentSelectionCommand.name,
        CreateDemoRequestCommand.name,
        PublishTutorLeadCommand.name,
    }
)


@dataclass(slots=True)
class ApprovalRequest:
    """A command held for a human. The HITL queue entry."""

    envelope: CommandEnvelope
    reason: str
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    approved_by: str | None = None
    approved_at: datetime | None = None

    @property
    def approved(self) -> bool:
        return self.approved_by is not None


def requires_approval(command: WebsiteCommand, *, reason: str | None = None) -> str | None:
    """Return a reason string when the command must not auto-execute."""
    if command.risk is CommandRisk.HIGH:
        return reason or "high_risk_command"
    if command.name not in AUTO_EXECUTABLE:
        return reason or "command_not_allowlisted"
    return None
