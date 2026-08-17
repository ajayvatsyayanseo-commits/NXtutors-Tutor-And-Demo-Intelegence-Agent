"""Versioned scoring policy.

No business weight, threshold or band appears in Python source. They live in
`config/policies/*.yaml`, are validated on load, and every decision records the
policy id, version and a **checksum of the resolved document** — so an
unversioned edit to a live policy is detectable after the fact, not merely
discouraged.

Policies compose through `extends`, which keeps the seven variants (home, online,
hybrid, board exam, competitive, regular, urgent) from drifting apart: each one
states only what makes it different from the base.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tutor_match_meta.contracts.common import Dimension

#: Weights must sum to this, within tolerance. A policy that does not is a bug,
#: not a preference — it would make scores incomparable across policies.
WEIGHT_SUM = 1.0
WEIGHT_TOLERANCE = 1e-6


class PolicyError(Exception):
    """A policy document is missing, malformed, or internally inconsistent."""


class ReviewPrior(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Mean rating a tutor is assumed to have before any evidence. Pulled toward
    #: this by `domain.reviews.shrunk_rating`.
    prior_mean: float = Field(ge=0, le=5)
    #: Imaginary review count mixed in. Higher = more sceptical of small samples.
    prior_weight: float = Field(ge=0, le=100)
    min_reviews_for_rating: int = Field(ge=0, le=100)
    full_confidence_at: int = Field(ge=1, le=500)
    recency_half_life_days: float = Field(gt=0, le=3650)


class ProximityPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    default_travel_radius_km: dict[str, float]
    #: Distance at which proximity score reaches 0 for an in-radius tutor.
    max_useful_distance_km: float = Field(gt=0, le=200)
    #: Score awarded when only city/pincode equality is known, no coordinates.
    coarse_match_score: float = Field(ge=0, le=1)
    max_travel_minutes: int = Field(gt=0, le=300)

    def radius_for(self, tier: str) -> float:
        return self.default_travel_radius_km.get(
            tier, self.default_travel_radius_km.get("default", 15.0)
        )


class NegotiationStrategy(BaseModel):
    """An approved negotiation move. The set is closed (docs/assumptions.md A12)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(max_length=48)
    #: Applied when the tutor's floor exceeds the parent's ceiling by at most
    #: this ratio. 1.0 means "no gap".
    applies_up_to_ratio: float = Field(ge=1.0, le=3.0)
    description: str = Field(max_length=240)
    requires_approval: bool = False


class NegotiationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Beyond this ratio the tutor is hard-filtered out: no strategy can close it.
    max_over_budget_ratio: float = Field(ge=1.0, le=3.0)
    #: A tutor at or under budget scores this. Above 1.0 is impossible.
    within_budget_score: float = Field(ge=0, le=1)
    #: Workload above this many active students counts as overload.
    overload_active_students: int = Field(ge=1, le=100)
    strategies: tuple[NegotiationStrategy, ...]

    def strategy_for(self, ratio: float) -> NegotiationStrategy | None:
        """The cheapest approved strategy that closes a gap of `ratio`."""
        eligible = [s for s in self.strategies if ratio <= s.applies_up_to_ratio]
        return min(eligible, key=lambda s: s.applies_up_to_ratio) if eligible else None


class DiversityPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    #: At most this many shortlisted tutors may share one locality.
    max_same_locality: int = Field(ge=1, le=5)
    #: A more diverse candidate may displace a better-scoring one only when it
    #: is within this much of the incumbent. Quality is never traded for variety.
    max_score_sacrifice: float = Field(ge=0, le=0.3)


class ExplanationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expose_numeric_scores: bool = False
    expose_replacement_risk: bool = False
    max_reasons_per_tutor: int = Field(ge=1, le=6)
    #: Dimensions that may be cited to a parent, in preference order.
    citable_dimensions: tuple[Dimension, ...]


class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: A candidate below this never enters a shortlist, even if it would leave
    #: the shortlist empty (docs/assumptions.md A17).
    min_final_score: float = Field(ge=0, le=1)
    shortlist_size: int = Field(ge=1, le=5)
    candidate_pool_limit: int = Field(ge=1, le=500)
    #: Below this weight coverage the result is flagged for human review: the
    #: score rested on too few working dimensions to trust.
    min_weight_coverage: float = Field(ge=0, le=1)
    #: A dimension reporting confidence under this contributes nothing.
    min_dimension_confidence: float = Field(ge=0, le=1)
    #: Score used for a dimension with no data at all.
    neutral_score: float = Field(ge=0, le=1)


