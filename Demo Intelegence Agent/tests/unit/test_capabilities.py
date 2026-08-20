"""The eight capabilities' business rules, tested without infrastructure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from demo_command_center.bootstrap import (
    discount_policy,
    forecast_model,
    monitoring_policy,
    reminder_policy,
)
from demo_command_center.capabilities.discounts.service import DiscountCapability
from demo_command_center.capabilities.forecasting import features as feature_builder
from demo_command_center.capabilities.forecasting.service import (
    ForecastCapability,
    RiskBand,
    Strategy,
)
from demo_command_center.capabilities.monitoring.service import (
    MonitoringCapability,
    NotAuthorisedForRegion,
    RegionalMetrics,
)
from demo_command_center.capabilities.reminders.service import ReminderCapability
from demo_command_center.capabilities.scheduling import timeparse
from demo_command_center.capabilities.scheduling.ranking import (
    W_DAYPART,
    W_PREFERENCE,
    W_SOONER,
    W_WEEKDAY,
    rank_slots,
)
from demo_command_center.contracts.common import (
    Confidence,
    DemoMode,
    DemoOutcome,
    Evidence,
    Party,
)
from demo_command_center.domain.demo import Demo, DemoAttendee
from demo_command_center.domain.objections import (
    ObjectionAnalysisV1,
    ObjectionCategory,
    ObjectionItem,
    verify_quotes,
)
from demo_command_center.domain.pricing import DenialReason, DiscountStatus, PlanQuote
from demo_command_center.domain.reminders import ReminderStatus, in_quiet_hours
from demo_command_center.domain.slots import TimeSlot
from demo_command_center.shared.clock import FrozenClock
from demo_command_center.storage.memory.commerce import InMemoryOperationsRepository
from demo_command_center.storage.memory.demos import InMemoryDemoRepository

NOW = datetime(2026, 3, 10, 4, 30, tzinfo=UTC)  # Tuesday 10:00 IST
LIST_PRICE = 480_000


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


def analysis_with(*items: ObjectionItem) -> ObjectionAnalysisV1:
    return ObjectionAnalysisV1(
        demo_id="dmo_1",
        conversation_ref="cv_1",
        objections=items,
        analysed_at=NOW,
    )


def explicit(category: ObjectionCategory, quote: str = "it is too expensive") -> ObjectionItem:
    return ObjectionItem(
        category=category, evidence=Evidence.EXPLICIT, quote=quote, confidence=Confidence.HIGH
    )


def inferred(category: ObjectionCategory) -> ObjectionItem:
    return ObjectionItem(
        category=category,
        evidence=Evidence.INFERRED,
        rationale="tone suggested hesitation",
        confidence=Confidence.LOW,
    )


# =====================================================  025  scheduling


class TestTimeInterpretation:
    def test_tomorrow_evening_resolves_to_a_weekday_evening_ist(self) -> None:
        guess = timeparse.interpret("kal shaam 6 baje", now=NOW, timezone="Asia/Kolkata")
        assert guess.slot is not None
        local = guess.slot.local_start()
        assert (local.hour, local.minute) == (18, 0)
        assert local.date() == (
            NOW.astimezone(guess.slot.starts_at.tzinfo).date() + timedelta(days=1)
        )

    @pytest.mark.parametrize(
        ("text", "hour"),
        [
            ("tomorrow 6pm", 18),
            ("tomorrow at 6", 18),
            ("kal 6 baje", 18),
            ("tomorrow morning", 9),
            ("kal subah", 9),
            ("tomorrow 10am", 10),
            ("tomorrow 7:30 pm", 19),
        ],
    )
    def test_common_phrasings_all_land_on_the_intended_hour(self, text: str, hour: int) -> None:
        guess = timeparse.interpret(text, now=NOW, timezone="Asia/Kolkata")
        assert guess.slot is not None, text
        assert guess.slot.local_start().hour == hour, text

    def test_a_bare_hour_for_a_tuition_demo_means_evening(self) -> None:
        """ "6" means 6pm. Booking a demo at 06:00 is never what a parent meant."""
        guess = timeparse.interpret("tomorrow 6", now=NOW, timezone="Asia/Kolkata")
        assert guess.slot is not None
        assert guess.slot.local_start().hour == 18

    def test_class_ten_is_not_ten_oclock(self) -> None:
        """The misparse that books a demo for a class number."""
        guess = timeparse.interpret("my son is in class 10", now=NOW, timezone="Asia/Kolkata")
        assert guess.slot is None

    def test_a_time_today_that_has_passed_rolls_to_tomorrow(self) -> None:
        evening = datetime(2026, 3, 10, 14, 0, tzinfo=UTC)  # 19:30 IST
        guess = timeparse.interpret("6pm", now=evening, timezone="Asia/Kolkata")
        assert guess.slot is not None
        assert guess.slot.starts_at > evening

    def test_a_day_with_no_time_reports_the_missing_half(self) -> None:
        guess = timeparse.interpret("how about tomorrow", now=NOW, timezone="Asia/Kolkata")
        assert guess.slot is None
        assert guess.reason == "time_missing"
        assert guess.partial_day is not None

    def test_a_slot_beyond_the_horizon_is_refused(self) -> None:
        guess = timeparse.interpret("15/09", now=NOW, timezone="Asia/Kolkata")
        assert guess.slot is None or guess.slot.bookable_from(now=NOW) is None

    def test_an_unknown_timezone_fails_loudly(self) -> None:
        """A silent UTC fallback books an Indian parent's 6pm at 23:30 local."""
        with pytest.raises(ValueError, match="unknown IANA timezone"):
            timeparse.interpret("tomorrow 6pm", now=NOW, timezone="Mars/Olympus")

    def test_the_confirmation_label_always_names_the_zone(self) -> None:
        guess = timeparse.interpret("tomorrow 6pm", now=NOW, timezone="Asia/Kolkata")
        assert guess.slot is not None
        assert "IST" in timeparse.describe_window(guess.slot)


