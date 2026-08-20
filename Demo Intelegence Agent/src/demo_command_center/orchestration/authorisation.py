"""The tool authorisation pipeline. Nine stages, all of which may only refuse.

    schema → ownership → state → authorization → policy
           → rate limit → idempotency → single-flight → (execute) → result

The ordering is cost-ascending and information-descending: a malformed proposal
is rejected before an ownership row is read, and "you may not do that here" is
answered before any check that would reveal whether the thing exists.

Nothing in this module can *widen* what a tool may do. Every stage returns a
`Refusal` or nothing. That is what makes "the model has no authority" a
structural property: the model chooses a name and some arguments, and every
other decision is made here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from demo_command_center.contracts.ownership import Ownership
from demo_command_center.orchestration.tools import (
    Refusal,
    SideEffect,
    ToolRefused,
    ToolSpec,
    get,
)
from demo_command_center.security.rate_limit import Decision, Limiter, LimitScope
from demo_command_center.security.signatures import idempotency_key
from demo_command_center.state.machine import StateSnapshot
from demo_command_center.state.states import can_send_automated_message
from demo_command_center.state.triggers import Actor

#: Rate limit per conversation for each side-effect level, per hour. Financial
#: is deliberately tiny: nobody legitimately creates six payment orders an hour.
_LIMITS: dict[SideEffect, tuple[LimitScope, int, int]] = {
    SideEffect.READ_ONLY: (LimitScope.CONVERSATION, 120, 3600),
    SideEffect.LOCAL_WRITE: (LimitScope.CONVERSATION, 60, 3600),
    SideEffect.CUSTOMER_VISIBLE: (LimitScope.WHATSAPP_SEND, 20, 3600),
    SideEffect.EXTERNAL_BOOKING: (LimitScope.CONVERSATION, 12, 3600),
    SideEffect.FINANCIAL: (LimitScope.PAYMENT_ORDER, 5, 3600),
}


@dataclass(frozen=True, slots=True)
class Proposal:
    """A requested tool call, whatever proposed it."""

    tool: str
    arguments: dict[str, Any]
    actor: Actor
    conversation_ref: str
    #: Set when a model produced this. Purely for audit and metrics — it changes
    #: no decision, because a model proposal and a deterministic one are held to
    #: exactly the same checks.
    proposed_by_model: bool = False


@dataclass(frozen=True, slots=True)
class Authorised:
    """A proposal that survived every stage. The only thing an executor takes."""

    spec: ToolSpec
    arguments: dict[str, Any]
    proposal: Proposal
    idempotency_key: str
    audit: dict[str, Any] = field(default_factory=dict)


class AuthorisationPipeline:
    def __init__(self, limiter: Limiter) -> None:
        self._limiter = limiter
        #: Conversations with an exclusive tool currently in flight. Per
        #: container — the durable guarantee is the database's unique index;
        #: this simply avoids paying for the round trip to discover a conflict.
        self._in_flight: set[tuple[str, str]] = set()

    async def authorise(
        self,
        proposal: Proposal,
        *,
        snapshot: StateSnapshot,
        ownership: Ownership,
        now: datetime,
        already_executed: bool = False,
        policy_refusal: str | None = None,
    ) -> Authorised:
        """Run every stage. Raises `ToolRefused` at the first failure."""
        spec = get(proposal.tool)

        # 1. schema
        self._check_schema(spec, proposal.arguments)

        # 2. ownership — we may not act on a conversation we do not hold
        if not ownership.held_by_us(now=now):
            raise ToolRefused(spec.name, Refusal.NOT_OWNER, ownership.owner.value)

        # 3. state
        if spec.allowed_states and snapshot.state not in spec.allowed_states:
            raise ToolRefused(spec.name, Refusal.WRONG_STATE, snapshot.state.value)
        if spec.side_effect is SideEffect.CUSTOMER_VISIBLE and not can_send_automated_message(
            snapshot.state
        ):
            raise ToolRefused(spec.name, Refusal.WRONG_STATE, snapshot.state.value)

        # 4. authorization
        if proposal.actor not in spec.allowed_actors:
            raise ToolRefused(spec.name, Refusal.ACTOR_NOT_PERMITTED, proposal.actor.value)

        # 5. business policy — supplied by the caller, which owns the rules
        if policy_refusal:
            raise ToolRefused(spec.name, Refusal.POLICY_DENIED, policy_refusal)

        # 6. rate limit
        decision = await self._rate_limit(spec, proposal.conversation_ref)
        if not decision.allowed:
            raise ToolRefused(
                spec.name, Refusal.RATE_LIMITED, f"retry_in={decision.retry_after_seconds}s"
            )

        # 7. idempotency
        key = self.key_for(proposal)
        if already_executed:
            raise ToolRefused(spec.name, Refusal.ALREADY_EXECUTED, key[:16])

        # 8. single-flight for exclusive tools
        if spec.exclusive:
            lock = (proposal.conversation_ref, spec.name)
            if lock in self._in_flight:
                raise ToolRefused(spec.name, Refusal.CONCURRENT_EXCLUSIVE)
            self._in_flight.add(lock)

        return Authorised(
            spec=spec,
            arguments=proposal.arguments,
            proposal=proposal,
            idempotency_key=key,
            audit={
                field_name: proposal.arguments.get(field_name) for field_name in spec.audit_fields
            }
            | {"proposed_by_model": proposal.proposed_by_model},
        )

    def release(self, authorised: Authorised) -> None:
        """Clear the single-flight lock. Always run in a `finally`."""
        if authorised.spec.exclusive:
            self._in_flight.discard((authorised.proposal.conversation_ref, authorised.spec.name))

    @staticmethod
    def key_for(proposal: Proposal) -> str:
        """Deterministic per (conversation, tool, arguments).

        Hashed, so a phone number in an argument never becomes part of a key
        that is logged and indexed.
        """
        parts = [proposal.conversation_ref, proposal.tool]
        parts.extend(f"{k}={proposal.arguments[k]}" for k in sorted(proposal.arguments))
        return idempotency_key(*parts)

    @staticmethod
    def validate_result(spec: ToolSpec, result: dict[str, Any]) -> None:
        """A provider returning something unexpected must not become state."""
        if not spec.result_schema:
            return
        required = spec.result_schema.get("required", [])
        missing = [name for name in required if not result.get(name)]
        if missing:
            raise ToolRefused(spec.name, Refusal.RESULT_INVALID, f"missing={missing}")

    # ------------------------------------------------------------- internals
    @staticmethod
    def _check_schema(spec: ToolSpec, arguments: dict[str, Any]) -> None:
        """Enough JSON Schema to be the boundary, without a validator dependency.

        Checks the three things that actually matter at this boundary: no extra
        keys, no missing required keys, and every enum/length constraint held.
        A full JSON Schema engine would be a dependency in every Lambda for
        rules these five lines already enforce on the shapes we define.
        """
        schema = spec.input_schema
        properties: dict[str, Any] = schema.get("properties", {})

        extra = set(arguments) - set(properties)
        if extra:
            raise ToolRefused(spec.name, Refusal.SCHEMA_INVALID, f"unexpected={sorted(extra)}")

        missing = [name for name in schema.get("required", []) if name not in arguments]
        if missing:
            raise ToolRefused(spec.name, Refusal.SCHEMA_INVALID, f"missing={missing}")

        for name, value in arguments.items():
            rule = properties[name]
            expected = rule.get("type")
            if expected == "string":
                if not isinstance(value, str):
                    raise ToolRefused(spec.name, Refusal.SCHEMA_INVALID, f"{name}: not a string")
                if len(value) > int(rule.get("maxLength", 4096)):
                    raise ToolRefused(spec.name, Refusal.SCHEMA_INVALID, f"{name}: too long")
            elif expected == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ToolRefused(spec.name, Refusal.SCHEMA_INVALID, f"{name}: not an integer")
                if (
                    not int(rule.get("minimum", -(2**31)))
                    <= value
                    <= int(rule.get("maximum", 2**31))
                ):
                    raise ToolRefused(spec.name, Refusal.SCHEMA_INVALID, f"{name}: out of range")
            choices = rule.get("enum")
            if choices is not None and value not in choices:
                raise ToolRefused(spec.name, Refusal.SCHEMA_INVALID, f"{name}: not permitted")

    async def _rate_limit(self, spec: ToolSpec, conversation_ref: str) -> Decision:
        scope, limit, per_seconds = _LIMITS[spec.side_effect]
        return await self._limiter.check(
            scope, conversation_ref, limit=limit, per_seconds=per_seconds
        )
