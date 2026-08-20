"""Phase 2 controls: tool registry, authorisation pipeline, boundaries, budgets.

These test the controls added for hardening. The pattern throughout is that a
control is asserted as a *structural* property — the registry has no SQL tool,
the routing table has no shared table owner, no drift finding auto-applies —
so that removing the control fails a test rather than passing review.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from demo_command_center.analytics.drift import (
    AUTO_APPLY_ENABLED,
    DriftEvaluator,
    DriftKind,
)
from demo_command_center.cache.layers import (
    NEVER_CACHE,
    TTL_CEILINGS,
    CacheKey,
    L1Cache,
    NotCacheable,
    assert_cacheable,
)
from demo_command_center.contracts.ownership import Owner, Ownership
from demo_command_center.cost_control.budget import (
    FORBIDDEN_USES,
    Budget,
    BudgetExceeded,
    BudgetGuard,
    DailyCostCircuit,
    ForbiddenModelUse,
    Purpose,
    Tier,
    Usage,
    assert_not_forbidden,
    estimate_cost_micros,
)
from demo_command_center.glue.routing import (
    WRITES,
    BoundaryViolation,
    Capability,
    CapabilityEvent,
    Lane,
    assert_write_allowed,
    routing_invariants,
)
from demo_command_center.glue.saga import (
    SagaName,
    SagaRun,
    StepState,
    saga_invariants,
)
from demo_command_center.human_handoff.escalation import (
    EscalationTrigger,
    Severity,
    build_packet,
)
from demo_command_center.memory.context import ContextBuilder, ConversationMemory
from demo_command_center.orchestration.authorisation import (
    AuthorisationPipeline,
    Proposal,
)
from demo_command_center.orchestration.tools import (
    FORBIDDEN_TOOL_NAMES,
    TOOLS,
    Refusal,
    SideEffect,
    ToolRefused,
    model_facing_tools,
    registry_invariants,
)
from demo_command_center.resilience.errors import (
    Disposition,
    ErrorClass,
    RetryPolicy,
    classify,
    evaluate,
)
from demo_command_center.resilience.providers import (
    DEGRADATIONS,
    CircuitRegistry,
    Provider,
)
from demo_command_center.security.rate_limit import InProcessLimiter
from demo_command_center.shared.clock import FrozenClock
from demo_command_center.state.machine import StateSnapshot
from demo_command_center.state.states import DemoState
from demo_command_center.state.triggers import Actor

pytestmark = pytest.mark.security

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


# ============================================================ tool registry


class TestToolRegistry:
    def test_the_registry_is_structurally_sound(self) -> None:
        assert registry_invariants() == []

    def test_no_arbitrary_sql_http_or_shell_tool_exists(self) -> None:
        """Not blocked — absent. There is nothing to reach."""
        assert {tool.name for tool in TOOLS} & FORBIDDEN_TOOL_NAMES == set()

    def test_financial_tools_are_never_offered_to_the_model(self) -> None:
        offered = {tool.name for tool in model_facing_tools()}
        financial = {
            tool.name
            for tool in TOOLS
            if tool.side_effect in (SideEffect.FINANCIAL, SideEffect.EXTERNAL_BOOKING)
        }
        assert offered & financial == set()

    def test_a_user_cannot_trigger_a_financial_tool(self) -> None:
        for tool in TOOLS:
            if tool.side_effect is SideEffect.FINANCIAL:
                assert Actor.USER not in tool.allowed_actors, tool.name

    def test_tutor_selection_takes_an_ordinal_never_a_reference(self) -> None:
        """A reference in the arguments would bypass the snapshot lookup."""
        from demo_command_center.orchestration.tools import get

        properties = get("select_tutor_option").input_schema["properties"]
        assert set(properties) == {"ordinal"}
        assert "tutor_ref" not in properties

    def test_booking_and_payment_tools_are_single_flight(self) -> None:
        from demo_command_center.orchestration.tools import get

        for name in (
            "hold_slot",
            "create_calendar_event",
            "create_payment_order",
            "activate_subscription",
            "commit_discount",
        ):
            assert get(name).exclusive, name


# ==================================================== authorisation pipeline


class TestAuthorisation:
    def pipeline(self) -> AuthorisationPipeline:
        return AuthorisationPipeline(InProcessLimiter(FrozenClock(NOW)))

    def owned(self) -> Ownership:
        return Ownership(conversation_ref="cv_1", owner=Owner.DEMO_COMMAND_CENTER, since=NOW)

    def snapshot(self, state: DemoState) -> StateSnapshot:
        return StateSnapshot(conversation_ref="cv_1", state=state, version=1)

    async def authorise(self, tool: str, arguments: dict, **kwargs):  # type: ignore[no-untyped-def]
        return await self.pipeline().authorise(
            Proposal(
                tool=tool,
                arguments=arguments,
                actor=kwargs.pop("actor", Actor.USER),
                conversation_ref="cv_1",
                proposed_by_model=True,
            ),
            snapshot=self.snapshot(kwargs.pop("state", DemoState.AWAITING_TUTOR_SELECTION)),
            ownership=kwargs.pop("ownership", self.owned()),
            now=NOW,
            **kwargs,
        )

    async def test_a_valid_proposal_is_authorised(self) -> None:
        result = await self.authorise("select_tutor_option", {"ordinal": 1})
        assert result.spec.name == "select_tutor_option"
        assert result.idempotency_key

    async def test_an_unknown_tool_is_refused(self) -> None:
        with pytest.raises(ToolRefused) as exc:
            await self.authorise("execute_sql", {})
        assert exc.value.refusal is Refusal.UNKNOWN_TOOL

    async def test_an_extra_argument_is_refused(self) -> None:
        """`additionalProperties: false` is the boundary, not a suggestion."""
        with pytest.raises(ToolRefused) as exc:
            await self.authorise("select_tutor_option", {"ordinal": 1, "tutor_ref": "tut_x"})
        assert exc.value.refusal is Refusal.SCHEMA_INVALID

    async def test_an_out_of_range_ordinal_is_refused(self) -> None:
        with pytest.raises(ToolRefused) as exc:
            await self.authorise("select_tutor_option", {"ordinal": 99})
        assert exc.value.refusal is Refusal.SCHEMA_INVALID

    async def test_a_wrong_type_is_refused(self) -> None:
        with pytest.raises(ToolRefused) as exc:
            await self.authorise("select_tutor_option", {"ordinal": "one"})
        assert exc.value.refusal is Refusal.SCHEMA_INVALID

    async def test_a_conversation_we_do_not_own_refuses_everything(self) -> None:
        with pytest.raises(ToolRefused) as exc:
            await self.authorise(
                "select_tutor_option",
                {"ordinal": 1},
                ownership=Ownership(conversation_ref="cv_1", owner=Owner.HUMAN, since=NOW),
            )
        assert exc.value.refusal is Refusal.NOT_OWNER

    async def test_the_wrong_state_is_refused(self) -> None:
        with pytest.raises(ToolRefused) as exc:
            await self.authorise(
                "select_tutor_option", {"ordinal": 1}, state=DemoState.PAYMENT_PENDING
            )
        assert exc.value.refusal is Refusal.WRONG_STATE

    async def test_an_unpermitted_actor_is_refused(self) -> None:
        with pytest.raises(ToolRefused) as exc:
            await self.authorise(
                "activate_subscription",
                {"order_ref": "nxo_1"},
                actor=Actor.USER,
                state=DemoState.PAYMENT_CONFIRMED,
            )
        assert exc.value.refusal in (Refusal.ACTOR_NOT_PERMITTED, Refusal.WRONG_STATE)

    async def test_a_business_policy_refusal_stops_it(self) -> None:
        with pytest.raises(ToolRefused) as exc:
            await self.authorise(
                "select_tutor_option", {"ordinal": 1}, policy_refusal="tutor_unavailable"
            )
        assert exc.value.refusal is Refusal.POLICY_DENIED

    async def test_an_already_executed_key_is_refused(self) -> None:
        with pytest.raises(ToolRefused) as exc:
            await self.authorise("select_tutor_option", {"ordinal": 1}, already_executed=True)
        assert exc.value.refusal is Refusal.ALREADY_EXECUTED

    async def test_two_concurrent_exclusive_proposals_produce_one_refusal(self) -> None:
        """Single-flight: a second booking for one conversation cannot start."""
        pipeline = self.pipeline()
        proposal = Proposal(
            tool="hold_slot",
            arguments={"tutor_ref": "tut_1", "starts_at": "2026-03-11T12:30:00Z"},
            actor=Actor.SYSTEM,
            conversation_ref="cv_1",
        )
        common = {
            "snapshot": self.snapshot(DemoState.NEGOTIATING_SLOT),
            "ownership": self.owned(),
            "now": NOW,
        }
        await pipeline.authorise(proposal, **common)  # type: ignore[arg-type]
        with pytest.raises(ToolRefused) as exc:
            await pipeline.authorise(proposal, **common)  # type: ignore[arg-type]
        assert exc.value.refusal is Refusal.CONCURRENT_EXCLUSIVE

    async def test_the_idempotency_key_never_contains_a_raw_argument(self) -> None:
        key = AuthorisationPipeline.key_for(
            Proposal(
                tool="record_requirement",
                arguments={"field": "locality", "value": "9876543210"},
                actor=Actor.USER,
                conversation_ref="cv_1",
            )
        )
        assert "9876543210" not in key


# ================================================== glue: routing and sagas


class TestGlue:
    def test_the_routing_table_is_structurally_sound(self) -> None:
        assert routing_invariants() == []

    def test_no_table_has_two_writers(self) -> None:
        """Shared ownership is exactly what the boundary rule prevents."""
        seen: dict[str, Capability] = {}
        for capability, tables in WRITES.items():
            for table in tables:
                assert table not in seen, f"{table} written by two capabilities"
                seen[table] = capability

    def test_a_capability_cannot_write_another_aggregate(self) -> None:
        assert_write_allowed(Capability.SCHEDULING, "dcc_slot_holds")
        with pytest.raises(BoundaryViolation):
            assert_write_allowed(Capability.SCHEDULING, "dcc_payment_orders")
        with pytest.raises(BoundaryViolation):
            assert_write_allowed(Capability.REMINDERS, "dcc_demos")

    def test_payment_outranks_analytics(self) -> None:
        assert Lane.PAYMENT < Lane.ANALYTICS
        assert (
            CapabilityEvent(
                event=__import__(
                    "demo_command_center.contracts.events", fromlist=["DomainEvent"]
                ).DomainEvent.PAYMENT_CONFIRMED,
                capability=Capability.PAID_TRANSITION,
                conversation_ref="cv_1",
                idempotency_key="k",
            ).lane
            is Lane.PAYMENT
        )

    def test_conversation_work_is_ordered_and_analytics_is_not(self) -> None:
        from demo_command_center.contracts.events import DomainEvent

        booking = CapabilityEvent(
            event=DomainEvent.SLOT_HELD,
            capability=Capability.SCHEDULING,
            conversation_ref="cv_1",
            idempotency_key="k",
        )
        forecast = CapabilityEvent(
            event=DomainEvent.FORECAST_UPDATED,
            capability=Capability.FORECASTING,
            conversation_ref="cv_1",
            idempotency_key="k",
        )
        assert booking.message_group_id == "cv_1"
        # Grouping analytics by conversation would serialise a queue that has
        # no ordering requirement at all.
        assert forecast.message_group_id != "cv_1"

    def test_saga_definitions_are_sound(self) -> None:
        assert saga_invariants() == []

    def test_the_customer_visible_step_is_always_last(self) -> None:
        """Anything after it could fail and make an already-sent message a lie."""
        from demo_command_center.glue.saga import SAGAS

        for name, steps in SAGAS.items():
            visible = [i for i, s in enumerate(steps) if s.customer_visible]
            if visible:
                assert visible[0] == len(steps) - 1, name

    def test_a_booking_saga_compensates_in_reverse(self) -> None:
        run = SagaRun(
            saga=SagaName.BOOKING,
            conversation_ref="cv_1",
            correlation_key="k",
            started_at=NOW,
        )
        run.mark("hold_slot", StepState.DONE)
        run.mark("persist_intent", StepState.DONE)
        run.mark("create_calendar_event", StepState.FAILED)
        # The event must be cancelled before the hold is released, or the slot
        # frees up while an event still sits on it.
        assert run.compensations() == ("clear_intent", "release_slot_hold")
        assert not run.customer_was_told()

    def test_a_saga_knows_whether_the_customer_was_already_told(self) -> None:
        run = SagaRun(
            saga=SagaName.PAYMENT, conversation_ref="cv_1", correlation_key="k", started_at=NOW
        )
        assert not run.customer_was_told()
        for step in (
            "verify_webhook",
            "persist_payment_event",
            "activate_subscription",
            "handoff_to_onboarding",
            "send_welcome",
        ):
            run.mark(step, StepState.DONE)
        assert run.customer_was_told()
        assert run.complete


# ============================================================ error handling


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (
                __import__(
                    "demo_command_center.security.signatures", fromlist=["SignatureError"]
                ).SignatureError(
                    __import__(
                        "demo_command_center.security.signatures", fromlist=["SignatureFailure"]
                    ).SignatureFailure.SIGNATURE_MISMATCH
                ),
                ErrorClass.AUTHENTICATION,
            ),
            (ValueError("bad"), ErrorClass.VALIDATION),
            (RuntimeError("?"), ErrorClass.UNKNOWN),
        ],
    )
    def test_classification(self, error: BaseException, expected: ErrorClass) -> None:
        assert classify(error) is expected

    def test_an_amount_mismatch_is_never_transient(self) -> None:
        """Retrying it is a second chance to mis-process money."""
        from demo_command_center.domain.payments import (
            PaymentReconciliationError,
            ReconciliationFailure,
        )

        error = PaymentReconciliationError(ReconciliationFailure.AMOUNT_MISMATCH)
        outcome = evaluate(error, attempt=1, policy=RetryPolicy())
        assert outcome.error_class is ErrorClass.VALIDATION
        assert outcome.disposition is Disposition.TERMINAL_FAILURE

    def test_a_bad_credential_is_never_retried(self) -> None:
        from demo_command_center.contracts.ports import ProviderRejected

        outcome = evaluate(
            ProviderRejected("meta", "unauthorized", status_code=401),
            attempt=1,
            policy=RetryPolicy(),
        )
        assert outcome.error_class is ErrorClass.AUTHENTICATION
        assert outcome.disposition is Disposition.TERMINAL_FAILURE

    def test_a_timeout_is_retried_then_dead_lettered(self) -> None:
        from demo_command_center.contracts.ports import ProviderTimeout

        policy = RetryPolicy(max_attempts=2)
        error = ProviderTimeout("google", 10.0)
        assert evaluate(error, attempt=1, policy=policy).disposition is Disposition.RETRY
        assert evaluate(error, attempt=5, policy=policy).disposition is Disposition.DEAD_LETTER

    def test_a_conflict_goes_to_a_human(self) -> None:
        from demo_command_center.domain.slots import SlotConflict

        outcome = evaluate(SlotConflict("tut_1", NOW), attempt=1, policy=RetryPolicy())
        assert outcome.disposition is Disposition.HUMAN_REVIEW
        assert outcome.alarm_worthy

    def test_a_duplicate_is_a_success_and_never_alarms(self) -> None:
        from demo_command_center.resilience.errors import Outcome, disposition_for

        disposition = disposition_for(ErrorClass.DUPLICATE, attempt=1, policy=RetryPolicy())
        assert disposition is Disposition.SUCCESS
        assert not Outcome(ErrorClass.DUPLICATE, disposition, 1).alarm_worthy

    def test_backoff_is_jittered_and_capped(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, max_seconds=10.0, jitter=lambda: 1.0)
        assert policy.delay_for(0) == 1.0
        assert policy.delay_for(3) == 8.0
        assert policy.delay_for(10) == 10.0  # capped

    def test_full_jitter_can_return_zero(self) -> None:
        """Uniform in [0, ceiling]: equal backoff across a fleet re-synchronises."""
        policy = RetryPolicy(base_seconds=1.0, jitter=lambda: 0.0)
        assert policy.delay_for(5) == 0.0

    def test_a_provider_retry_after_wins_but_is_capped(self) -> None:
        policy = RetryPolicy(max_seconds=20.0)
        assert policy.delay_for(0, retry_after=5.0) == 5.0
        assert policy.delay_for(0, retry_after=3600.0) == 20.0

    def test_the_error_detail_never_carries_a_message_body(self) -> None:
        outcome = evaluate(ValueError("phone 9876543210 rejected"), attempt=1, policy=RetryPolicy())
        assert "9876543210" not in outcome.detail
        assert outcome.detail == "ValueError"


# =============================================================== resilience


class TestProviderResilience:
    def test_every_provider_declares_a_degradation(self) -> None:
        for provider in Provider:
            assert provider in DEGRADATIONS, provider

    def test_no_degradation_permits_a_fabricated_claim(self) -> None:
        for degradation in DEGRADATIONS.values():
            assert degradation.forbidden_claim, degradation.provider

    def test_google_being_down_never_confirms_a_meeting(self) -> None:
        degradation = DEGRADATIONS[Provider.GOOGLE_CALENDAR]
        assert not degradation.lifecycle_continues
        assert "confirmed" in degradation.forbidden_claim

    def test_openai_being_down_does_not_stop_the_lifecycle(self) -> None:
        assert DEGRADATIONS[Provider.OPENAI].lifecycle_continues

    def test_one_provider_failing_does_not_open_another_circuit(self) -> None:
        registry = CircuitRegistry(failure_threshold=2)
        for _ in range(3):
            registry.breaker(Provider.CASHFREE).record_failure()
        assert not registry.available(Provider.CASHFREE)
        assert registry.available(Provider.GOOGLE_CALENDAR)

    def test_a_kill_switch_behaves_exactly_like_an_open_circuit(self) -> None:
        registry = CircuitRegistry()
        registry.disable(Provider.OPENAI)
        assert not registry.available(Provider.OPENAI)
        registry.enable(Provider.OPENAI)
        assert registry.available(Provider.OPENAI)

    def test_lifecycle_blocking_providers_are_distinguished(self) -> None:
        """Paging the same way for OpenAI and Cashfree trains people to mute."""
        registry = CircuitRegistry(failure_threshold=1)
        registry.breaker(Provider.OPENAI).record_failure()
        assert Provider.OPENAI in registry.open_providers()
        assert Provider.OPENAI not in registry.lifecycle_blocked_by()


# ============================================================ cost controls


class TestCostControl:
    def guard(self) -> tuple[BudgetGuard, Usage]:
        return BudgetGuard(
            Budget(
                calls_per_turn=2,
                calls_per_conversation=5,
                reasoning_calls_per_conversation=1,
                tokens_per_conversation=1000,
            )
        ), Usage("cv_1")

    def test_calls_per_turn_is_enforced(self) -> None:
        guard, usage = self.guard()
        for _ in range(2):
            guard.check(usage, Purpose.INTENT_CLASSIFICATION)
            guard.record(
                usage,
                purpose=Purpose.INTENT_CLASSIFICATION,
                model_ref="m",
                prompt_version="v1",
                input_tokens=10,
                output_tokens=5,
                latency_ms=1.0,
                succeeded=True,
            )
        with pytest.raises(BudgetExceeded, match="calls_per_turn"):
            guard.check(usage, Purpose.INTENT_CLASSIFICATION)

    def test_a_new_turn_resets_the_per_turn_ceiling_but_not_the_total(self) -> None:
        guard, usage = self.guard()
        for _ in range(2):
            guard.record(
                usage,
                purpose=Purpose.INTENT_CLASSIFICATION,
                model_ref="m",
                prompt_version="v1",
                input_tokens=1,
                output_tokens=1,
                latency_ms=1.0,
                succeeded=True,
            )
        usage.start_turn()
        guard.check(usage, Purpose.INTENT_CLASSIFICATION)
        assert usage.calls_total == 2

    def test_the_expensive_tier_has_its_own_ceiling(self) -> None:
        guard, usage = self.guard()
        guard.record(
            usage,
            purpose=Purpose.OBJECTION_EXTRACTION,
            model_ref="m",
            prompt_version="v1",
            input_tokens=10,
            output_tokens=5,
            latency_ms=1.0,
            succeeded=True,
        )
        usage.start_turn()
        with pytest.raises(BudgetExceeded, match="reasoning_calls"):
            guard.check(usage, Purpose.OBJECTION_EXTRACTION)
        # A cheap call is still allowed after the reasoning budget is spent.
        guard.check(usage, Purpose.INTENT_CLASSIFICATION)

    def test_the_token_ceiling_is_enforced(self) -> None:
        guard, usage = self.guard()
        guard.record(
            usage,
            purpose=Purpose.REQUIREMENT_EXTRACTION,
            model_ref="m",
            prompt_version="v1",
            input_tokens=900,
            output_tokens=200,
            latency_ms=1.0,
            succeeded=True,
        )
        usage.start_turn()
        with pytest.raises(BudgetExceeded, match="tokens_per_conversation"):
            guard.check(usage, Purpose.REQUIREMENT_EXTRACTION)

    def test_only_objection_extraction_uses_the_expensive_tier(self) -> None:
        from demo_command_center.cost_control.budget import TIERS

        expensive = {p for p, tier in TIERS.items() if tier is Tier.REASONING}
        assert expensive == {Purpose.OBJECTION_EXTRACTION}

    @pytest.mark.parametrize("use", sorted(FORBIDDEN_USES))
    def test_forbidden_model_uses_are_refused(self, use: str) -> None:
        with pytest.raises(ForbiddenModelUse):
            assert_not_forbidden(use)

    def test_arithmetic_and_payment_validation_are_forbidden(self) -> None:
        assert "arithmetic" in FORBIDDEN_USES
        assert "payment_validation" in FORBIDDEN_USES
        assert "discount_amount" in FORBIDDEN_USES
        assert "state_transition" in FORBIDDEN_USES

    def test_every_usage_record_attributes_spend_to_a_capability(self) -> None:
        guard, usage = self.guard()
        record = guard.record(
            usage,
            purpose=Purpose.OBJECTION_EXTRACTION,
            model_ref="model-x",
            prompt_version="objections.v1",
            input_tokens=100,
            output_tokens=50,
            latency_ms=12.5,
            succeeded=True,
        )
        assert record["purpose"] == "objection_extraction"
        assert record["model_ref"] == "model-x"
        assert record["prompt_version"] == "objections.v1"

    def test_the_daily_circuit_trips_and_resets(self) -> None:
        circuit = DailyCostCircuit(daily_ceiling_micros=1000)
        circuit.spend(600, now=NOW)
        assert not circuit.tripped
        circuit.spend(600, now=NOW)
        assert circuit.tripped
        with pytest.raises(BudgetExceeded):
            circuit.assert_open()
        circuit.spend(1, now=NOW + timedelta(days=2))
        assert not circuit.tripped

    def test_cost_is_integer_arithmetic_from_configured_rates(self) -> None:
        """A float cost summed across a month drifts."""
        micros = estimate_cost_micros(
            input_tokens=1_000_000,
            output_tokens=500_000,
            input_per_million=150,
            output_per_million=600,
        )
        assert micros == 150 + 300
        assert isinstance(micros, int)


# ================================================================== caching


class TestCache:
    def test_authoritative_values_cannot_be_cached(self) -> None:
        for name in NEVER_CACHE:
            with pytest.raises(NotCacheable):
                assert_cacheable(name)

    def test_the_never_cache_list_covers_the_dangerous_values(self) -> None:
        assert {"tutor_availability", "payment_status", "conversation_ownership"} <= set(
            NEVER_CACHE
        )

    def test_authorization_has_the_shortest_ttl(self) -> None:
        """A revoked sub-admin keeping access is a bounded exposure only if
        the bound is small."""
        assert TTL_CEILINGS[CacheKey.REGION_AUTHORIZATION] <= 60

    def test_a_caller_cannot_exceed_a_kind_ttl_ceiling(self) -> None:
        clock = [0.0]
        cache = L1Cache(monotonic=lambda: clock[0])
        cache.put(CacheKey.REGION_AUTHORIZATION, "op_1", ["north"], ttl_seconds=99_999)
        clock[0] = TTL_CEILINGS[CacheKey.REGION_AUTHORIZATION] + 1
        assert cache.get(CacheKey.REGION_AUTHORIZATION, "op_1") is None

    def test_entries_expire(self) -> None:
        clock = [0.0]
        cache = L1Cache(monotonic=lambda: clock[0])
        cache.put(CacheKey.PLAN_CATALOGUE, "p", {"x": 1}, ttl_seconds=10)
        assert cache.get(CacheKey.PLAN_CATALOGUE, "p") is not None
        clock[0] = 11.0
        assert cache.get(CacheKey.PLAN_CATALOGUE, "p") is None

    def test_the_cache_is_bounded(self) -> None:
        """An unbounded dict in a warm container is a slow memory leak."""
        cache = L1Cache(max_entries=10)
        for index in range(50):
            cache.put(CacheKey.TUTOR_PUBLIC_PROFILE, f"t{index}", {"n": index})
        assert cache.stats()["entries"] == 10

    def test_invalidation_drops_a_whole_kind(self) -> None:
        cache = L1Cache()
        for index in range(3):
            cache.put(CacheKey.POLICY_DOCUMENT, f"p{index}", {"n": index})
        assert cache.invalidate(CacheKey.POLICY_DOCUMENT) == 3


# =================================================================== memory


class TestMemory:
    def test_financial_truth_is_never_only_prose(self) -> None:
        memory = ConversationMemory(conversation_ref="cv_1")
        assert "offer_percent" in memory.authoritative_refs
        assert "payment_state" in memory.authoritative_refs

    def test_summarising_cannot_touch_structured_state(self) -> None:
        memory = ConversationMemory(
            conversation_ref="cv_1", offer_percent=10, payment_state="pending"
        )
        memory.summarise_into("They seemed happy and agreed to everything.", now=NOW)
        assert memory.offer_percent == 10
        assert memory.payment_state == "pending"

    def test_remembered_turns_are_redacted_and_bounded(self) -> None:
        memory = ConversationMemory(conversation_ref="cv_1")
        for index in range(20):
            memory.remember(f"message {index} call me on 9876543210", now=NOW)
        assert len(memory.recent) <= 8
        assert all("9876543210" not in item for item in memory.recent)

    def test_the_context_builder_respects_its_budget(self) -> None:
        memory = ConversationMemory(conversation_ref="cv_1", state=DemoState.FOLLOWUP_PENDING)
        memory.summarise_into("x" * 5000, now=NOW)
        for index in range(8):
            memory.remember("y" * 400 + str(index), now=NOW)
        built = ContextBuilder(token_budget=200).build(memory)
        assert built.tokens <= 400  # core state can exceed; everything else drops
        assert built.dropped

    def test_core_state_is_never_dropped(self) -> None:
        memory = ConversationMemory(
            conversation_ref="cv_1",
            state=DemoState.PAYMENT_PENDING,
            tutor_name="Anaya",
            slot_label="Tue 6pm",
        )
        built = ContextBuilder(token_budget=1).build(memory)
        assert "state" in built.included

    def test_a_correction_invalidates_the_summary(self) -> None:
        memory = ConversationMemory(conversation_ref="cv_1")
        memory.summarise_into("Student is in class 10.", now=NOW)
        version = memory.summary_version
        memory.invalidate_summary(now=NOW)
        assert memory.summary == ""
        assert memory.summary_version > version


# ===================================================================== HITL


class TestHumanHandoff:
    def test_a_packet_is_not_a_transcript(self) -> None:
        assert not hasattr(
            build_packet(
                case_id="hc_1",
                conversation_ref="cv_1",
                trigger=EscalationTrigger.USER_REQUESTED_HUMAN,
                state=DemoState.COLLECTING_REQUIREMENTS,
                now=NOW,
                problem="asked for a person",
            ),
            "transcript",
        )

    def test_excerpts_are_capped(self) -> None:
        packet = build_packet(
            case_id="hc_1",
            conversation_ref="cv_1",
            trigger=EscalationTrigger.USER_REQUESTED_HUMAN,
            state=DemoState.NEW,
            now=NOW,
            problem="x",
            excerpts=tuple(f"message {i}" for i in range(50)),
        )
        assert len(packet.excerpts) <= 2

    def test_every_field_is_redacted(self) -> None:
        packet = build_packet(
            case_id="hc_1",
            conversation_ref="cv_1",
            trigger=EscalationTrigger.PAYMENT_MISMATCH,
            state=DemoState.PAYMENT_PENDING,
            now=NOW,
            problem="customer 9876543210 disputes the amount",
            evidence=("they emailed a@b.com",),
            excerpts=("call me on 9876543210",),
        )
        rendered = packet.render()
        assert "9876543210" not in rendered
        assert "a@b.com" not in rendered

    def test_money_triggers_are_always_critical(self) -> None:
        for trigger in (
            EscalationTrigger.PAYMENT_MISMATCH,
            EscalationTrigger.SUSPECTED_FRAUD,
            EscalationTrigger.ACTIVATION_INCONSISTENCY,
        ):
            packet = build_packet(
                case_id="hc",
                conversation_ref="cv",
                trigger=trigger,
                state=DemoState.PAYMENT_PENDING,
                now=NOW,
                problem="x",
                severity=Severity.NORMAL,  # a caller cannot lower it
            )
            assert packet.severity is Severity.CRITICAL, trigger

    def test_a_payment_packet_tells_the_operator_what_not_to_do(self) -> None:
        packet = build_packet(
            case_id="hc",
            conversation_ref="cv",
            trigger=EscalationTrigger.PAYMENT_MISMATCH,
            state=DemoState.PAYMENT_PENDING,
            now=NOW,
            problem="amounts differ",
        )
        assert packet.prohibited_actions
        assert any("manually" in item for item in packet.prohibited_actions)


# ==================================================================== drift


class TestDrift:
    def test_drift_never_auto_applies(self) -> None:
        """A detector that swaps in a refitted model changes prices unreviewed."""
        assert AUTO_APPLY_ENABLED is False

    def test_every_finding_records_that_it_was_not_applied(self) -> None:
        finding = DriftEvaluator().calibration(predicted=[0.9] * 50, observed=[False] * 50, now=NOW)
        assert finding is not None
        assert finding.as_row()["auto_applied"] is False

    def test_a_small_sample_never_produces_a_finding(self) -> None:
        assert (
            DriftEvaluator().calibration(predicted=[0.9] * 5, observed=[False] * 5, now=NOW) is None
        )

    def test_calibration_drift_is_detected(self) -> None:
        finding = DriftEvaluator().calibration(
            predicted=[0.8] * 100, observed=[False] * 70 + [True] * 30, now=NOW
        )
        assert finding is not None
        assert finding.kind is DriftKind.FORECAST_CALIBRATION
        assert finding.sample_size == 100
        assert finding.evidence

    def test_a_well_calibrated_model_produces_nothing(self) -> None:
        assert (
            DriftEvaluator().calibration(
                predicted=[0.3] * 100, observed=[True] * 30 + [False] * 70, now=NOW
            )
            is None
        )

    def test_distribution_drift_is_detected_when_the_mean_is_unchanged(self) -> None:
        """A bimodal split with the same mean is exactly what PSI catches."""
        baseline = [0.5] * 100
        current = [0.0] * 50 + [1.0] * 50
        finding = DriftEvaluator().feature_distribution(
            feature="tutor_historical_conversion", baseline=baseline, current=current, now=NOW
        )
        assert finding is not None
        assert finding.kind is DriftKind.FEATURE_DISTRIBUTION

    def test_schema_failure_drift_is_critical(self) -> None:
        finding = DriftEvaluator().schema_failures(
            attempts=100, failures=30, now=NOW, purpose="objection_extraction"
        )
        assert finding is not None
        assert finding.severity.value == "critical"
        assert "auto-update" in finding.recommended_review