class TestSlotRanking:
    def test_weights_sum_to_one(self) -> None:
        assert W_PREFERENCE + W_SOONER + W_DAYPART + W_WEEKDAY == pytest.approx(1.0)

    def test_the_requested_time_ranks_first(self) -> None:
        preferred = TimeSlot(starts_at=NOW + timedelta(days=1, hours=8))
        available = [
            TimeSlot(starts_at=NOW + timedelta(days=1, hours=hours)) for hours in (2, 8, 30)
        ]
        ranked = rank_slots(
            available,
            preferred=preferred,
            timezone="Asia/Kolkata",
            duration_minutes=45,
            now=NOW,
            limit=3,
        )
        assert ranked[0].slot.starts_at == preferred.starts_at
        assert "matches_requested_time" in ranked[0].reason_codes

    def test_ranking_is_deterministic(self) -> None:
        available = [TimeSlot(starts_at=NOW + timedelta(days=d, hours=12)) for d in (1, 2, 3)]
        args = {
            "preferred": None,
            "timezone": "Asia/Kolkata",
            "duration_minutes": 45,
            "now": NOW,
            "limit": 3,
        }
        assert rank_slots(available, **args) == rank_slots(available, **args)  # type: ignore[arg-type]

    def test_ranks_are_dense_and_ordered(self) -> None:
        available = [TimeSlot(starts_at=NOW + timedelta(days=d, hours=12)) for d in (1, 2, 3, 4)]
        ranked = rank_slots(
            available,
            preferred=None,
            timezone="Asia/Kolkata",
            duration_minutes=45,
            now=NOW,
            limit=3,
        )
        assert [p.rank for p in ranked] == [1, 2, 3]
        assert ranked[0].score >= ranked[1].score >= ranked[2].score


# =====================================================  026  reminders


