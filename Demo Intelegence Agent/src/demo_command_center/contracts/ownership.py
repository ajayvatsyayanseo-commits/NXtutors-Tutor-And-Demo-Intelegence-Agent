"""Conversation ownership — the rule that stops two agents answering one parent.

Exactly one agent owns a WhatsApp conversation at a time. Ownership is data, not
convention: it is a row, it is checked on the outbound path, and a capability
worker that finished after a handoff cannot talk over the new owner because the
check happens at send time rather than at decide time.

The distinction that matters most here is **ownership transfer vs capability
call**. Asking Tutor Intelligence to rank tutors is a capability call: no
ownership moves, Tutor Intelligence returns data and stays silent. Handing a
converted customer to Onboarding is a transfer: ownership moves and Demo stops
speaking. Modelling both as "handoff" is how you end up with two senders.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from demo_command_center.contracts.envelope import AgentId
from demo_command_center.shared.clock import ensure_utc

#: A lease nobody renewed is a stuck conversation. Ownership acquired for a
#: transfer expires back to the previous owner rather than stranding the parent.
DEFAULT_LEASE = timedelta(hours=1)


class Owner(StrEnum):
    """Who currently owns the conversation.

    `RELEASED` is distinct from "no row": it means we deliberately let go, which
    an operator reading the audit trail needs to be able to tell apart from a
    conversation that was never claimed.
    """

    LEAD_INTAKE = "lead_intake_agent"
    TUTOR_MATCH = "tutor_match_meta"
    DEMO_COMMAND_CENTER = "demo_command_center_agent"
    ONBOARDING = "onboarding_agent"
    HUMAN = "human_operator"
    RELEASED = "released"

    @classmethod
    def from_agent(cls, agent: AgentId) -> Owner:
        try:
            return cls(agent.value)
        except ValueError as exc:
            raise ValueError(f"{agent.value} is not a conversation owner") from exc


#: This service. Everything else is "not us".
SELF: Owner = Owner.DEMO_COMMAND_CENTER

#: Owners Demo may accept the conversation *from*.
ACCEPT_FROM: frozenset[Owner] = frozenset({Owner.LEAD_INTAKE, Owner.RELEASED, Owner.HUMAN})

#: Owners Demo may hand the conversation *to*. Note the absence of
#: `LEAD_INTAKE`: handing a post-demo customer back to intake restarts their
#: funnel from "hi, how can I help", which is a real incident and not a
#: hypothetical one.
TRANSFER_TO: frozenset[Owner] = frozenset({Owner.ONBOARDING, Owner.HUMAN, Owner.RELEASED})


class OwnershipError(Exception):
    """An ownership rule was violated. Never surfaced to a parent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Ownership(BaseModel):
    """The current ownership record for one conversation."""

    model_config = ConfigDict(frozen=True)

    conversation_ref: str = Field(max_length=128)
    owner: Owner
    since: datetime
    #: None means an indefinite hold — normal for the agent actively working the
    #: conversation. A lease is used for transfers that must not strand.
    lease_expires_at: datetime | None = None
    #: Ordered history of previous owners. Bounded by the handoff hop budget.
    previous: tuple[Owner, ...] = ()

    @model_validator(mode="after")
    def _utc(self) -> Self:
        ensure_utc(self.since)
        if self.lease_expires_at is not None:
            ensure_utc(self.lease_expires_at)
        return self

    def expired(self, *, now: datetime) -> bool:
        return self.lease_expires_at is not None and now >= self.lease_expires_at

    def held_by_us(self, *, now: datetime) -> bool:
        """The only question the outbound path asks."""
        return self.owner is SELF and not self.expired(now=now)

    # ----------------------------------------------------------- operations
    def acquire(self, *, now: datetime, lease: timedelta | None = None) -> Ownership:
        """Take ownership. Idempotent when we already hold it.

        Idempotence is not a nicety here: a redelivered handoff would otherwise
        push our own id onto `previous` and reset `since`, which corrupts the
        very audit trail that answers "who was speaking when".
        """
        if self.owner is SELF and not self.expired(now=now):
            return self
        if self.owner not in ACCEPT_FROM and self.owner is not SELF:
            raise OwnershipError(f"cannot take conversation from {self.owner.value}")
        return Ownership(
            conversation_ref=self.conversation_ref,
            owner=SELF,
            since=now,
            lease_expires_at=now + lease if lease else None,
            previous=(*self.previous, self.owner)[-8:],
        )

    def transfer(
        self, to: Owner, *, now: datetime, lease: timedelta | None = DEFAULT_LEASE
    ) -> Ownership:
        """Hand the conversation on. Only from us, only to an allowed owner."""
        if self.owner is not SELF:
            raise OwnershipError(f"cannot transfer a conversation owned by {self.owner.value}")
        if to not in TRANSFER_TO:
            raise OwnershipError(f"transfer to {to.value} is not permitted")
        return Ownership(
            conversation_ref=self.conversation_ref,
            owner=to,
            since=now,
            lease_expires_at=now + lease if lease and to is not Owner.RELEASED else None,
            previous=(*self.previous, SELF)[-8:],
        )

    def assert_may_send(self, *, now: datetime) -> None:
        """Called by the outbound boundary before every business message."""
        if not self.held_by_us(now=now):
            raise OwnershipError(
                f"not the conversation owner (owner={self.owner.value}, "
                f"expired={self.expired(now=now)})"
            )


def unowned(conversation_ref: str, *, now: datetime) -> Ownership:
    """The starting record for a conversation nobody has claimed."""
    return Ownership(conversation_ref=conversation_ref, owner=Owner.RELEASED, since=now)


class CapabilityCall(BaseModel):
    """A request to another agent that does **not** move ownership.

    Exists as a distinct type so the difference is visible at every call site.
    `assert_return_only` is what a transport adapter calls before dispatching —
    it is the last place a "just let Tutor reply directly, it's simpler" change
    can be caught.
    """

    model_config = ConfigDict(frozen=True)

    conversation_ref: str = Field(max_length=128)
    callee: AgentId
    purpose: str = Field(max_length=64)
    trace_id: str = Field(max_length=64)
    correlation_id: str = Field(max_length=128)
    causation_id: str | None = Field(default=None, max_length=128)
    #: Always true, always asserted. See `TutorMatchRequestV1.return_only`.
    return_only: bool = True

    @model_validator(mode="after")
    def _must_be_return_only(self) -> Self:
        if not self.return_only:
            raise ValueError("a capability call may not authorise the callee to send")
        return self

    def assert_return_only(self, owner: Ownership, *, now: datetime) -> None:
        owner.assert_may_send(now=now)
        if not self.return_only:  # pragma: no cover - validator makes this unreachable
            raise OwnershipError("capability call lost its return_only flag")
