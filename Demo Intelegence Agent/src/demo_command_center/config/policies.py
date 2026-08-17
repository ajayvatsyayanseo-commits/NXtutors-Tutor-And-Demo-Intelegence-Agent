"""Versioned business policy loading.

Every number a business person might want to change — reminder offsets, discount
bands, forecast weights, underperformance thresholds — is a YAML document loaded
here, validated into a typed model, and **checksummed**. The checksum is stamped
onto every decision the policy produced, so six months later "why did this
customer get 15% off?" is answerable by looking up the exact policy bytes that
were live, rather than by reading today's source.

Policies are cached per process. A warm Lambda parses each file once; a changed
file requires a deployment, which is the point — a discount ceiling is not
something that should be hot-swappable.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolicyError(Exception):
    """A policy file is missing, malformed, or fails its own invariants."""


class PolicyDocument(BaseModel):
    """Common header on every policy file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str = Field(max_length=64)
    version: str = Field(max_length=16)
    description: str = ""
    #: Filled by the loader from the file bytes. Never written in the YAML.
    checksum: str = ""

    @property
    def stamp(self) -> str:
        """What gets written onto a decision row."""
        return f"{self.policy_id}@{self.version}#{self.checksum[:12]}"


# ---------------------------------------------------------------- reminders


class ReminderOffset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(max_length=32)
    #: Minutes before the demo start. Positive = before.
    minutes_before: Annotated[int, Field(ge=1, le=20_160)]
    audience: Literal["student", "tutor", "both"]
    #: The approved WhatsApp template. Free-form text is only possible inside
    #: the session window; a reminder at T-24h almost never is.
    template: str = Field(max_length=64)
    channel: Literal["whatsapp", "email", "whatsapp_then_email"] = "whatsapp"


class ReminderPolicy(PolicyDocument):
    offsets: tuple[ReminderOffset, ...]
    #: Hard ceiling per demo per audience, across reschedules. Without it, a
    #: parent who reschedules four times gets four full reminder ladders.
    max_reminders_per_demo: Annotated[int, Field(ge=1, le=20)] = 6
    max_reminders_per_identity_per_day: Annotated[int, Field(ge=1, le=50)] = 8
    #: Local-time hours during which we will not send. A T-24h reminder that
    #: lands at 02:40 is a complaint, not a nudge.
    quiet_hours_start: Annotated[int, Field(ge=0, le=23)] = 21
    quiet_hours_end: Annotated[int, Field(ge=0, le=23)] = 8
    #: A reminder whose send time falls in quiet hours is pushed to this hour
    #: rather than dropped — unless doing so would put it after the demo.
    quiet_hours_defer_to_hour: Annotated[int, Field(ge=0, le=23)] = 8
    #: Minutes of silence after a reminder before escalation is considered.
    silence_escalation_minutes: Annotated[int, Field(ge=5, le=1440)] = 120
    #: A demo scoring at or above this no-show risk gets a human nudge.
    no_show_risk_escalation_threshold: Annotated[float, Field(ge=0, le=1)] = 0.7

    @model_validator(mode="after")
    def _offsets_are_sane(self) -> ReminderPolicy:
        if not self.offsets:
            raise ValueError("reminder policy must define at least one offset")
        labels = [offset.label for offset in self.offsets]
        if len(set(labels)) != len(labels):
            raise ValueError(f"duplicate reminder labels: {labels}")
        if len(self.offsets) > self.max_reminders_per_demo:
            raise ValueError("more offsets than max_reminders_per_demo permits")
        return self


# ---------------------------------------------------------------- discounts


class DiscountBand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(max_length=48)
    #: Inclusive percentage range this band may award.
    min_percent: Annotated[int, Field(ge=0, le=100)]
    max_percent: Annotated[int, Field(ge=0, le=100)]
    #: Only these objection categories justify this band. An engine that awards
    #: a price discount for a *timing* objection is discounting for free.
    triggers: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> DiscountBand:
        if self.min_percent > self.max_percent:
            raise ValueError(f"band {self.name}: min_percent exceeds max_percent")
        return self


class DiscountPolicy(PolicyDocument):
    bands: tuple[DiscountBand, ...]
    #: Above this, a human approves. The LLM never sees these numbers and never
    #: proposes an amount — it only words an offer the engine already approved.
    max_auto_approve_percent: Annotated[int, Field(ge=0, le=100)] = 10
    absolute_max_percent: Annotated[int, Field(ge=0, le=100)] = 25
    #: A floor price below which no combination of discounts may go, expressed
    #: as a percentage of the authoritative list price. Margin protection that
    #: does not require this service to know the actual cost base.
    price_floor_percent_of_list: Annotated[int, Field(ge=1, le=100)] = 70
    validity_hours: Annotated[int, Field(ge=1, le=720)] = 48
    #: Abuse controls.
    max_offers_per_customer_per_days: Annotated[int, Field(ge=1, le=10)] = 2
    lookback_days: Annotated[int, Field(ge=1, le=365)] = 90
    #: Repeated requests after a decline are a negotiation tactic, not a signal
    #: to keep raising the offer.
    escalate_after_repeat_requests: Annotated[int, Field(ge=1, le=10)] = 2

    @model_validator(mode="after")
    def _bands_within_ceiling(self) -> DiscountPolicy:
        if not self.bands:
            raise ValueError("discount policy must define at least one band")
        over = [b.name for b in self.bands if b.max_percent > self.absolute_max_percent]
        if over:
            raise ValueError(f"bands exceed absolute_max_percent: {over}")
        if self.max_auto_approve_percent > self.absolute_max_percent:
            raise ValueError("max_auto_approve_percent exceeds absolute_max_percent")
        return self