class TestReminders:
    def demo_at(self, offset: timedelta, *, revision: int = 1) -> Demo:
        return Demo(
            demo_id="dmo_r",
            conversation_ref="cv_r",
            request_id="req_r",
            tutor_ref="tut_r",
            mode=DemoMode.ONLINE,
            slot=TimeSlot(starts_at=NOW + offset, timezone="Asia/Kolkata"),
            calendar_event_id="evt_1",
            meet_url="https://meet.google.com/abc-defg-hij",
            attendees=(
                DemoAttendee(party=Party.STUDENT, ref="cv_r"),
                DemoAttendee(party=Party.TUTOR, ref="tut_r"),
            ),
            revision=revision,
            created_at=NOW,
            updated_at=NOW,
        )

    def capability(self, clock: FrozenClock, settings) -> ReminderCapability:  # type: ignore[no-untyped-def]
        return ReminderCapability(reminder_policy(settings), clock)

    def test_a_full_ladder_is_planned_for_both_audiences(self, clock, settings) -> None:  # type: ignore[no-untyped-def]
        planned = self.capability(clock, settings).plan(self.demo_at(timedelta(days=3)))
        labels = {r.label for r in planned}
        assert labels == {"t24h", "t2h", "t15m"}
        assert {r.audience for r in planned} == {Party.STUDENT, Party.TUTOR}

    def test_offsets_already_in_the_past_are_skipped(self, clock, settings) -> None:  # type: ignore[no-untyped-def]
        """A demo booked three hours out must not fire its T-24h immediately."""
        planned = self.capability(clock, settings).plan(self.demo_at(timedelta(hours=3)))
        assert "t24h" not in {r.label for r in planned}
        assert "t2h" in {r.label for r in planned}

    def test_the_per_demo_ceiling_is_enforced_across_the_whole_ladder(
        self, clock, settings
    ) -> None:  # type: ignore[no-untyped-def]
        policy = reminder_policy(settings)
        planned = self.capability(clock, settings).plan(self.demo_at(timedelta(days=3)))
        assert len(planned) <= policy.max_reminders_per_demo

    def test_each_reminder_has_a_stable_unique_idempotency_key(self, clock, settings) -> None:  # type: ignore[no-untyped-def]
        planned = self.capability(clock, settings).plan(self.demo_at(timedelta(days=3)))
        keys = [r.idempotency_key for r in planned]
        assert len(set(keys)) == len(keys)

    def test_a_reschedule_produces_different_keys(self, clock, settings) -> None:  # type: ignore[no-untyped-def]
        """The revision is in the key, so a new ladder is legitimately new."""
        first = self.capability(clock, settings).plan(self.demo_at(timedelta(days=3), revision=1))
        second = self.capability(clock, settings).plan(self.demo_at(timedelta(days=3), revision=2))
        assert {r.idempotency_key for r in first}.isdisjoint({r.idempotency_key for r in second})

    def test_a_reminder_from_an_older_revision_is_obsolete(self, clock, settings) -> None:  # type: ignore[no-untyped-def]
        capability = self.capability(clock, settings)
        demo_v1 = self.demo_at(timedelta(days=3), revision=1)
        planned = capability.plan(demo_v1)
        demo_v2 = self.demo_at(timedelta(days=3), revision=2)
        assert capability.obsolete(planned[0], demo_v2, now=NOW) == "superseded_by_reschedule"

    def test_a_cancelled_demo_suppresses_its_reminders(self, clock, settings) -> None:  # type: ignore[no-untyped-def]
        capability = self.capability(clock, settings)
        demo = self.demo_at(timedelta(days=3))
        planned = capability.plan(demo)
        cancelled = demo.model_copy(update={"cancelled_at": NOW})
        assert capability.obsolete(planned[0], cancelled, now=NOW) == "demo_cancelled"

    def test_quiet_hours_wrap_across_midnight(self) -> None:
        """`start=21, end=8` must mean 21:00-08:00, not 21:00-08:00 the same day."""
        late = datetime(2026, 3, 10, 19, 0, tzinfo=UTC)  # 00:30 IST
        assert in_quiet_hours(late, timezone="Asia/Kolkata", start_hour=21, end_hour=8)
        midday = datetime(2026, 3, 10, 7, 0, tzinfo=UTC)  # 12:30 IST
        assert not in_quiet_hours(midday, timezone="Asia/Kolkata", start_hour=21, end_hour=8)

    def test_no_reminder_is_scheduled_inside_quiet_hours(self, clock, settings) -> None:  # type: ignore[no-untyped-def]
        policy = reminder_policy(settings)
        # A demo at 02:00 IST would put T-24h and T-2h in the quiet window.
        demo = self.demo_at(timedelta(days=2, hours=20))
        for reminder in self.capability(clock, settings).plan(demo):
            assert not in_quiet_hours(
                reminder.fire_at,
                timezone="Asia/Kolkata",
                start_hour=policy.quiet_hours_start,
                end_hour=policy.quiet_hours_end,
            )

    def test_no_show_risk_names_its_signals(self, clock, settings) -> None:  # type: ignore[no-untyped-def]
        risk = self.capability(clock, settings).no_show_risk(
            demo=self.demo_at(timedelta(days=10)),
            reminders_sent=3,
            reminders_acknowledged=0,
            reschedule_count=2,
            minutes_since_last_inbound=60 * 60,
        )
        assert risk.score > 0.7
        assert "no_reminder_acknowledged" in risk.signals
        assert "repeated_reschedules" in risk.signals
        assert self.capability(clock, settings).should_escalate(risk)

    def test_an_engaged_parent_scores_low_risk(self, clock, settings) -> None:  # type: ignore[no-untyped-def]
        risk = self.capability(clock, settings).no_show_risk(
            demo=self.demo_at(timedelta(days=1)),
            reminders_sent=1,
            reminders_acknowledged=1,
            reschedule_count=0,
            minutes_since_last_inbound=5,
        )
        assert risk.band == "low"


