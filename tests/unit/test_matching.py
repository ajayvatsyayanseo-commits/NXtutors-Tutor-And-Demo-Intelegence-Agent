"""The eight evaluators, the hard filters, and the ranking combiner."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from tests.conftest import make_requirement
from tutor_match_meta.contracts.common import (
    DataQuality,
    Dimension,
    Freshness,
    Provenance,
    Tracked,
    TuitionMode,
)
from tutor_match_meta.contracts.requirement import BudgetBand, LocationRequirement
from tutor_match_meta.contracts.schedule import TimeWindow, Weekday, WeeklySchedule
from tutor_match_meta.contracts.tutor import (
    FeeBand,
    GeoPoint,
    ReviewAggregate,
    TutorCandidate,
    TutorCapabilities,
)
from tutor_match_meta.domain.identity import encode_public_ref
from tutor_match_meta.matching import default_evaluators
from tutor_match_meta.matching.base import EvaluationContext
from tutor_match_meta.matching.hard_filters import apply as apply_filters
from tutor_match_meta.scoring import combiner


def tutor(tutor_id: str = "T1", **kwargs: object) -> TutorCandidate:
    defaults: dict[str, object] = {
        "name": "Test Tutor",
        "freshness": Freshness.FRESH,
        "capabilities": TutorCapabilities(
            subjects=("Mathematics",),
            boards=("CBSE",),
            classes=("Class 10",),
            modes=(TuitionMode.HOME,),
        ),
    }
    defaults.update(kwargs)
    return TutorCandidate(
        tutor_id=tutor_id,
        public_ref=encode_public_ref(tutor_id),
        **defaults,  # type: ignore[arg-type]
    )


class TestSelfCorrection:
    """A parent who corrects themselves must be believed.

    `Tracked.beats` used to resolve an exact tie (same provenance, same
    confidence) in favour of the value already held. Two things the parent said
    are exactly that kind of tie, so the first one stuck forever: "class 9 —
    sorry, class 10" kept filtering on Class 9.
    """

    EARLIER = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    LATER = datetime(2026, 1, 1, 10, 1, tzinfo=UTC)

    def stated(self, value: str, at: datetime) -> Tracked:
        return Tracked(
            value=value, confidence=0.92, provenance=Provenance.DETERMINISTIC, observed_at=at
        )

    def test_the_later_of_two_equally_trusted_statements_wins(self) -> None:
        first = self.stated("Class 9", self.EARLIER)
        correction = self.stated("Class 10", self.LATER)
        assert correction.beats(first)
        assert not first.beats(correction)

    def test_a_model_guess_still_cannot_overwrite_the_parent(self) -> None:
        """The correction rule must not become a hole in the trust ranking:
        provenance is checked before recency, so a later, *more confident*
        model guess still loses to what the parent actually typed."""
        parent = self.stated("Class 9", self.EARLIER)
        guess = Tracked(
            value="Class 10",
            confidence=0.99,
            provenance=Provenance.LLM,
            observed_at=self.LATER,
        )
        assert not guess.beats(parent)

    def test_an_unstamped_value_does_not_displace_a_stamped_one(self) -> None:
        assert not Tracked(value="Class 10", confidence=0.92).beats(
            self.stated("Class 9", self.EARLIER)
        )

    def test_the_correction_survives_the_requirement_merge(self) -> None:
        first = make_requirement(student_class=None).model_copy(
            update={"student_class": self.stated("Class 9", self.EARLIER)}
        )
        second = make_requirement(student_class=None).model_copy(
            update={"student_class": self.stated("Class 10", self.LATER)}
        )
        merged = first.merged_with(second)
        assert merged.student_class is not None
        assert merged.student_class.value == "Class 10"


class TestEvaluatorContract:
    """Constraints every evaluator must satisfy — see matching/base.py."""

    def test_all_eight_skills_are_registered(self) -> None:
        assert set(default_evaluators()) == set(Dimension)

    def test_every_evaluator_is_total(self, context: EvaluationContext) -> None:
        """A near-empty tutor must produce a score, never an exception."""
        bare = TutorCandidate(
            tutor_id="EMPTY",
            public_ref=encode_public_ref("EMPTY"),
            name="X",
            freshness=Freshness.FRESH,
        )
        for dimension, evaluator in default_evaluators().items():
            score = evaluator.evaluate(bare, context)
            assert score.dimension is dimension
            assert 0.0 <= score.score <= 1.0

    def test_absent_data_is_never_quotable(self, context: EvaluationContext) -> None:
        bare = TutorCandidate(
            tutor_id="EMPTY",
            public_ref=encode_public_ref("EMPTY"),
            name="X",
            freshness=Freshness.FRESH,
        )
        for evaluator in default_evaluators().values():
            score = evaluator.evaluate(bare, context)
            if score.data_quality in {DataQuality.MISSING, DataQuality.INSUFFICIENT}:
                assert not score.quotable

    def test_evaluators_do_not_mutate_their_input(self, context: EvaluationContext) -> None:
        subject = tutor()
        snapshot = subject.model_dump()
        for evaluator in default_evaluators().values():
            evaluator.evaluate(subject, context)
        assert subject.model_dump() == snapshot


class TestAvailability:
    def test_absent_availability_reports_missing_not_unavailable(
        self, context: EvaluationContext
    ) -> None:
        score = default_evaluators()[Dimension.AVAILABILITY].evaluate(tutor(), context)
        assert score.data_quality is DataQuality.MISSING
        assert "availability_unknown" in score.flags
        assert not score.quotable

    def test_no_overlap_scores_zero_with_high_confidence(self, policy, now: datetime) -> None:
        requirement = make_requirement(
            preferred_schedule=WeeklySchedule(
                windows=(TimeWindow(weekday=Weekday.MON, start=time(18, 0), end=time(20, 0)),)
            )
        )
        subject = tutor(
            availability=WeeklySchedule(
                windows=(TimeWindow(weekday=Weekday.SAT, start=time(9, 0), end=time(11, 0)),)
            )
        )
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        score = default_evaluators()[Dimension.AVAILABILITY].evaluate(subject, context)
        assert score.score == 0.0
        assert score.confidence > 0.8

    def test_recorded_availability_never_scores_below_unknown(self, policy, now: datetime) -> None:
        """Regression: having availability data used to *cost* a tutor rank when
        the parent stated no timing preference."""
        requirement = make_requirement()  # no preferred_schedule
        with_data = tutor(
            "A",
            availability=WeeklySchedule(
                windows=(TimeWindow(weekday=Weekday.MON, start=time(17, 0), end=time(21, 0)),)
            ),
        )
        without_data = tutor("B")
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        evaluator = default_evaluators()[Dimension.AVAILABILITY]
        assert (
            evaluator.evaluate(with_data, context).score
            >= evaluator.evaluate(without_data, context).score
        )


class TestPerformance:
    def test_single_review_is_insufficient(self, context: EvaluationContext) -> None:
        subject = tutor(reviews=ReviewAggregate(count=1, rating_avg=5.0))
        score = default_evaluators()[Dimension.PERFORMANCE].evaluate(subject, context)
        assert score.data_quality is DataQuality.INSUFFICIENT
        assert not score.quotable

    def test_shrinkage_beats_a_lone_perfect_score(self, context: EvaluationContext) -> None:
        evaluator = default_evaluators()[Dimension.PERFORMANCE]
        many = tutor(
            "M", reviews=ReviewAggregate(count=40, rating_avg=4.6, latest_review_at=context.now)
        )
        one = tutor(
            "O", reviews=ReviewAggregate(count=1, rating_avg=5.0, latest_review_at=context.now)
        )
        assert evaluator.evaluate(many, context).score > evaluator.evaluate(one, context).score

    def test_old_reviews_are_marked_stale(self, context: EvaluationContext) -> None:
        subject = tutor(
            reviews=ReviewAggregate(
                count=25,
                rating_avg=4.9,
                latest_review_at=context.now - timedelta(days=1800),
            )
        )
        score = default_evaluators()[Dimension.PERFORMANCE].evaluate(subject, context)
        assert score.data_quality is DataQuality.STALE
        assert not score.quotable

    def test_no_reviews_falls_back_to_experience_honestly(self, context: EvaluationContext) -> None:
        subject = tutor(experience_years=10, reviews=ReviewAggregate(count=0))
        score = default_evaluators()[Dimension.PERFORMANCE].evaluate(subject, context)
        assert score.data_quality is DataQuality.INSUFFICIENT
        assert "no_review_evidence" in score.flags


class TestProximity:
    def test_online_makes_distance_irrelevant(self, policy, now: datetime) -> None:
        requirement = make_requirement(mode=TuitionMode.ONLINE)
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        score = default_evaluators()[Dimension.PROXIMITY].evaluate(tutor(city="Pune"), context)
        assert score.score == 1.0

    def test_unknown_location_is_missing_not_a_mismatch(self, context: EvaluationContext) -> None:
        """Regression: a blank `register.city` was scored as 'different city'."""
        score = default_evaluators()[Dimension.PROXIMITY].evaluate(
            tutor(city=None, pincode=None), context
        )
        assert score.data_quality is DataQuality.MISSING

    def test_closer_scores_higher(self, policy, now: datetime) -> None:
        requirement = make_requirement(
            location=LocationRequirement(city="Gurugram", latitude=28.4211, longitude=77.0490)
        )
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        evaluator = default_evaluators()[Dimension.PROXIMITY]
        near = tutor(
            "N",
            city="Gurugram",
            geo=GeoPoint(latitude=28.4243, longitude=77.0902, granularity="pincode"),
        )
        far = tutor(
            "F",
            city="Gurugram",
            geo=GeoPoint(latitude=28.6139, longitude=77.2090, granularity="pincode"),
        )
        assert evaluator.evaluate(near, context).score > evaluator.evaluate(far, context).score


class TestPersonality:
    def test_only_declared_style_evidence_is_used(self, policy, now: datetime) -> None:
        requirement = make_requirement(
            preferred_teaching_style=Tracked(
                value="need someone patient",
                confidence=0.9,
                provenance=Provenance.DETERMINISTIC,
            )
        )
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        patient = tutor("P", profile_summary="Patient and supportive teaching approach.")
        score = default_evaluators()[Dimension.PERSONALITY].evaluate(patient, context)
        assert score.score > 0.5

    def test_urgency_is_not_a_pace_preference(self, policy, now: datetime) -> None:
        """Regression: 'urgent' (start soon) was read as 'fast-paced teaching'
        and penalised patient tutors."""
        from tutor_match_meta.matching.personality.evidence import (
            StyleTrait,
            traits_from_request,
        )

        assert StyleTrait.FAST_PACED not in traits_from_request("urgent, need someone this week")

    def test_no_style_signals_reports_missing(self, policy, now: datetime) -> None:
        requirement = make_requirement()
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        score = default_evaluators()[Dimension.PERSONALITY].evaluate(tutor(), context)
        assert score.data_quality in {DataQuality.MISSING, DataQuality.INSUFFICIENT}


class TestNegotiation:
    def test_within_budget_scores_full(self, policy, now: datetime) -> None:
        requirement = make_requirement(budget=BudgetBand(minimum=800, maximum=1200))
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        subject = tutor(fee=FeeBand(minimum=900, maximum=1000, label="₹900–₹1,000"))
        score = default_evaluators()[Dimension.NEGOTIATION].evaluate(subject, context)
        assert score.score == pytest.approx(1.0)

    def test_gap_beyond_policy_scores_zero(self, policy, now: datetime) -> None:
        requirement = make_requirement(budget=BudgetBand(minimum=400, maximum=500))
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        subject = tutor(fee=FeeBand(minimum=3000, maximum=4000, label="₹3,000–₹4,000"))
        score = default_evaluators()[Dimension.NEGOTIATION].evaluate(subject, context)
        assert score.score == 0.0

    def test_strategy_comes_from_the_approved_set_only(self, policy, now: datetime) -> None:
        from tutor_match_meta.matching.negotiation import NegotiationEvaluator

        requirement = make_requirement(budget=BudgetBand(minimum=800, maximum=1000))
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        subject = tutor(fee=FeeBand(minimum=1080, maximum=1200, label="₹1,080–₹1,200"))
        strategy = NegotiationEvaluator().strategy_for(subject, context)
        assert strategy is not None
        assert strategy.code in {s.code for s in policy.negotiation.strategies}


class TestReplacementRisk:
    def test_is_never_quotable_to_a_parent(self, context: EvaluationContext) -> None:
        subject = tutor(reviews=ReviewAggregate(count=10, rating_avg=2.0, reliability_avg=2.0))
        score = default_evaluators()[Dimension.REPLACEMENT_RISK].evaluate(subject, context)
        from tutor_match_meta.contracts.common import INTERNAL_ONLY_DIMENSIONS

        assert Dimension.REPLACEMENT_RISK in INTERNAL_ONLY_DIMENSIONS
        assert score.score < 0.5

    def test_no_evidence_never_raises_an_alert(self, context: EvaluationContext) -> None:
        from tutor_match_meta.matching.replacement_risk import ReplacementRiskEvaluator

        evaluator = ReplacementRiskEvaluator()
        score = evaluator.evaluate(tutor(), context)
        assert score.data_quality is DataQuality.INSUFFICIENT
        assert evaluator.needs_backup(score) is False


class TestHardFilters:
    def test_wrong_subject_is_rejected(self, context: EvaluationContext) -> None:
        wrong = tutor(capabilities=TutorCapabilities(subjects=("Hindi",), classes=("Class 10",)))
        outcome = apply_filters([wrong], context)
        assert outcome.empty
        assert outcome.rejections[0].rule == "SUBJECT_SUPPORTED"

    def test_stale_projection_is_rejected(self, context: EvaluationContext) -> None:
        outcome = apply_filters([tutor(freshness=Freshness.STALE)], context)
        assert outcome.rejections[0].rule == "ACTIVE_TUTOR"

    def test_unknown_capability_is_not_a_rejection(self, context: EvaluationContext) -> None:
        """A blank column means unknown; filters reject contradiction only."""
        unknown = tutor(capabilities=TutorCapabilities())
        assert not apply_filters([unknown], context).empty

    def test_low_confidence_guess_cannot_empty_the_pool(self, policy, now: datetime) -> None:
        """An LLM guess informs scoring but must never delete candidates."""
        requirement = make_requirement(
            subject="Chemistry", provenance=Provenance.LLM, confidence=0.55
        )
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        maths_only = tutor(capabilities=TutorCapabilities(subjects=("Mathematics",)))
        assert not apply_filters([maths_only], context).empty

    def test_confident_statement_does_filter(self, policy, now: datetime) -> None:
        requirement = make_requirement(
            subject="Chemistry", provenance=Provenance.USER_CONFIRMED, confidence=1.0
        )
        context = EvaluationContext(requirement=requirement, policy=policy, now=now)
        maths_only = tutor(capabilities=TutorCapabilities(subjects=("Mathematics",)))
        assert apply_filters([maths_only], context).empty

    def test_gender_filters_only_when_requested(self, policy, now: datetime) -> None:
        male = tutor(gender="Male")
        neutral_ctx = EvaluationContext(requirement=make_requirement(), policy=policy, now=now)
        assert not apply_filters([male], neutral_ctx).empty

        requested = make_requirement(
            tutor_gender_preference=Tracked(
                value="female", confidence=1.0, provenance=Provenance.USER_CONFIRMED
            )
        )
        gendered_ctx = EvaluationContext(requirement=requested, policy=policy, now=now)
        assert apply_filters([male], gendered_ctx).empty


class TestCombiner:
    def test_missing_data_is_neutral_not_advantageous(self, policy, context) -> None:
        """Regression: averaging only known dimensions made ignorance look like
        excellence, ranking a 1-review tutor above a 40-review one."""
        evaluators = default_evaluators()
        rich = tutor(
            "RICH",
            experience_years=8,
            reviews=ReviewAggregate(
                count=40, rating_avg=4.6, reliability_avg=4.5, latest_review_at=context.now
            ),
            city="Gurugram",
            profile_summary="Patient and structured teaching.",
            fee=FeeBand(minimum=800, maximum=1000, label="₹800–₹1,000"),
        )
        thin = tutor("THIN", city="Gurugram")
        scored = [
            combiner.combine(
                tutor=t,
                pseudonym=f"c{i}",
                scores={d: e.evaluate(t, context) for d, e in evaluators.items()},
                policy=policy,
            )
            for i, t in enumerate((rich, thin))
        ]
        by_id = {c.tutor_id: c for c in scored}
        assert by_id["RICH"].final_score > by_id["THIN"].final_score
        assert by_id["RICH"].weight_coverage > by_id["THIN"].weight_coverage

    def test_ranking_is_deterministic(self, policy, context) -> None:
        evaluators = default_evaluators()
        candidates = [
            combiner.combine(
                tutor=t,
                pseudonym="c",
                scores={d: e.evaluate(t, context) for d, e in evaluators.items()},
                policy=policy,
            )
            for t in (tutor("A"), tutor("B"), tutor("C"))
        ]
        first = [c.tutor_id for c in combiner.rank(candidates, policy=policy, locality_of={})]
        second = [
            c.tutor_id
            for c in combiner.rank(list(reversed(candidates)), policy=policy, locality_of={})
        ]
        assert first == second

    def test_quality_bar_is_absolute(self, policy, context) -> None:
        """Never pad a shortlist to three with a candidate below the bar."""
        evaluators = default_evaluators()
        weak = combiner.combine(
            tutor=tutor("W"),
            pseudonym="c",
            scores={d: e.evaluate(tutor("W"), context) for d, e in evaluators.items()},
            policy=policy,
        )
        object.__setattr__(weak, "final_score", 0.10)
        assert combiner.shortlist_cutoff([weak], policy=policy) == []
