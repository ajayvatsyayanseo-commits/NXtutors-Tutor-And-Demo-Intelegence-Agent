"""Agent 019 — Tutor Proximity.

Skills: geocode address · compute distance · estimate travel time · validate
radius · cluster locality.

Two privacy rules constrain the whole module (docs/assumptions.md A5):

* the tutor's raw `register.address` is never read, geocoded or transmitted —
  only `pincode`, locality label, city;
* coordinates are this service's own derived data, cached in `geo_point`, never
  something the website provides.

Consequently distance has three tiers of confidence, and the evaluator reports
which one it used rather than presenting a pincode-centroid estimate as if it
were a measured distance.
"""

from __future__ import annotations

from tutor_match_meta.contracts.common import DataQuality, Dimension, Evidence, TuitionMode
from tutor_match_meta.contracts.scoring import SkillScore
from tutor_match_meta.contracts.tutor import GeoPoint, TutorCandidate
from tutor_match_meta.domain import localities
from tutor_match_meta.matching.base import EvaluationContext, build, clamp, evidence, scaled


class ProximityEvaluator:
    """Distance, travel feasibility and radius validation."""

    dimension = Dimension.PROXIMITY

    def evaluate(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        requirement = context.requirement
        mode = requirement.value_of("mode")

        # Online tuition: physical distance is not a fit signal at all. Saying
        # so explicitly beats scoring a meaningless number with real weight.
        if mode is TuitionMode.ONLINE:
            return build(
                self.dimension,
                score=1.0,
                confidence=0.9,
                quality=DataQuality.OK,
                evidences=(evidence("requirement", "mode", "online"),),
                reasons=("online_distance_irrelevant",),
            )

        location = requirement.location
        if not location:
            return build(
                self.dimension,
                score=context.policy.thresholds.neutral_score,
                confidence=0.1,
                quality=DataQuality.MISSING,
                reasons=("parent_location_unknown",),
            )

        # ---------------------------------------------- tier 1: coordinates
        if location.has_coordinates and tutor.geo is not None:
            return self._coordinate_score(tutor, context)

        # ---------------------------------------------- tier 2: pincode match
        if location.pincode and tutor.pincode and location.pincode == tutor.pincode:
            return build(
                self.dimension,
                score=min(1.0, context.policy.proximity.coarse_match_score + 0.3),
                confidence=0.7,
                quality=DataQuality.PARTIAL,
                evidences=(evidence("website.register", "pincode", tutor.pincode),),
                reasons=("same_pincode",),
                flags=("distance_estimated_from_pincode",),
            )

        # ---------------------------------------------- tier 3: city / locality
        return self._coarse_score(tutor, context)

    # ------------------------------------------------------------- internals
    def _coordinate_score(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        location = context.requirement.location
        assert tutor.geo is not None
        parent_point = GeoPoint(
            latitude=location.latitude or 0.0,
            longitude=location.longitude or 0.0,
            granularity="requirement",
        )
        distance_km = localities.haversine_km(parent_point, tutor.geo)
        travel_minutes = localities.estimated_travel_minutes(distance_km)

        proximity_policy = context.policy.proximity
        radius = self._effective_radius(tutor, context)

        evidences: list[Evidence] = [
            evidence("tmm.geo_point", "distance_km", f"{distance_km:.1f}", tutor.geo.resolved_at),
            evidence("tmm.geo_point", "granularity", tutor.geo.granularity),
        ]
        flags: list[str] = []
        reasons: list[str] = [f"distance_km:{distance_km:.1f}"]

        if distance_km > radius:
            flags.append(f"outside_travel_radius:{radius:.0f}km")
            reasons.append("outside_radius")
        if travel_minutes > proximity_policy.max_travel_minutes:
            flags.append(f"travel_time_high:{travel_minutes}min")

        score = scaled(distance_km, best=0.0, worst=proximity_policy.max_useful_distance_km)
        if distance_km > radius:
            # Beyond the assumed radius the arrangement is unlikely to survive,
            # but the radius is a policy default rather than a tutor-declared
            # fact — so this halves the score instead of zeroing it.
            score *= 0.5

        # A pincode-derived tutor point is roughly 1-3 km accurate in Indian
        # urban areas, so confidence is capped below a true address-level fix.
        confidence = 0.9 if tutor.geo.granularity == "pincode" else 0.75

        return build(
            self.dimension,
            score=score,
            confidence=confidence,
            quality=DataQuality.OK,
            evidences=tuple(evidences),
            flags=tuple(flags),
            reasons=tuple(reasons),
        )

    def _coarse_score(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        location = context.requirement.location
        coarse = context.policy.proximity.coarse_match_score

        same_locality = (
            location.locality
            and tutor.locality
            and localities.normalize_locality(location.locality)
            == localities.normalize_locality(tutor.locality)
        )
        if same_locality:
            return build(
                self.dimension,
                score=min(1.0, coarse + 0.35),
                confidence=0.6,
                quality=DataQuality.PARTIAL,
                evidences=(evidence("website.register", "locality", tutor.locality or ""),),
                reasons=("same_locality",),
                flags=("distance_not_measured",),
            )

        if localities.same_city(location.city, tutor.city):
            return build(
                self.dimension,
                score=coarse,
                confidence=0.5,
                quality=DataQuality.PARTIAL,
                evidences=(evidence("website.register", "city", tutor.city or ""),),
                reasons=("same_city",),
                flags=("distance_not_measured",),
            )

        # Unknown location is not a mismatch. Scoring a blank `register.city` as
        # "different city" would bury every tutor with an incomplete profile
        # behind a claim we cannot support.
        if not tutor.city and not tutor.pincode:
            return build(
                self.dimension,
                score=context.policy.thresholds.neutral_score,
                confidence=0.1,
                quality=DataQuality.MISSING,
                reasons=("tutor_location_unknown",),
                flags=("tutor_location_unknown",),
            )

        # Genuinely different city, home tuition requested. Should already be
        # hard-filtered; scoring near-zero here is defence in depth.
        return build(
            self.dimension,
            score=0.05,
            confidence=0.6,
            quality=DataQuality.PARTIAL,
            evidences=(evidence("website.register", "city", tutor.city or "unknown"),),
            flags=("different_city",),
            reasons=("city_mismatch",),
        )

    def _effective_radius(self, tutor: TutorCandidate, context: EvaluationContext) -> float:
        """The tighter of the parent's stated limit and the tutor's radius.

        The tutor's radius is a policy default by city tier — the schema has no
        radius column (docs/assumptions.md A6).
        """
        tier = localities.city_tier(tutor.city or context.requirement.location.city)
        tutor_radius = tutor.travel_radius_km or context.policy.proximity.radius_for(tier)
        parent_limit = context.requirement.location.max_travel_km
        return min(tutor_radius, parent_limit) if parent_limit else tutor_radius

    def cluster_key(self, tutor: TutorCandidate) -> str:
        """Locality identity used by the diversity rule in the combiner."""
        parts = [
            localities.normalize_locality(tutor.locality) or "",
            tutor.pincode or "",
            localities.normalize_city(tutor.city) or "",
        ]
        return next((p for p in parts if p), "unknown").lower()

    def within_radius(self, tutor: TutorCandidate, context: EvaluationContext) -> bool | None:
        """Radius check for the hard filter. None when distance is unknown."""
        location = context.requirement.location
        if not (location.has_coordinates and tutor.geo is not None):
            return None
        parent_point = GeoPoint(
            latitude=location.latitude or 0.0,
            longitude=location.longitude or 0.0,
            granularity="requirement",
        )
        distance = localities.haversine_km(parent_point, tutor.geo)
        # Generous slack over the policy radius: the radius is an assumption, and
        # a hard filter built on an assumption should only remove clear failures.
        return distance <= clamp(self._effective_radius(tutor, context) * 1.5, 1.0, 100.0)
