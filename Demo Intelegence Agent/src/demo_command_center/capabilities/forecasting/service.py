"""Capability 018 — Demo Success Forecast.

An interpretable logistic model whose coefficients live in versioned YAML. The
LLM is not involved and cannot be: `score()` takes a feature dict of floats and
returns arithmetic. That is the whole point — a probability a model invented is
not a probability, and every downstream decision (reminder intensity, discount
band eligibility, ops alerting) would inherit its noise.

Three properties make the output honest rather than merely confident:

* **Missing data is declared, not defaulted silently.** A feature that is
  absent uses the policy's neutral default *and* appears in `missing_features`,
  and enough missing features drop `confidence` to LOW.
* **Sample size gates confidence, not the score.** With fewer than
  `min_sample_size` labelled demos behind a segment the score is still computed
  — it is simply not something to act on alone.
* **Everything is versioned.** The stamp on a stored forecast names the exact
  policy bytes, so re-scoring a historical demo with today's model is visibly a
  different number rather than a silent revision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from demo_command_center.config.policies import ForecastModel
from demo_command_center.contracts.common import Confidence

#: Below this share of features present, the score is not actionable alone.
MIN_FEATURE_COVERAGE = 0.5


class RiskBand(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Strategy(StrEnum):
    """What the orchestrator should do with this forecast. A closed set."""

    FAST_CLOSE = "fast_close"
    NURTURE = "nurture"
    ADDRESS_OBJECTIONS = "address_objections"
    OFFER_ALTERNATIVE_TUTOR = "offer_alternative_tutor"
    HUMAN_OUTREACH = "human_outreach"
    DEPRIORITISE = "deprioritise"


@dataclass(frozen=True, slots=True)
class Forecast:
    """One scoring run. Persisted verbatim to `dcc_conversion_forecasts`."""

    demo_id: str
    probability: float
    risk_band: RiskBand
    confidence: Confidence
    strategy: Strategy
    #: Exactly what went in. Reproducing the score requires nothing else.
    features: dict[str, float]
    missing_features: tuple[str, ...]
    #: Per-feature contribution to the log-odds. This is the interpretability:
    #: "why is this 0.31" is answerable without re-running anything.
    contributions: dict[str, float]
    policy_stamp: str
    scored_at: datetime

    @property
    def actionable(self) -> bool:
        """Whether a decision may rest on this score alone."""
        return self.confidence is not Confidence.LOW

    def as_row(self) -> dict[str, Any]:
        return {
            "demo_id": self.demo_id,
            "probability": self.probability,
            "risk_band": self.risk_band.value,
            "confidence": self.confidence.value,
            "strategy": self.strategy.value,
            "features": self.features,
            "missing_features": list(self.missing_features),
            "contributions": self.contributions,
            "policy_stamp": self.policy_stamp,
            "scored_at": self.scored_at.isoformat(),
        }


class ForecastCapability:
    """Stateless scorer. One instance per container is fine."""

    def __init__(self, model: ForecastModel) -> None:
        self._model = model

    def score(
        self,
        *,
        demo_id: str,
        features: dict[str, float | None],
        sample_size: int,
        now: datetime,
        has_explicit_objection: bool = False,
        tutor_fit_concern: bool = False,
    ) -> Forecast:
        """Compute the probability. Pure arithmetic over declared features."""
        used: dict[str, float] = {}
        missing: list[str] = []
        contributions: dict[str, float] = {}
        log_odds = self._model.intercept

        for spec in self._model.features:
            raw = features.get(spec.name)
            if raw is None:
                missing.append(spec.name)
                value = spec.default
            else:
                # Clamped: the model was fitted on standardised features, and an
                # out-of-range value from a bad upstream would otherwise
                # dominate every other term.
                value = max(-3.0, min(3.0, float(raw)))
            used[spec.name] = value
            contribution = spec.weight * value
            contributions[spec.name] = round(contribution, 6)
            log_odds += contribution

        probability = _sigmoid(log_odds)
        coverage = 1.0 - (len(missing) / max(len(self._model.features), 1))
        confidence = self._confidence(coverage=coverage, sample_size=sample_size)
        band = self._band(probability)

        return Forecast(
            demo_id=demo_id,
            probability=round(probability, 4),
            risk_band=band,
            confidence=confidence,
            strategy=self._strategy(
                band=band,
                confidence=confidence,
                has_explicit_objection=has_explicit_objection,
                tutor_fit_concern=tutor_fit_concern,
            ),
            features=used,
            missing_features=tuple(missing),
            contributions=contributions,
            policy_stamp=self._model.stamp,
            scored_at=now,
        )

    # ------------------------------------------------------------- internals
    def _band(self, probability: float) -> RiskBand:
        """Note the inversion: a *low* conversion probability is *high* risk."""
        if probability >= self._model.high_risk_threshold:
            return RiskBand.LOW
        if probability >= self._model.low_risk_threshold:
            return RiskBand.MEDIUM
        return RiskBand.HIGH

    def _confidence(self, *, coverage: float, sample_size: int) -> Confidence:
        if coverage < MIN_FEATURE_COVERAGE or sample_size < self._model.min_sample_size:
            return Confidence.LOW
        if coverage < 0.85 or sample_size < self._model.min_sample_size * 3:
            return Confidence.MEDIUM
        return Confidence.HIGH

    @staticmethod
    def _strategy(
        *,
        band: RiskBand,
        confidence: Confidence,
        has_explicit_objection: bool,
        tutor_fit_concern: bool,
    ) -> Strategy:
        """Deterministic dispatch. Objections outrank the band on purpose.

        A high conversion probability with an unanswered explicit objection is
        not a fast close — it is a fast close that fails for a reason we already
        knew about and did not address.
        """
        if tutor_fit_concern:
            return Strategy.OFFER_ALTERNATIVE_TUTOR
        if has_explicit_objection:
            return Strategy.ADDRESS_OBJECTIONS
        if confidence is Confidence.LOW:
            # Not enough signal to sort into a lane. Keep it warm, do not spend.
            return Strategy.NURTURE
        if band is RiskBand.LOW:
            return Strategy.FAST_CLOSE
        if band is RiskBand.MEDIUM:
            return Strategy.NURTURE
        return Strategy.HUMAN_OUTREACH


def _sigmoid(x: float) -> float:
    """Overflow-safe logistic.

    `math.exp(710)` raises `OverflowError`; a runaway feature value would
    otherwise crash the scorer instead of saturating at 0 or 1, which is what a
    logistic function is supposed to do.
    """
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-min(x, 700.0)))
    exp_x = math.exp(max(x, -700.0))
    return exp_x / (1.0 + exp_x)
