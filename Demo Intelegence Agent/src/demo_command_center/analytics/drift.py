"""Drift evaluation. Detects, reports, and never auto-deploys.

The last line is the important one. A drift detector that automatically swaps in
a refitted model or a new discount policy is a system that can change what it
charges customers without anyone reviewing it. Everything here produces a
`DriftFinding` for a human; nothing here promotes anything.

Every finding carries the four things that make it actionable and the absence of
which makes an alert ignorable: sample size, comparison window, baseline, and
observed value. A "conversion is down" with none of those is noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class DriftKind(StrEnum):
    FORECAST_CALIBRATION = "forecast_calibration"
    REGIONAL_CONVERSION = "regional_conversion"
    TUTOR_SEGMENT = "tutor_segment"
    NO_SHOW_RATE = "no_show_rate"
    FEATURE_DISTRIBUTION = "feature_distribution"
    PROMPT_SCHEMA_FAILURE = "prompt_schema_failure"
    PROVIDER_LATENCY = "provider_latency"
    COST_PER_DEMO = "cost_per_demo"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class DriftFinding:
    """One detected drift. Advisory — it changes nothing on its own."""

    kind: DriftKind
    severity: Severity
    scope: str
    baseline: float
    observed: float
    delta: float
    sample_size: int
    window_start: datetime
    window_end: datetime
    evidence: tuple[str, ...] = ()
    #: What a human should consider. Never executed automatically.
    recommended_review: str = ""

    @property
    def relative_change(self) -> float:
        return round(self.delta / self.baseline, 4) if self.baseline else 0.0

    def as_row(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "scope": self.scope,
            "baseline": round(self.baseline, 6),
            "observed": round(self.observed, 6),
            "delta": round(self.delta, 6),
            "relative_change": self.relative_change,
            "sample_size": self.sample_size,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "evidence": list(self.evidence),
            "recommended_review": self.recommended_review,
            "auto_applied": False,
        }


@dataclass(frozen=True, slots=True)
class DriftThresholds:
    """All from policy, none hardcoded in a check."""

    min_sample_size: int = 30
    calibration_delta: float = 0.12
    distribution_delta: float = 0.2
    conversion_delta: float = 0.25
    no_show_delta: float = 0.15
    latency_multiplier: float = 2.0
    cost_multiplier: float = 1.5
    schema_failure_rate: float = 0.05


class DriftEvaluator:
    """Pure. Takes numbers, returns findings. No I/O, no clock read."""

    def __init__(self, thresholds: DriftThresholds | None = None) -> None:
        self._t = thresholds or DriftThresholds()

    def _window(self, now: datetime, days: int = 7) -> tuple[datetime, datetime]:
        return now - timedelta(days=days), now

    def calibration(
        self,
        *,
        predicted: list[float],
        observed: list[bool],
        now: datetime,
        scope: str = "global",
    ) -> DriftFinding | None:
        """Mean predicted probability vs the actual conversion rate.

        A model predicting 0.7 on a cohort that converts at 0.3 is not slightly
        off — every downstream decision keyed on the risk band is wrong.
        """
        n = min(len(predicted), len(observed))
        if n < self._t.min_sample_size:
            return None

        mean_predicted = sum(predicted[:n]) / n
        actual = sum(1 for hit in observed[:n] if hit) / n
        delta = abs(mean_predicted - actual)
        if delta <= self._t.calibration_delta:
            return None

        start, end = self._window(now)
        return DriftFinding(
            kind=DriftKind.FORECAST_CALIBRATION,
            severity=Severity.CRITICAL
            if delta > self._t.calibration_delta * 2
            else Severity.WARNING,
            scope=scope,
            baseline=round(actual, 4),
            observed=round(mean_predicted, 4),
            delta=round(delta, 4),
            sample_size=n,
            window_start=start,
            window_end=end,
            evidence=(
                f"mean predicted {mean_predicted:.3f} vs observed {actual:.3f} over {n} demos",
            ),
            recommended_review=(
                "Refit the forecast coefficients and publish forecast.v2 after review. "
                "Do not change the live policy automatically."
            ),
        )

    def rate_shift(
        self,
        kind: DriftKind,
        *,
        baseline: float,
        observed: float,
        sample_size: int,
        now: datetime,
        scope: str,
        threshold: float | None = None,
    ) -> DriftFinding | None:
        """A generic rate comparison against a rolling baseline."""
        if sample_size < self._t.min_sample_size:
            return None
        if baseline <= 0:
            return None

        relative = abs(observed - baseline) / baseline
        limit = threshold if threshold is not None else self._t.conversion_delta
        if relative <= limit:
            return None

        start, end = self._window(now)
        return DriftFinding(
            kind=kind,
            severity=Severity.WARNING if relative < limit * 2 else Severity.CRITICAL,
            scope=scope,
            baseline=baseline,
            observed=observed,
            delta=observed - baseline,
            sample_size=sample_size,
            window_start=start,
            window_end=end,
            evidence=(f"{relative:.1%} change against a {sample_size}-demo baseline",),
            recommended_review="Investigate before changing policy. No automatic action taken.",
        )

    def feature_distribution(
        self,
        *,
        feature: str,
        baseline: list[float],
        current: list[float],
        now: datetime,
    ) -> DriftFinding | None:
        """Population Stability Index over a feature's distribution.

        PSI rather than a mean comparison: a feature whose mean is unchanged but
        whose *shape* has split into two modes is drifting, and a mean check
        cannot see it.
        """
        if len(current) < self._t.min_sample_size or len(baseline) < self._t.min_sample_size:
            return None

        psi = _population_stability_index(baseline, current)
        if psi <= self._t.distribution_delta:
            return None

        start, end = self._window(now, days=28)
        return DriftFinding(
            kind=DriftKind.FEATURE_DISTRIBUTION,
            severity=Severity.WARNING if psi < 0.4 else Severity.CRITICAL,
            scope=feature,
            baseline=0.0,
            observed=round(psi, 4),
            delta=round(psi, 4),
            sample_size=len(current),
            window_start=start,
            window_end=end,
            evidence=(f"PSI {psi:.3f} over {len(current)} observations",),
            recommended_review=(
                f"The input distribution for {feature} has moved. Re-examine the feature builder."
            ),
        )

    def schema_failures(
        self, *, attempts: int, failures: int, now: datetime, purpose: str
    ) -> DriftFinding | None:
        """Structured-output failures. A rising rate means a prompt or a model
        changed under us — usually a silent provider-side model update."""
        if attempts < self._t.min_sample_size:
            return None
        rate = failures / attempts
        if rate <= self._t.schema_failure_rate:
            return None

        start, end = self._window(now, days=1)
        return DriftFinding(
            kind=DriftKind.PROMPT_SCHEMA_FAILURE,
            severity=Severity.CRITICAL,
            scope=purpose,
            baseline=self._t.schema_failure_rate,
            observed=round(rate, 4),
            delta=round(rate - self._t.schema_failure_rate, 4),
            sample_size=attempts,
            window_start=start,
            window_end=end,
            evidence=(f"{failures}/{attempts} structured outputs failed validation",),
            recommended_review=(
                "Check the pinned model version and the prompt. Do not auto-update either."
            ),
        )

    def cost_per_demo(
        self, *, baseline_micros: int, observed_micros: int, demos: int, now: datetime
    ) -> DriftFinding | None:
        if demos < self._t.min_sample_size or baseline_micros <= 0:
            return None
        ratio = observed_micros / baseline_micros
        if ratio <= self._t.cost_multiplier:
            return None

        start, end = self._window(now)
        return DriftFinding(
            kind=DriftKind.COST_PER_DEMO,
            severity=Severity.WARNING,
            scope="llm",
            baseline=float(baseline_micros),
            observed=float(observed_micros),
            delta=float(observed_micros - baseline_micros),
            sample_size=demos,
            window_start=start,
            window_end=end,
            evidence=(f"{ratio:.2f}x the baseline cost per completed demo",),
            recommended_review="Check model routing and retry rates before raising the budget.",
        )


def _population_stability_index(
    baseline: list[float], current: list[float], *, bins: int = 10
) -> float:
    """PSI across equal-width bins over the combined range.

    Both distributions are floored at a small epsilon so an empty bin produces
    a large-but-finite contribution rather than `inf`, which would make every
    comparison with a missing bucket look maximally drifted.
    """
    combined = baseline + current
    low, high = min(combined), max(combined)
    if math.isclose(low, high):
        return 0.0

    width = (high - low) / bins
    epsilon = 1e-6

    def histogram(values: list[float]) -> list[float]:
        counts = [0] * bins
        for value in values:
            index = min(int((value - low) / width), bins - 1)
            counts[index] += 1
        total = len(values) or 1
        return [max(count / total, epsilon) for count in counts]

    expected = histogram(baseline)
    actual = histogram(current)
    return sum((a - e) * math.log(a / e) for e, a in zip(expected, actual, strict=True))


#: Every drift finding is advisory. Asserted by a test so that a future
#: "auto-remediate" flag cannot be added without deleting this.
AUTO_APPLY_ENABLED = False