# ==============================================  031  objection extraction


class TestObjections:
    def test_an_explicit_objection_requires_a_quote(self) -> None:
        with pytest.raises(ValueError, match="requires a quote"):
            ObjectionItem(category=ObjectionCategory.PRICE, evidence=Evidence.EXPLICIT)

    def test_an_inferred_objection_requires_a_rationale(self) -> None:
        with pytest.raises(ValueError, match="requires a rationale"):
            ObjectionItem(category=ObjectionCategory.PRICE, evidence=Evidence.INFERRED)

    def test_an_inferred_objection_is_never_quotable_to_a_customer(self) -> None:
        """It may inform strategy; it may not be echoed as something they said."""
        assert inferred(ObjectionCategory.PRICE).quotable_to_customer is False
        assert explicit(ObjectionCategory.PRICE).quotable_to_customer is True

    def test_a_low_confidence_explicit_objection_is_not_quotable(self) -> None:
        item = ObjectionItem(
            category=ObjectionCategory.PRICE,
            evidence=Evidence.EXPLICIT,
            quote="maybe too costly",
            confidence=Confidence.LOW,
        )
        assert item.quotable_to_customer is False

    def test_duplicate_categories_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate objection categories"):
            analysis_with(explicit(ObjectionCategory.PRICE), explicit(ObjectionCategory.PRICE))

    def test_a_fabricated_quote_is_detected(self) -> None:
        analysis = analysis_with(explicit(ObjectionCategory.PRICE, "your fees are outrageous"))
        assert verify_quotes(analysis, "The class was fine, thanks.") != ()

    def test_a_reflowed_quote_still_verifies(self) -> None:
        """A model that wraps a line has not fabricated the quote."""
        analysis = analysis_with(explicit(ObjectionCategory.PRICE, "it is  too\n expensive"))
        assert verify_quotes(analysis, "Parent: it is too expensive for us") == ()

    def test_explicit_only_filtering_excludes_inferences(self) -> None:
        analysis = analysis_with(
            explicit(ObjectionCategory.PRICE), inferred(ObjectionCategory.TIMING)
        )
        assert analysis.categories(explicit_only=True) == {ObjectionCategory.PRICE}
        assert len(analysis.categories()) == 2


# ==================================================  018  forecasting


