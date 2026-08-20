"""Feature construction for the conversion forecast.

The twelve feature names here are pinned to `config/policies/forecast.v1.yaml`
and nothing else. `tests/unit/test_forecast.py` asserts the two sets are equal,
because a feature the policy weights but the builder never produces is not a
loud failure — it silently falls back to its default on every scoring run, and
the model quietly degrades to its intercept.

Every feature is standardised to roughly [-1, 1] and centred on a platform mean,
which is the scale the coefficients were set on.

Absent inputs return `None`, never 0.0. Zero is a *centred* value meaning
"exactly average", which is a claim. `None` means "we do not know", and the
scorer records it as missing and lowers confidence. Conflating the two gives a
demo with no data at all a confident average prediction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from demo_command_center.contracts.common import DemoMode, DemoOutcome
from demo_command_center.domain.demo import Demo
from demo_command_center.domain.objections import ObjectionAnalysisV1, ObjectionCategory

#: Platform means the features are centred on. Population descriptions rather
#: than business thresholds, which is why they live with the builder and not in
#: the policy file.
PLATFORM_CONVERSION_RATE = 0.30
PLATFORM_NO_SHOW_RATE = 0.08
PLATFORM_REPLY_MINUTES = 30.0
PLATFORM_NEGOTIATION_TURNS = 3.0
PLATFORM_LEAD_TO_DEMO_DAYS = 3.0

#: The names the policy weights. Kept as a constant so the equality test has
#: something to compare against without parsing YAML twice.
FEATURE_NAMES: tuple[str, ...] = (
    "tutor_historical_conversion",
    "tutor_no_show_rate",
    "student_response_latency",
    "scheduling_friction",
    "reschedule_count",
    "lead_to_demo_days",
    "prior_qualification_score",
    "demo_outcome_positive",
    "objection_price_only",
    "objection_severity",
    "followup_engagement",
    "mode_online",
)


@dataclass(frozen=True, slots=True)
class FeatureInputs:
    """Raw, un-standardised inputs. Optional by design — most are unknown
    pre-demo, and the model is scored both before and after."""

    tutor_conversion_rate: float | None = None
    tutor_no_show_rate: float | None = None
    median_reply_minutes: float | None = None
    negotiation_turns: int | None = None
    reschedule_count: int = 0
    lead_to_demo_days: float | None = None
    #: Lead Intake's own qualification signal, 0..1, when a handoff supplied one.
    qualification_score: float | None = None
    outcome: DemoOutcome = DemoOutcome.UNKNOWN
    replied_to_followup: bool | None = None
    mode: DemoMode = DemoMode.ONLINE
    #: Labelled demos behind this tutor/region segment. Gates confidence only.
    sample_size: int = 0

    # Post-demo extras used for the quality score.
    demo_duration_minutes: int | None = None
    reminder_count: int = 0
    student_messages_after_demo: int = 0


def build(
    inputs: FeatureInputs,
    *,
    analysis: ObjectionAnalysisV1 | None = None,
    demo: Demo | None = None,
    now: datetime | None = None,
) -> dict[str, float | None]:
    """Standardised features keyed exactly by `FEATURE_NAMES`."""
    mode = demo.mode if demo is not None else inputs.mode

    return {
        "tutor_historical_conversion": _centre(
            inputs.tutor_conversion_rate, PLATFORM_CONVERSION_RATE, spread=0.30
        ),
        "tutor_no_show_rate": _centre(
            inputs.tutor_no_show_rate, PLATFORM_NO_SHOW_RATE, spread=0.15
        ),
        "student_response_latency": _log_centre(
            inputs.median_reply_minutes, PLATFORM_REPLY_MINUTES
        ),
        "scheduling_friction": _centre(
            None if inputs.negotiation_turns is None else float(inputs.negotiation_turns),
            PLATFORM_NEGOTIATION_TURNS,
            spread=3.0,
        ),
        "reschedule_count": _bounded_count(inputs.reschedule_count, cap=3),
        "lead_to_demo_days": _log_centre(inputs.lead_to_demo_days, PLATFORM_LEAD_TO_DEMO_DAYS),
        "prior_qualification_score": (
            None
            if inputs.qualification_score is None
            # 0..1 → -1..1, so an unqualified lead is a genuine negative rather
            # than merely a small positive.
            else max(-1.0, min(1.0, inputs.qualification_score * 2.0 - 1.0))
        ),
        "demo_outcome_positive": _outcome_signal(inputs.outcome),
        "objection_price_only": _price_only(analysis),
        "objection_severity": _severity(analysis),
        "followup_engagement": (
            None if inputs.replied_to_followup is None else float(inputs.replied_to_followup)
        ),
        "mode_online": 1.0 if mode is DemoMode.ONLINE else 0.0,
    }


# ------------------------------------------------------------------ scaling


def _centre(value: float | None, mean: float, *, spread: float) -> float | None:
    if value is None:
        return None
    return max(-1.5, min(1.5, (value - mean) / spread))


def _log_centre(value: float | None, mean: float) -> float | None:
    """Log-scaled: 2 minutes vs 20 matters far more than 2 days vs 2.5."""
    if value is None or value <= 0:
        return None
    return max(-1.5, min(1.5, math.log(value / mean) / math.log(10)))


def _bounded_count(count: int, *, cap: int) -> float:
    """Counts saturate. 5 vs 9 reschedules is nothing; 0 vs 2 is everything."""
    return min(max(count, 0), cap) / cap


def _outcome_signal(outcome: DemoOutcome) -> float | None:
    """A demo that did not happen has no outcome signal — None, not -1."""
    return {
        DemoOutcome.POSITIVE: 1.0,
        DemoOutcome.NEUTRAL: 0.0,
        DemoOutcome.NEGATIVE: -1.0,
        DemoOutcome.NOT_HELD: None,
        DemoOutcome.UNKNOWN: None,
    }[outcome]


# --------------------------------------------------------------- objections


def _price_only(analysis: ObjectionAnalysisV1 | None) -> float | None:
    """1.0 when price is the *only* objection.

    Mildly positive by design (weight +0.25): intent exists and the blocker is
    one we can actually address, unlike a tutor-fit objection.
    """
    if analysis is None:
        return None
    categories = analysis.categories()
    if not categories:
        return 0.0
    return 1.0 if categories == {ObjectionCategory.PRICE} else 0.0


def _severity(analysis: ObjectionAnalysisV1 | None) -> float | None:
    """Distinct objection categories, scaled by /4 as the policy describes."""
    if analysis is None:
        return None
    return _bounded_count(len(analysis.categories()), cap=4)


# ------------------------------------------------------------ demo quality


def demo_quality(
    inputs: FeatureInputs,
    *,
    analysis: ObjectionAnalysisV1 | None = None,
    scheduled_minutes: int = 45,
) -> float:
    """A 0..1 score for the demo *itself*, deliberately not the forecast.

    A demo can be excellent and still not convert (wrong budget), and a regional
    ops view has to tell those apart before it starts retraining tutors for what
    is actually a pricing problem.
    """
    signals: list[float] = []

    if inputs.demo_duration_minutes is not None and scheduled_minutes > 0:
        signals.append(min(1.0, inputs.demo_duration_minutes / scheduled_minutes))
    outcome = _outcome_signal(inputs.outcome)
    if outcome is not None:
        signals.append((outcome + 1.0) / 2.0)
    if analysis is not None:
        signals.append(1.0 - _bounded_count(len(analysis.categories()), cap=4))
    signals.append(_bounded_count(inputs.student_messages_after_demo, cap=5))

    return round(sum(signals) / len(signals), 4) if signals else 0.0
