"""Agent 013 — Tutor Availability.

Skills: parse schedules · find free slots · handle time zones · check overlap ·
suggest slots.

**The honesty constraint that shapes this whole module:** the NXTutors website
stores no tutor availability at all (docs/current-state-audit.md §3). So the
common case today is `DataQuality.MISSING`, and this evaluator returns the
neutral prior with low confidence rather than inventing a schedule. The
explanation layer is then mechanically barred from saying "available Monday and
Wednesday after 6:30" — because nothing knows that.

When this service *does* hold availability (captured from the tutor directly),
the full overlap machinery runs: timezone-correct intersection, sufficiency
against the requested session length and frequency, and concrete slot
suggestions.
"""

from __future__ import annotations

from tutor_match_meta.contracts.common import DataQuality, Dimension, Evidence
from tutor_match_meta.contracts.schedule import TimeWindow, WeeklySchedule
from tutor_match_meta.contracts.scoring import SkillScore
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain import scheduling
from tutor_match_meta.matching.base import EvaluationContext, build, clamp, evidence

DEFAULT_SESSION_MINUTES = 60
DEFAULT_SESSIONS_PER_WEEK = 2

#: Overlap beyond `needed × this` stops improving the score. Twice the required
#: time is comfortable; ten times is not ten times better, it just means the
#: tutor is empty.
_SUFFICIENCY_CEILING = 2.0


class AvailabilityEvaluator:
    """Timezone-correct schedule overlap, or an honest 'unknown'."""

    dimension = Dimension.AVAILABILITY

    def evaluate(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        requirement = context.requirement
        wanted = requirement.preferred_schedule

        if not tutor.has_availability_data:
            # The overwhelmingly common case today. Neutral, low confidence, and
            # flagged so the shortlist can say "we'll confirm timing" instead of
            # claiming a slot.
            return build(
                self.dimension,
                score=context.policy.thresholds.neutral_score,
                confidence=0.1,
                quality=DataQuality.MISSING,
                reasons=("tutor_availability_not_recorded",),
                flags=("availability_unknown",),
            )

        assert tutor.availability is not None  # narrowed by has_availability_data
        tutor_schedule = tutor.availability

        if wanted is None:
            # The parent stated no timing constraint, so availability cannot
            # discriminate between candidates — there is nothing to overlap
            # against. It returns the neutral prior at low confidence, the same
            # value a tutor with no recorded slots gets.
            #
            # This symmetry is deliberate. Scoring "has capacity" as a partial
            # positive here quietly *penalised* tutors whose availability we had
            # bothered to record, because a mid-range score with real confidence
            # drags a weighted average further than an unknown with near-zero
            # confidence does. Having better data must never cost a tutor rank.
            return build(
                self.dimension,
                score=context.policy.thresholds.neutral_score,
                confidence=0.15,
                quality=DataQuality.PARTIAL,
                evidences=(
                    evidence(
                        "tmm.tutor_availability",
                        "windows",
                        ", ".join(tutor_schedule.labels(3)),
                        tutor.synced_at,
                    ),
                ),
                reasons=("parent_schedule_unstated",),
            )

        # Convert both into one timezone before comparing. Never guess: an
        # implicit coercion here is how "available at 6:30" becomes the wrong
        # 6:30 for an overseas online student.
        target_tz = requirement.timezone
        parent = scheduling.to_timezone(wanted, target_tz)
        tutor_here = scheduling.to_timezone(tutor_schedule, target_tz)

        overlap = parent.intersection(tutor_here)
        session_minutes = requirement.session_minutes or DEFAULT_SESSION_MINUTES
        sessions = requirement.sessions_per_week or DEFAULT_SESSIONS_PER_WEEK
        needed = session_minutes * sessions

        slots = scheduling.suggest_slots(overlap, session_minutes=session_minutes, limit=sessions)
        evidences: list[Evidence] = [
            evidence(
                "tmm.tutor_availability",
                "overlap",
                ", ".join(w.label() for w in slots) or "none",
                tutor.synced_at,
            )
        ]
        flags: list[str] = []
        reasons: list[str] = []

        if overlap.total_minutes <= 0:
            return build(
                self.dimension,
                score=0.0,
                confidence=0.85,
                quality=DataQuality.OK,
                evidences=tuple(evidences),
                flags=("no_schedule_overlap",),
                reasons=("no_overlap",),
            )

        # Two things matter and they are not the same: is there enough total
        # time, and are there enough *distinct days* to run the requested
        # frequency. Six hours on one Sunday does not support three sessions a
        # week.
        sufficiency = clamp(overlap.total_minutes / (needed * _SUFFICIENCY_CEILING))
        day_coverage = clamp(len(slots) / sessions) if sessions else 1.0

        if len(slots) < sessions:
            flags.append(f"insufficient_distinct_days:{len(slots)}/{sessions}")
            reasons.append("fewer_days_than_requested")
        if overlap.total_minutes < needed:
            flags.append("overlap_below_requested_hours")

        score = 0.45 * sufficiency + 0.55 * day_coverage
        reasons.append(f"overlap_minutes:{overlap.total_minutes}")

        return build(
            self.dimension,
            score=score,
            confidence=0.9,
            quality=DataQuality.OK,
            evidences=tuple(evidences),
            flags=tuple(flags),
            reasons=tuple(reasons),
        )

    def suggested_slots(
        self, tutor: TutorCandidate, context: EvaluationContext
    ) -> list[TimeWindow]:
        """Concrete slots to offer the parent. Empty when nothing is recorded.

        Separate from `evaluate` because a score and a human-facing suggestion
        have different failure modes: we would rather show no slots than a
        plausible-looking wrong one.
        """
        if not tutor.has_availability_data or context.requirement.preferred_schedule is None:
            return []
        assert tutor.availability is not None
        target_tz = context.requirement.timezone
        overlap = scheduling.to_timezone(
            context.requirement.preferred_schedule, target_tz
        ).intersection(scheduling.to_timezone(tutor.availability, target_tz))
        return scheduling.suggest_slots(
            overlap,
            session_minutes=context.requirement.session_minutes or DEFAULT_SESSION_MINUTES,
            limit=context.requirement.sessions_per_week or DEFAULT_SESSIONS_PER_WEEK,
        )

    def conflicts(self, tutor: TutorCandidate, commitments: WeeklySchedule) -> list[TimeWindow]:
        """Where a tutor's declared availability collides with known bookings."""
        if not tutor.has_availability_data:
            return []
        assert tutor.availability is not None
        return scheduling.detect_conflicts(tutor.availability, commitments)