class TestForecast:
    def test_every_policy_feature_is_produced_by_the_builder(self, settings) -> None:  # type: ignore[no-untyped-def]
        """A weighted feature the builder never emits silently defaults forever."""
        declared = {feature.name for feature in forecast_model(settings).features}
        assert declared == set(feature_builder.FEATURE_NAMES)

    def test_the_same_inputs_always_produce_the_same_score(self, settings) -> None:  # type: ignore[no-untyped-def]
        capability = ForecastCapability(forecast_model(settings))
        inputs = feature_builder.FeatureInputs(
            tutor_conversion_rate=0.45, outcome=DemoOutcome.POSITIVE, sample_size=100
        )
        args = {
            "demo_id": "dmo_1",
            "features": feature_builder.build(inputs),
            "sample_size": 100,
            "now": NOW,
        }
        assert capability.score(**args).probability == capability.score(**args).probability  # type: ignore[arg-type]

    def test_a_missing_feature_is_declared_not_silently_defaulted(self, settings) -> None:  # type: ignore[no-untyped-def]
        capability = ForecastCapability(forecast_model(settings))
        forecast = capability.score(
            demo_id="dmo_1",
            features=feature_builder.build(feature_builder.FeatureInputs()),
            sample_size=0,
            now=NOW,
        )
        assert forecast.missing_features
        assert forecast.confidence is Confidence.LOW
        assert not forecast.actionable

    def test_contributions_explain_the_score(self, settings) -> None:  # type: ignore[no-untyped-def]
        """ "Why is this 0.31" must be answerable without re-running anything."""
        capability = ForecastCapability(forecast_model(settings))
        forecast = capability.score(
            demo_id="dmo_1",
            features=feature_builder.build(
                feature_builder.FeatureInputs(tutor_conversion_rate=0.6, sample_size=200)
            ),
            sample_size=200,
            now=NOW,
        )
        assert forecast.contributions["tutor_historical_conversion"] > 0
        assert set(forecast.contributions) == set(feature_builder.FEATURE_NAMES)

    def test_a_better_tutor_raises_the_probability(self, settings) -> None:  # type: ignore[no-untyped-def]
        capability = ForecastCapability(forecast_model(settings))

        def score(rate: float) -> float:
            return capability.score(
                demo_id="d",
                features=feature_builder.build(
                    feature_builder.FeatureInputs(tutor_conversion_rate=rate, sample_size=200)
                ),
                sample_size=200,
                now=NOW,
            ).probability

        assert score(0.6) > score(0.1)

    def test_the_probability_is_always_a_probability(self, settings) -> None:  # type: ignore[no-untyped-def]
        """An extreme feature must saturate, never overflow."""
        capability = ForecastCapability(forecast_model(settings))
        extreme = dict.fromkeys(feature_builder.FEATURE_NAMES, 1e9)
        forecast = capability.score(demo_id="d", features=extreme, sample_size=500, now=NOW)
        assert 0.0 <= forecast.probability <= 1.0

    def test_low_conversion_probability_is_high_risk(self, settings) -> None:  # type: ignore[no-untyped-def]
        capability = ForecastCapability(forecast_model(settings))
        forecast = capability.score(
            demo_id="d",
            features=feature_builder.build(
                feature_builder.FeatureInputs(
                    tutor_conversion_rate=0.02,
                    tutor_no_show_rate=0.4,
                    outcome=DemoOutcome.NEGATIVE,
                    sample_size=200,
                )
            ),
            sample_size=200,
            now=NOW,
        )
        assert forecast.risk_band is RiskBand.HIGH

    def test_an_unanswered_objection_outranks_a_good_score(self, settings) -> None:  # type: ignore[no-untyped-def]
        """A fast close that fails for a reason we already knew is not a close."""
        capability = ForecastCapability(forecast_model(settings))
        forecast = capability.score(
            demo_id="d",
            features=feature_builder.build(
                feature_builder.FeatureInputs(tutor_conversion_rate=0.9, sample_size=500)
            ),
            sample_size=500,
            now=NOW,
            has_explicit_objection=True,
        )
        assert forecast.strategy is Strategy.ADDRESS_OBJECTIONS

    def test_a_tutor_fit_concern_routes_to_an_alternative_tutor(self, settings) -> None:  # type: ignore[no-untyped-def]
        capability = ForecastCapability(forecast_model(settings))
        forecast = capability.score(
            demo_id="d",
            features=feature_builder.build(feature_builder.FeatureInputs(sample_size=500)),
            sample_size=500,
            now=NOW,
            tutor_fit_concern=True,
        )
        assert forecast.strategy is Strategy.OFFER_ALTERNATIVE_TUTOR

    def test_the_policy_stamp_is_recorded_on_every_forecast(self, settings) -> None:  # type: ignore[no-untyped-def]
        capability = ForecastCapability(forecast_model(settings))
        forecast = capability.score(demo_id="d", features={}, sample_size=100, now=NOW)
        assert forecast.policy_stamp.startswith("forecast@v1#")


# ====================================================  034  discounts