class ScoringPolicy(BaseModel):
    """A complete, resolved, immutable policy document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str = Field(max_length=64)
    version: str = Field(max_length=16)
    description: str = Field(max_length=400)
    weights: dict[Dimension, float]
    thresholds: Thresholds
    reviews: ReviewPrior
    proximity: ProximityPolicy
    negotiation: NegotiationPolicy
    diversity: DiversityPolicy
    explanation: ExplanationPolicy
    #: sha256 of the resolved document, stamped onto every decision.
    checksum: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _validate_weights(self) -> Self:
        missing = set(Dimension) - set(self.weights)
        if missing:
            raise ValueError(f"policy {self.policy_id}: missing weights for {sorted(missing)}")
        total = sum(self.weights.values())
        if abs(total - WEIGHT_SUM) > WEIGHT_TOLERANCE:
            raise ValueError(
                f"policy {self.policy_id}: weights sum to {total:.6f}, expected {WEIGHT_SUM}"
            )
        if any(w < 0 for w in self.weights.values()):
            raise ValueError(f"policy {self.policy_id}: negative weight")
        return self

    @property
    def ref(self) -> str:
        return f"{self.policy_id}.v{self.version}"

    def weight(self, dimension: Dimension) -> float:
        return self.weights[dimension]


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive merge. Scalars and lists replace; mappings merge."""
    out = dict(base)
    for key, value in override.items():
        current = out.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            out[key] = _deep_merge(current, value)
        else:
            out[key] = value
    return out


def _read_document(
    policy_dir: Path, name: str, seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Load one policy document, resolving `extends` chains.

    Cycle-guarded: a policy that extends itself (directly or through a chain)
    raises instead of recursing until the stack dies.
    """
    if name in seen:
        raise PolicyError(f"circular policy inheritance at {name!r} (chain: {sorted(seen)})")
    path = policy_dir / f"{name}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in policy_dir.glob("*.yaml"))
        raise PolicyError(f"policy {name!r} not found in {policy_dir}; available: {available}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy {name!r} is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise PolicyError(f"policy {name!r} must be a mapping at the top level")

    parent_name = document.pop("extends", None)
    if parent_name is None:
        return document
    parent = _read_document(policy_dir, str(parent_name), seen | {name})
    return _deep_merge(parent, document)


def load_policy(name: str, *, policy_dir: Path) -> ScoringPolicy:
    """Load, resolve, validate and checksum a policy. Raises `PolicyError`."""
    document = _read_document(policy_dir, name)
    # Checksum the resolved document with stable key ordering, so a formatting
    # change does not look like a policy change and a real change always does.
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        return ScoringPolicy.model_validate({**document, "checksum": checksum})
    except ValueError as exc:
        raise PolicyError(f"policy {name!r} failed validation: {exc}") from exc


class PolicyRegistry:
    """Loads policies once per process and picks the right one per request.

    Selection is deterministic and explicit — an LLM never chooses the policy
    that will rank the results it is about to explain.
    """

    def __init__(self, policy_dir: Path, default_policy: str) -> None:
        self._dir = policy_dir
        self._default = default_policy
        self._cache: dict[str, ScoringPolicy] = {}

    def get(self, name: str | None = None) -> ScoringPolicy:
        key = name or self._default
        if key not in self._cache:
            self._cache[key] = load_policy(key, policy_dir=self._dir)
        return self._cache[key]

    def available(self) -> list[str]:
        return sorted(p.stem for p in self._dir.glob("*.yaml"))

    def load_all(self) -> dict[str, ScoringPolicy]:
        """Eagerly load every policy. Used by CI to fail on a broken document."""
        return {name: self.get(name) for name in self.available() if not name.startswith("_")}


@lru_cache(maxsize=1)
def get_registry() -> PolicyRegistry:
    from tutor_match_meta.config.settings import get_settings

    settings = get_settings()
    return PolicyRegistry(_resolve_policy_dir(settings.policy_dir), settings.default_policy)


def _resolve_policy_dir(configured: Path) -> Path:
    """Find the policy directory whether running from the repo or a Lambda zip."""
    if configured.is_absolute():
        return configured
    for root in (Path.cwd(), *Path(__file__).resolve().parents):
        candidate = root / configured
        if candidate.is_dir():
            return candidate
    return configured
