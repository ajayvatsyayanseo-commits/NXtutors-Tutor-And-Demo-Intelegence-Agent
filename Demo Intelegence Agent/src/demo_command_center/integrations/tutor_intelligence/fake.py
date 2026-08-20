"""Deterministic fake Tutor Intelligence.

Same output for the same input, every time. That is what makes the E2E test
assert on a specific tutor rather than "some tutor", and what makes a failure
reproducible rather than a re-run.

It is a genuine test double, not a stub that always succeeds: it models an empty
match, a stale result and a silent-sender violation, because those are the three
branches `TutorMatchResultV1.validate_for_presentation` exists to catch and an
adapter that cannot produce them leaves those paths untested.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from demo_command_center.contracts.tutor_match import (
    DataQuality,
    DimensionScore,
    Freshness,
    ScoreEvidence,
    TutorCandidateV1,
    TutorMatchRequestV1,
    TutorMatchResultV1,
)


def _configured_site() -> str:
    """The public site this deployment allowlists.

    The fake builds profile URLs on it for the same reason the real adapter
    does: the outbound guard allowlists exactly one non-provider host, so a
    double that hardcodes a different one produces candidates whose message is
    silently refused at send time. That failure is genuinely hard to read — the
    lifecycle passes every step and one message simply never appears.

    It surfaced exactly that way: `make demo` delivered six messages instead of
    seven and logged `unapproved_url`, because the fixtures were pinned to a
    host the tests configure but the real `.env` does not.
    """
    from demo_command_center.config.settings import get_settings

    return get_settings().website_public_base_url.rstrip("/") or "https://nxtutors.example"


#: Fixture tutors. Names and refs are obviously synthetic so a leak into a real
#: environment is recognisable at a glance.
_FIXTURES: tuple[tuple[str, str, str, str], ...] = (
    ("tut_fixture_anaya", "Anaya Sharma", "Teaches CBSE Class 10 Maths", "₹500-700 per session"),
    ("tut_fixture_rohit", "Rohit Verma", "8 years of board exam coaching", "₹450-600 per session"),
    ("tut_fixture_meera", "Meera Iyer", "Rated 4.8/5 by 42 parents", "₹600-800 per session"),
)


class FakeTutorIntelligence:
    """Configurable double. Every failure mode is opt-in and explicit."""

    def __init__(
        self,
        *,
        candidate_count: int = 3,
        empty: bool = False,
        stale_by: timedelta | None = None,
        requires_human_review: bool = False,
        claims_it_sent: bool = False,
        freshness: Freshness = Freshness.FRESH,
        now: datetime | None = None,
    ) -> None:
        self.candidate_count = candidate_count
        self.empty = empty
        self.stale_by = stale_by
        self.requires_human_review = requires_human_review
        #: When True, the result reports that Tutor may have sent a message.
        #: Demo must refuse to present it — that is the double-send guard.
        self.claims_it_sent = claims_it_sent
        self.freshness = freshness
        self._now = now
        self.calls: list[TutorMatchRequestV1] = []

    async def match_tutors(self, request: TutorMatchRequestV1) -> TutorMatchResultV1:
        self.calls.append(request)
        now = self._now or datetime.now(UTC)
        generated = now - (self.stale_by or timedelta(seconds=1))

        candidates: list[TutorCandidateV1] = []
        if not self.empty:
            excluded = set(request.exclude_tutor_refs)
            pool = [row for row in _FIXTURES if row[0] not in excluded]
            for index, (ref, name, reason, fee) in enumerate(pool[: self.candidate_count], start=1):
                candidates.append(
                    TutorCandidateV1(
                        rank=index,
                        tutor_ref=ref,
                        name=name,
                        profile_url=f"{_configured_site()}/tutor/{ref}",
                        reasons=(reason,),
                        mode_label=request.mode.value.title(),
                        locality_label=request.locality,
                        availability_label="Mon-Fri 16:00-20:00",
                        fee_label=fee,
                        scores=(
                            DimensionScore(
                                dimension="subject_expertise",
                                score=0.9 - index * 0.05,
                                confidence=0.85,
                                data_quality=DataQuality.OK,
                                evidence=(
                                    ScoreEvidence(
                                        source="website.teacher_courses",
                                        field_name="subject",
                                        value=request.subject,
                                        observed_at=generated,
                                    ),
                                ),
                            ),
                            DimensionScore(
                                dimension="availability",
                                score=0.8,
                                confidence=0.7,
                                data_quality=DataQuality.PARTIAL,
                                evidence=(
                                    ScoreEvidence(
                                        source="tmm.availability",
                                        field_name="windows",
                                        value="Mon-Fri 16:00-20:00",
                                        observed_at=generated,
                                    ),
                                ),
                            ),
                        ),
                        final_score=0.9 - index * 0.05,
                        weight_coverage=0.72,
                        freshness=self.freshness,
                    )
                )

        return TutorMatchResultV1(
            trace_id=request.trace_id,
            correlation_id=request.correlation_id,
            conversation_ref=request.conversation_ref,
            match_session_id=f"fake_session_{len(self.calls)}",
            policy_ref="fake_policy@v1",
            candidates=tuple(candidates),
            no_match_reason=None if candidates else "empty_candidate_pool",
            requires_human_review=self.requires_human_review,
            generated_at=generated,
            sender_was_silent=not self.claims_it_sent,
        )
