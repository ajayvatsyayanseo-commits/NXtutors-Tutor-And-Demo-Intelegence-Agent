"""`DemoCommandCenterOrchestrator` — the one conversation owner.

Every inbound event, whatever its source, takes the same path:

    dedupe → load state + ownership → assemble facts → fire the machine
           → persist under optimistic lock → execute the command
           → emit outbox events → send through the one boundary

The ordering is what makes the system safe rather than merely organised:

* **Dedupe before anything.** A redelivered Meta webhook must not re-run a
  capability, and certainly must not re-send.
* **Persist before executing.** The state change is committed first, so a crash
  mid-command leaves a state we can resume from rather than an executed side
  effect nobody recorded. Commands are therefore written to be idempotent.
* **Compensate on failure.** A failed command runs its declared compensation and
  fires `RECOVERABLE_FAILURE`; it never leaves a hold or a calendar event
  orphaned.

Capabilities return values. The orchestrator sends. That separation is why eight
capability Lambdas cannot produce eight simultaneous replies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from demo_command_center.contracts.ownership import SELF, Ownership, OwnershipError, unowned
from demo_command_center.guardrails.tutor_selection import resolve as resolve_selection
from demo_command_center.observability import metrics
from demo_command_center.observability.logging import bind, get_logger
from demo_command_center.orchestration.commands import CommandExecutor, CommandOutcome
from demo_command_center.orchestration.context import Dependencies, TurnContext, assemble_facts
from demo_command_center.security.signatures import idempotency_key
from demo_command_center.shared.ids import trace_id as new_trace_id
from demo_command_center.state.machine import (
    ConcurrencyConflict,
    StateSnapshot,
    TransitionRejected,
    TransitionResult,
)
from demo_command_center.state.transitions import Command
from demo_command_center.state.triggers import Actor, Trigger

logger = get_logger("orchestrator")

#: Optimistic-lock retries. Two conversations advancing the same row is rare;
#: three failures in a row means something is genuinely wrong, not contended.
MAX_CONFLICT_RETRIES = 3


@dataclass(frozen=True, slots=True)
class TurnResult:
    """What one event did. Returned to the handler and to the E2E harness."""

    conversation_ref: str
    state: str
    accepted: bool = False
    duplicate: bool = False
    rejected_reason: str = ""
    command: str = Command.NONE.value
    messages_sent: int = 0
    degraded: tuple[str, ...] = ()
    #: Text the caller should deliver, under `caller_sends` ownership only.
    reply_text: str | None = None


class DemoCommandCenterOrchestrator:
    def __init__(self, deps: Dependencies) -> None:
        self._d = deps
        self._commands = CommandExecutor(deps)

    # ------------------------------------------------------------- entry point
    async def handle(
        self,
        *,
        conversation_ref: str,
        trigger: Trigger,
        actor: Actor,
        event_id: str,
        payload: dict[str, Any] | None = None,
        trace_id: str | None = None,
        causation_id: str | None = None,
    ) -> TurnResult:
        """Process one event. The single entry point for every handler."""
        trace = trace_id or new_trace_id()
        bind(trace_id=trace, conversation_ref=conversation_ref, handler="orchestrator")

        dedup = idempotency_key("turn", conversation_ref, event_id, trigger.value)
        if not await self._d.idempotency.claim(
            dedup,
            scope="inbound",
            now=self._d.clock.now(),
            ttl_seconds=self._d.idempotency_ttl_seconds,
        ):
            metrics.emit(metrics.Metric.WEBHOOK_DUPLICATE, trigger=trigger.value)
            snapshot = await self._d.conversations.load(conversation_ref)
            return TurnResult(
                conversation_ref=conversation_ref,
                state=snapshot.state.value,
                accepted=True,
                duplicate=True,
            )

        last_error = ""
        for attempt in range(1, MAX_CONFLICT_RETRIES + 1):
            try:
                return await self._attempt(
                    conversation_ref=conversation_ref,
                    trigger=trigger,
                    actor=actor,
                    payload=payload or {},
                    trace=trace,
                    causation_id=causation_id,
                )
            except ConcurrencyConflict as exc:
                # Someone else advanced the row. Reload and re-decide — never
                # force the write, because the decision was made against a state
                # that no longer exists.
                last_error = str(exc)
                logger.info(
                    "optimistic lock conflict; retrying", extra={"dcc_attempt": str(attempt)}
                )

        snapshot = await self._d.conversations.load(conversation_ref)
        return TurnResult(
            conversation_ref=conversation_ref,
            state=snapshot.state.value,
            rejected_reason=f"concurrency_conflict:{last_error}"[:200],
        )

    # ---------------------------------------------------------------- one pass
    async def _attempt(
        self,
        *,
        conversation_ref: str,
        trigger: Trigger,
        actor: Actor,
        payload: dict[str, Any],
        trace: str,
        causation_id: str | None,
    ) -> TurnResult:
        now = self._d.clock.now()
        snapshot = await self._d.conversations.load(conversation_ref)
        ownership = await self._d.conversations.load_ownership(conversation_ref, now=now)

        ctx = TurnContext(
            conversation_ref=conversation_ref,
            trace_id=trace,
            correlation_id=conversation_ref,
            now=now,
            snapshot=snapshot,
            ownership=ownership,
            trigger=trigger,
            causation_id=causation_id,
        )
        await assemble_facts(self._d, ctx)
        # Payload facts are namespaced so a hostile inbound cannot overwrite a
        # guard input like `signature_verified` that the *system* established.
        ctx.merge(**{k: v for k, v in payload.items() if k in _PAYLOAD_FACT_ALLOWLIST})

        if trigger is Trigger.TUTOR_CHOSEN:
            await self._resolve_selection(ctx, payload)

        try:
            result = self._d.machine.fire(snapshot, trigger, actor=actor, facts=ctx.facts)
        except TransitionRejected as exc:
            metrics.emit(metrics.Metric.ILLEGAL_TRANSITION, reason=exc.reason[:40])
            logger.info(
                "transition rejected",
                extra={"dcc_reason": exc.reason, "dcc_state": snapshot.state.value},
            )
            await self._d.operations.audit(
                {
                    "conversation_ref": conversation_ref,
                    "event": "transition_rejected",
                    "state": snapshot.state.value,
                    "trigger": trigger.value,
                    "actor": actor.value,
                    "reason": exc.reason,
                    "occurred_at": now.isoformat(),
                }
            )
            return TurnResult(
                conversation_ref=conversation_ref,
                state=snapshot.state.value,
                rejected_reason=exc.reason,
            )

        await self._ensure_ownership(ctx, trigger=trigger)

        # Persist first. A crash after this point resumes; a crash after an
        # unpersisted side effect does not.
        updated = await self._d.conversations.save_transition(result, now=now, facts=ctx.facts)
        ctx.snapshot = updated
        metrics.emit(metrics.Metric.STATE_TRANSITION, to_state=result.to_state.value)

        outcome = await self._run_command(result, ctx, payload=payload)
        sent = await self._flush(ctx)
        await self._apply_transfer(ctx, outcome)

        return TurnResult(
            conversation_ref=conversation_ref,
            state=ctx.snapshot.state.value,
            accepted=True,
            command=result.command.value,
            messages_sent=sent,
            degraded=tuple(dict.fromkeys(ctx.degraded)),
            reply_text=outcome.reply_text,
            rejected_reason=outcome.failure_reason,
        )

    # ------------------------------------------------------------- sub-steps
    async def _resolve_selection(self, ctx: TurnContext, payload: dict[str, Any]) -> None:
        """Map what the parent said onto a stored candidate. Never the reverse.

        This is where the anti-tampering rule is enforced for every caller: the
        tutor reference is *looked up* in the snapshot we persisted when the
        options were presented. A `tutor_ref` appearing in the payload is
        ignored outright — accepting one would let a parent, or an injected
        instruction, name any tutor and have us try to book them.
        """
        candidates = await self._d.demos.load_candidates(ctx.conversation_ref)
        selection = resolve_selection(
            str(payload.get("text") or ""),
            candidates,
            button_id=str(payload.get("button_id") or "") or None,
        )
        ctx.merge(
            tutor_ref=selection.candidate.tutor_ref if selection.candidate else None,
            tutor_in_snapshot=selection.candidate is not None,
            selection_reason=selection.reason,
        )
        if not selection.resolved:
            logger.info("tutor selection unresolved", extra={"dcc_reason": selection.reason})

    async def _ensure_ownership(self, ctx: TurnContext, *, trigger: Trigger) -> None:
        """Take the conversation when a handoff arrives; assert it otherwise.

        Asserting on every turn rather than only on acquisition is what catches
        a worker resuming after a handoff to a human — the send would be blocked
        anyway, but failing here means we do not run the capability either.
        """
        if trigger is Trigger.HANDOFF_RECEIVED:
            taken = ctx.ownership.acquire(now=ctx.now)
            await self._d.conversations.save_ownership(taken)
            ctx.ownership = taken
            return
        if ctx.ownership.owner is not SELF:
            # Not an exception: a scheduler-driven trigger on a handed-off
            # conversation is normal, and the state machine already limited what
            # can happen. The send boundary is the enforcement point.
            ctx.degraded.append(f"not_owner:{ctx.ownership.owner.value}")

    async def _run_command(
        self, result: TransitionResult, ctx: TurnContext, *, payload: dict[str, Any]
    ) -> CommandOutcome:
        if result.command is Command.NONE:
            return CommandOutcome()
        try:
            outcome = await self._commands.execute(result.command, ctx, payload=payload)
        except Exception as exc:
            logger.exception(
                "command execution failed", extra={"dcc_command": result.command.value}
            )
            outcome = CommandOutcome(
                failed=True, failure_reason=f"command_error:{type(exc).__name__}"
            )

        if outcome.failed:
            await self._compensate(result, ctx)
            await self._fire_internal(ctx, Trigger.RECOVERABLE_FAILURE)
        return outcome

    async def _compensate(self, result: TransitionResult, ctx: TurnContext) -> None:
        if result.compensation is Command.NONE:
            return
        try:
            await self._commands.execute(result.compensation, ctx, payload={})
        except Exception:
            logger.exception(
                "compensation failed", extra={"dcc_command": result.compensation.value}
            )
            ctx.degraded.append(f"compensation_failed:{result.compensation.value}")

    async def _fire_internal(self, ctx: TurnContext, trigger: Trigger) -> None:
        """A system-actor follow-on transition inside the same turn.

        Used for the machine's own bookkeeping moves (failure, ownership,
        analysis chaining). Rejections are swallowed on purpose: a follow-on
        that is not legal from the current state simply does not happen.
        """
        snapshot = await self._d.conversations.load(ctx.conversation_ref)
        try:
            result = self._d.machine.fire(snapshot, trigger, actor=Actor.SYSTEM, facts=ctx.facts)
        except TransitionRejected:
            return
        ctx.snapshot = await self._d.conversations.save_transition(
            result, now=ctx.now, facts=ctx.facts
        )

    async def _apply_transfer(self, ctx: TurnContext, outcome: CommandOutcome) -> None:
        """Hand the conversation on, after everything we owed has been sent."""
        if outcome.transfer_to is None or ctx.ownership.owner is not SELF:
            return
        moved = ctx.ownership.transfer(outcome.transfer_to, now=ctx.now)
        await self._d.conversations.save_ownership(moved)
        ctx.ownership = moved
        logger.info("conversation transferred", extra={"dcc_to": outcome.transfer_to.value})

    async def _flush(self, ctx: TurnContext) -> int:
        """Send everything the capabilities queued, through the one boundary."""
        sent = 0
        for message in ctx.outbox:
            outcome = await self._d.outbound.send(message)
            if outcome.delivered:
                sent += 1
            elif outcome.suppressed:
                ctx.degraded.append(outcome.outcome.value)
        ctx.outbox.clear()
        return sent

    # ---------------------------------------------------------------- helpers
    async def snapshot(self, conversation_ref: str) -> StateSnapshot:
        return await self._d.conversations.load(conversation_ref)

    async def ownership(self, conversation_ref: str) -> Ownership:
        return await self._d.conversations.load_ownership(conversation_ref, now=self._d.clock.now())

    async def release(self, conversation_ref: str, *, to: Any) -> Ownership:
        """Transfer the conversation. Only the current owner may do this."""
        now = self._d.clock.now()
        current = await self._d.conversations.load_ownership(conversation_ref, now=now)
        if current.owner is not SELF:
            raise OwnershipError(f"cannot transfer: owned by {current.owner.value}")
        moved = current.transfer(to, now=now)
        await self._d.conversations.save_ownership(moved)
        return moved

    async def start(self, conversation_ref: str) -> Ownership:
        """Claim an unowned conversation — the direct-request entry point."""
        now = self._d.clock.now()
        current = await self._d.conversations.load_ownership(conversation_ref, now=now)
        if current.owner is SELF:
            return current
        taken = (current or unowned(conversation_ref, now=now)).acquire(now=now)
        await self._d.conversations.save_ownership(taken)
        return taken


#: Payload keys that may become guard facts. Anything else in an inbound payload
#: is data for a capability, never an input to an authorization decision.
_PAYLOAD_FACT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "signature_verified",
        "amount_matches_order",
        "order_belongs_to_conversation",
        "grace_period_elapsed",
        "evidence_source",
        "subscription_ref",
    }
)