class TestDiscounts:
    def quote(self) -> PlanQuote:
        return PlanQuote(
            plan_ref="plan_monthly",
            plan_name="Monthly",
            list_price_minor=LIST_PRICE,
            fetched_at=NOW,
        )

    def evaluate(self, settings, **overrides):  # type: ignore[no-untyped-def]
        args: dict[str, object] = {
            "conversation_ref": "cv_1",
            "demo_id": "dmo_1",
            "student_ref": "stu_1",
            "quote": self.quote(),
            "analysis": None,
            "outcome": DemoOutcome.POSITIVE,
            "prior_offers": 0,
            "repeat_requests": 0,
            "now": NOW,
        }
        args.update(overrides)
        return DiscountCapability(discount_policy(settings)).evaluate(**args)  # type: ignore[arg-type]

    def test_no_objection_means_no_discount(self, settings) -> None:  # type: ignore[no-untyped-def]
        decision = self.evaluate(settings)
        assert decision.status is DiscountStatus.NOT_APPLICABLE
        assert decision.percent == 0
        assert decision.reason_code is DenialReason.NO_QUALIFYING_OBJECTION

    def test_an_inferred_price_concern_alone_earns_nothing(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Paying out on a guess is discounting against a hallucination."""
        decision = self.evaluate(
            settings, analysis=analysis_with(inferred(ObjectionCategory.PRICE))
        )
        assert decision.percent == 0

    def test_an_explicit_price_objection_earns_the_price_band(self, settings) -> None:  # type: ignore[no-untyped-def]
        decision = self.evaluate(
            settings, analysis=analysis_with(explicit(ObjectionCategory.PRICE))
        )
        assert decision.band_name == "price_sensitive"
        assert decision.percent == 10
        assert decision.status is DiscountStatus.APPROVED

    def test_a_competitor_mention_escalates_rather_than_auto_approving(self, settings) -> None:  # type: ignore[no-untyped-def]
        decision = self.evaluate(
            settings,
            analysis=analysis_with(
                explicit(ObjectionCategory.PRICE),
                explicit(ObjectionCategory.COMPETITOR, "byjus offered less"),
            ),
        )
        assert decision.status is DiscountStatus.ESCALATED
        assert decision.requires_human_approval

    def test_no_decision_ever_exceeds_the_absolute_ceiling(self, settings) -> None:  # type: ignore[no-untyped-def]
        policy = discount_policy(settings)
        for category in ObjectionCategory:
            if category is ObjectionCategory.NONE:
                continue
            decision = self.evaluate(settings, analysis=analysis_with(explicit(category)))
            assert decision.percent <= policy.absolute_max_percent

    def test_no_decision_ever_breaches_the_price_floor(self, settings) -> None:  # type: ignore[no-untyped-def]
        for category in ObjectionCategory:
            if category is ObjectionCategory.NONE:
                continue
            decision = self.evaluate(settings, analysis=analysis_with(explicit(category)))
            assert decision.payable_minor >= decision.floor_minor

    def test_a_demo_that_never_happened_earns_nothing(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Discounting a no-show rewards no-shows."""
        decision = self.evaluate(
            settings,
            analysis=analysis_with(explicit(ObjectionCategory.PRICE)),
            outcome=DemoOutcome.NOT_HELD,
        )
        assert decision.status is DiscountStatus.DENIED
        assert decision.reason_code is DenialReason.DEMO_NOT_COMPLETED

    def test_the_offer_limit_blocks_a_third_offer(self, settings) -> None:  # type: ignore[no-untyped-def]
        decision = self.evaluate(
            settings, analysis=analysis_with(explicit(ObjectionCategory.PRICE)), prior_offers=2
        )
        assert decision.reason_code is DenialReason.OFFER_LIMIT_REACHED

    def test_repeated_asking_escalates_instead_of_raising_the_offer(self, settings) -> None:  # type: ignore[no-untyped-def]
        decision = self.evaluate(
            settings, analysis=analysis_with(explicit(ObjectionCategory.PRICE)), repeat_requests=3
        )
        assert decision.status is DiscountStatus.ESCALATED
        assert decision.percent == 0
        assert decision.reason_code is DenialReason.REPEAT_REQUESTS

    def test_the_policy_stamp_pins_the_exact_bytes(self, settings) -> None:  # type: ignore[no-untyped-def]
        assert self.evaluate(settings).policy_stamp.startswith("discount@v1#")

    def test_a_human_cannot_type_a_different_number(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Approval is yes/no on what the engine computed, not a free field."""
        capability = DiscountCapability(discount_policy(settings))
        decision = self.evaluate(
            settings,
            analysis=analysis_with(
                explicit(ObjectionCategory.PRICE),
                explicit(ObjectionCategory.COMPETITOR, "byjus offered less"),
            ),
        )
        approved = capability.approve_escalated(decision, approver="ops_1", now=NOW)
        assert approved.percent == decision.percent
        assert approved.approved_by == "ops_1"

    def test_approval_requires_an_identified_approver(self, settings) -> None:  # type: ignore[no-untyped-def]
        capability = DiscountCapability(discount_policy(settings))
        decision = self.evaluate(
            settings,
            analysis=analysis_with(
                explicit(ObjectionCategory.PRICE),
                explicit(ObjectionCategory.COMPETITOR, "byjus offered less"),
            ),
        )
        with pytest.raises(ValueError, match="identified approver"):
            capability.approve_escalated(decision, approver="  ", now=NOW)

    def test_a_zero_discount_decision_still_permits_payment(self, settings) -> None:  # type: ignore[no-untyped-def]
        """No discount is not no sale."""
        offer = self.evaluate(settings).approve(now=NOW)
        assert offer.amount_minor == LIST_PRICE
        assert offer.discount_percent == 0


# ==============================================  129  regional monitoring


class TestMonitoring:
    def capability(
        self, settings, operations: InMemoryOperationsRepository
    ) -> MonitoringCapability:  # type: ignore[no-untyped-def]
        return MonitoringCapability(
            policy=monitoring_policy(settings),
            demos=InMemoryDemoRepository(),
            operations=operations,
        )

    def metrics(self, **overrides: object) -> RegionalMetrics:
        base: dict[str, object] = {
            "region": "north",
            "window_start": NOW - timedelta(hours=24),
            "window_end": NOW,
            "completed": 30,
            "student_no_shows": 4,
            "tutor_no_shows": 0,
        }
        base.update(overrides)
        return RegionalMetrics(**base)  # type: ignore[arg-type]

    async def test_an_unauthorised_region_is_refused_server_side(self, settings) -> None:  # type: ignore[no-untyped-def]
        capability = self.capability(settings, InMemoryOperationsRepository())
        with pytest.raises(NotAuthorisedForRegion):
            await capability.rollup(region="south", authorised_regions=["north"], now=NOW)

    async def test_a_comparison_omits_regions_the_operator_cannot_see(self, settings) -> None:  # type: ignore[no-untyped-def]
        capability = self.capability(settings, InMemoryOperationsRepository())
        result = await capability.compare(
            regions=["north", "south"], authorised_regions=["north"], now=NOW
        )
        assert set(result) == {"north"}

    def test_rates_are_over_held_demos_not_over_every_row(self) -> None:
        """Dividing by the total would let cancellations lower the no-show rate."""
        metrics = self.metrics(cancelled=100)
        # Rates are rounded to 4dp on the way to a stored rollup row.
        assert metrics.student_no_show_rate == round(4 / 34, 4)

    async def test_a_small_sample_never_fires_an_alert(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Three no-shows out of four is not a regional trend."""
        capability = self.capability(settings, InMemoryOperationsRepository())
        alerts = await capability.evaluate(
            metrics=self.metrics(completed=1, student_no_shows=3), now=NOW
        )
        assert alerts == []

    async def test_an_absolute_rule_fires_above_its_threshold(self, settings) -> None:  # type: ignore[no-untyped-def]
        capability = self.capability(settings, InMemoryOperationsRepository())
        alerts = await capability.evaluate(
            metrics=self.metrics(completed=30, tutor_no_shows=10), now=NOW
        )
        assert any(alert.rule == "tutor_no_show_spike" for alert in alerts)

    async def test_the_cooldown_suppresses_a_repeat_alert(self, settings) -> None:  # type: ignore[no-untyped-def]
        operations = InMemoryOperationsRepository()
        capability = self.capability(settings, operations)
        metrics = self.metrics(completed=30, tutor_no_shows=10)
        first = await capability.evaluate(metrics=metrics, now=NOW)
        second = await capability.evaluate(metrics=metrics, now=NOW + timedelta(hours=1))
        assert first != []
        assert second == []

    async def test_the_cooldown_expires(self, settings) -> None:  # type: ignore[no-untyped-def]
        operations = InMemoryOperationsRepository()
        capability = self.capability(settings, operations)
        metrics = self.metrics(completed=30, tutor_no_shows=10)
        await capability.evaluate(metrics=metrics, now=NOW)
        later = await capability.evaluate(metrics=metrics, now=NOW + timedelta(hours=13))
        assert later != []

    async def test_a_relative_rule_needs_a_baseline_before_it_can_fire(self, settings) -> None:  # type: ignore[no-untyped-def]
        """A first window cannot be "worse than usual"."""
        capability = self.capability(settings, InMemoryOperationsRepository())
        alerts = await capability.evaluate(
            metrics=self.metrics(completed=30, student_no_shows=25), now=NOW
        )
        assert not any(alert.rule == "student_no_show_spike" for alert in alerts)


# ================================================  032  conversion


class TestConversion:
    def test_no_offer_means_no_deadline_and_no_urgency(self) -> None:
        """Manufactured scarcity is the easiest thing for a model to produce."""
        from demo_command_center.capabilities.conversion.service import (
            ConversionCapability,
            ConversionFacts,
        )

        composed = ConversionCapability().compose(
            facts=ConversionFacts(student_name="Asha", subject="Maths"),
            analysis=None,
            now=NOW,
        )
        assert "deadline" not in composed.used_facts
        for phrase in ("hurry", "last chance", "only", "expires"):
            assert phrase not in composed.body.lower()

    def test_only_explicit_objections_are_echoed_back(self) -> None:
        from demo_command_center.capabilities.conversion.service import (
            ConversionCapability,
            ConversionFacts,
        )

        composed = ConversionCapability().compose(
            facts=ConversionFacts(student_name="Asha"),
            analysis=analysis_with(inferred(ObjectionCategory.PRICE)),
            now=NOW,
        )
        assert "objection" not in composed.used_facts

    def test_a_rewrite_that_invents_a_number_is_caught(self) -> None:
        from demo_command_center.capabilities.conversion.service import (
            ComposedFollowup,
            rephrase_is_safe,
        )

        original = ComposedFollowup(body="The Monthly plan is ₹4,800.00.")
        assert rephrase_is_safe(original, "The Monthly plan is ₹4,800.00.") == ()
        invented = rephrase_is_safe(original, "Only 2 seats left at ₹3,900.00!")
        assert invented != ()


# ------------------------------------------------------------- reminders repo


async def test_replacing_a_ladder_cancels_the_previous_revision(clock, settings) -> None:  # type: ignore[no-untyped-def]
    """A parent who reschedules three times gets one ladder, not three."""
    from demo_command_center.storage.memory.demos import InMemoryReminderRepository

    repository = InMemoryReminderRepository()
    capability = ReminderCapability(reminder_policy(settings), clock)
    demo = Demo(
        demo_id="dmo_x",
        conversation_ref="cv_x",
        request_id="req_x",
        tutor_ref="tut_x",
        slot=TimeSlot(starts_at=NOW + timedelta(days=3), timezone="Asia/Kolkata"),
        calendar_event_id="evt_x",
        attendees=(DemoAttendee(party=Party.STUDENT, ref="cv_x"),),
        created_at=NOW,
        updated_at=NOW,
    )
    await repository.replace_for_demo(demo.demo_id, revision=1, reminders=capability.plan(demo))
    moved = demo.with_slot(
        TimeSlot(starts_at=NOW + timedelta(days=5), timezone="Asia/Kolkata"), now=NOW
    )
    await repository.replace_for_demo(moved.demo_id, revision=2, reminders=capability.plan(moved))

    rows = repository.all_rows()
    pending = [r for r in rows if r.status is ReminderStatus.PENDING]
    cancelled = [r for r in rows if r.status is ReminderStatus.CANCELLED]
    assert cancelled, "the old ladder must be cancelled"
    assert all(r.demo_revision == 2 for r in pending), "only the new revision stays pending"