# ---------------------------------------------------------------- forecast


class ForecastFeature(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(max_length=48)
    #: Logistic coefficient. Sign is meaningful and reviewed: a positive weight
    #: on `reschedule_count` would be claiming reschedules help conversion.
    weight: float
    #: Value used when the feature is unavailable. Chosen to be neutral, so a
    #: missing feature neither inflates nor deflates the score.
    default: float = 0.0
    description: str = ""


class ForecastModel(PolicyDocument):
    intercept: float
    features: tuple[ForecastFeature, ...]
    #: Below this many labelled demos for a segment, the model reports low
    #: confidence and the orchestrator does not act on the score alone.
    min_sample_size: Annotated[int, Field(ge=1, le=10_000)] = 30
    #: Risk bands used by reminders and by ops alerting.
    high_risk_threshold: Annotated[float, Field(ge=0, le=1)] = 0.7
    low_risk_threshold: Annotated[float, Field(ge=0, le=1)] = 0.3
    #: Calibration alarm: mean predicted probability vs observed rate.
    calibration_alert_delta: Annotated[float, Field(ge=0.01, le=0.5)] = 0.12
    drift_alert_delta: Annotated[float, Field(ge=0.01, le=1.0)] = 0.2

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> ForecastModel:
        if self.low_risk_threshold >= self.high_risk_threshold:
            raise ValueError("low_risk_threshold must be below high_risk_threshold")
        names = [feature.name for feature in self.features]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate forecast features: {names}")
        return self


# -------------------------------------------------------------- monitoring


class UnderperformanceRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(max_length=48)
    metric: str = Field(max_length=48)
    comparison: Literal["above", "below"]
    #: Absolute threshold, or a relative delta against the rolling baseline.
    threshold: float
    mode: Literal["absolute", "relative_to_baseline"] = "absolute"
    #: Nothing fires below this sample size. Three no-shows out of four demos is
    #: not a regional trend, and alerting on it trains people to ignore alerts.
    min_sample_size: Annotated[int, Field(ge=1, le=10_000)] = 20
    #: The condition must hold for this many consecutive windows.
    sustained_windows: Annotated[int, Field(ge=1, le=30)] = 2
    cooldown_hours: Annotated[int, Field(ge=1, le=720)] = 24
    severity: Literal["info", "warning", "critical"] = "warning"


class MonitoringPolicy(PolicyDocument):
    rules: tuple[UnderperformanceRule, ...]
    window_hours: Annotated[int, Field(ge=1, le=168)] = 24
    baseline_days: Annotated[int, Field(ge=1, le=365)] = 28
    #: Grace period after a demo's scheduled end before absence counts as a
    #: no-show at all.
    no_show_grace_minutes: Annotated[int, Field(ge=1, le=120)] = 10

    @model_validator(mode="after")
    def _unique_rules(self) -> MonitoringPolicy:
        names = [rule.name for rule in self.rules]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate monitoring rules: {names}")
        return self


TPolicy = TypeVar("TPolicy", bound=PolicyDocument)


def load_policy(model: type[TPolicy], path: Path) -> TPolicy:
    """Read, checksum and validate one policy file."""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise PolicyError(f"policy file unreadable: {path}") from exc

    checksum = hashlib.sha256(raw_bytes).hexdigest()
    try:
        data: Any = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy file is not valid YAML: {path}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"policy file must be a mapping: {path}")
    if "checksum" in data:
        raise PolicyError(f"checksum is computed, not declared: {path}")

    try:
        return model(**data, checksum=checksum)
    except Exception as exc:  # pydantic ValidationError, or an invariant failure
        raise PolicyError(f"policy {path.name} failed validation: {exc}") from exc


@lru_cache(maxsize=32)
def _cached(model: type[PolicyDocument], resolved: Path) -> PolicyDocument:
    return load_policy(model, resolved)


def load(model: type[TPolicy], directory: Path, name: str) -> TPolicy:
    """Load `<directory>/<name>.yaml`, cached per process."""
    resolved = (directory / f"{name}.yaml").resolve()
    document = _cached(model, resolved)
    if not isinstance(document, model):
        raise PolicyError(f"policy {name} is not a {model.__name__}")
    return document
