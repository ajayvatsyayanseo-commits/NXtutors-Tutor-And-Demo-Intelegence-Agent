"""`TutorMatchOrchestrator` — stages C through H.

One orchestrator, eight bounded skills. This module owns the *sequence* and the
*combination*; it owns none of the matching logic itself, which is what keeps it
readable and the skills independently testable.

    C  hard filters          deterministic, LLM cannot override
    D  candidate retrieval   structured query on the projection
    E  skill scoring         the eight evaluators
    F  ranking               versioned ScoringPolicy
    G  explanation           only guard-approved evidence
    H  link resolution       canonical URLs, verified or omitted

Failure posture: a dependency being down degrades the answer and is recorded in
`degraded_sources`; it does not fail the turn. The only outcomes are a shortlist,
an honest no-match, or an explicit escalation to a human.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tutor_match_meta.contracts.common import Dimension, Freshness
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.contracts.scoring import (
    HardFilterRejection,
    MatchDecisionV1,
    ScoredCandidate,
    ShortlistEntry,
    SkillScore,
)
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain.identity import TutorProfileLinkResolver
from tutor_match_meta.matching import default_evaluators
from tutor_match_meta.matching.availability import AvailabilityEvaluator
from tutor_match_meta.matching.base import EvaluationContext, SkillEvaluator
from tutor_match_meta.matching.hard_filters import apply as apply_hard_filters
from tutor_match_meta.matching.hard_filters import rejection_summary
from tutor_match_meta.matching.proximity import ProximityEvaluator
from tutor_match_meta.orchestration.evidence_guard import assert_no_fabrication
from tutor_match_meta.orchestration.explanation import ExplanationBuilder
from tutor_match_meta.repositories.ports import (
    CandidateQuery,
    DegradedSources,
    TutorSearchPort,
    query_from_requirement,
)
from tutor_match_meta.scoring import combiner
from tutor_match_meta.scoring.policy import PolicyRegistry, ScoringPolicy
from tutor_match_meta.scoring.selector import select_policy

#: Below this weight coverage the shortlist rests on too little evidence to send
#: without a human glance.
HUMAN_REVIEW_COVERAGE_FLOOR = 0.35


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    """Everything one matching run produced."""

    decision: MatchDecisionV1
    message: str
    policy: ScoringPolicy
    policy_reason: str
    #: Populated only when a claim was refused; useful in tests and audits.
    guard_refusals: tuple[str, ...] = ()
    fabrication_violations: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.decision.matched

    @property
    def needs_human(self) -> bool:
        return self.decision.requires_human_review


class TutorMatchOrchestrator:
    """Runs the staged pipeline. Owns sequencing, not matching logic."""

    def __init__(
        self,
        *,
        tutors: TutorSearchPort,
        policies: PolicyRegistry,
        public_base_url: str,
        evaluators: dict[Dimension, SkillEvaluator] | None = None,
    ) -> None:
        self._tutors = tutors
        self._policies = policies
        self._links = TutorProfileLinkResolver(public_base_url)
        self._public_base_url = public_base_url
        self._evaluators = evaluators or default_evaluators()

    @property
    def public_base_url(self) -> str:
        """The canonical link prefix. Read by the output guard."""
        return self._public_base_url

    async def find_by_name(self, terms: tuple[str, ...], *, limit: int = 5) -> list[Any]:
        """Tutors whose name contains every term. Empty when nothing matches.

        Deliberately not part of `match()`: this answers "who is this person",
        which is a directory question, not a ranking one. No policy is applied
        and no score is produced, so nothing here can present a lookup as a
        recommendation.
        """
        if not terms:
            return []
        return await self._tutors.search(CandidateQuery(limit=limit, name_terms=terms))

    def profile_link(self, tutor: Any) -> str | None:
        """Canonical URL, or None when the projection is too stale to assert the
        tutor is still active. Never a guess."""
        return self._links.try_resolve(tutor)

    async def match(
        self,
        requirement: MatchRequirementV1,
        *,
        trace_id: str,
        now: datetime | None = None,
        knowledge: tuple[str, ...] = (),
        degraded: DegradedSources | None = None,
    ) -> MatchOutcome:
        stamp = now or datetime.now(UTC)
        degraded = degraded or DegradedSources()
        session_id = str(uuid.uuid4())

        policy_name, policy_reason = select_policy(requirement)
        policy = self._policies.get(policy_name)
        context = EvaluationContext(
            requirement=requirement, policy=policy, now=stamp, knowledge=knowledge
        )
        explainer = ExplanationBuilder(policy, public_base_url=self._public_base_url)

        # ---------------------------------------------- D: candidate retrieval
        query = query_from_requirement(requirement, limit=policy.thresholds.candidate_pool_limit)
        try:
            pool = await self._tutors.search(query)
        except Exception:
            degraded.mark("tutor_projection")
            pool = []

        if not pool:
            return self._no_match(
                requirement=requirement,
                session_id=session_id,
                trace_id=trace_id,
                policy=policy,
                policy_reason=policy_reason,
                stamp=stamp,
                candidate_ids=(),
                rejections=(),
                reason="empty_candidate_pool",
                explainer=explainer,
                degraded=degraded,
            )

        # ---------------------------------------------------- C: hard filters
        filtered = apply_hard_filters(pool, context)
        if filtered.empty:
            return self._no_match(
                requirement=requirement,
                session_id=session_id,
                trace_id=trace_id,
                policy=policy,
                policy_reason=policy_reason,
                stamp=stamp,
                candidate_ids=tuple(t.tutor_id for t in pool),
                rejections=filtered.rejections,
                reason=self._diagnose(filtered.rejections),
                explainer=explainer,
                degraded=degraded,
            )

        # ------------------------------------------------- E: skill scoring
        by_id = {tutor.tutor_id: tutor for tutor in filtered.survivors}
        proximity = ProximityEvaluator()
        scored: list[ScoredCandidate] = []
        for index, tutor in enumerate(filtered.survivors, start=1):
            scores = self._score_tutor(tutor, context)
            scored.append(
                combiner.combine(
                    tutor=tutor, pseudonym=f"cand_{index}", scores=scores, policy=policy
                )
            )

        # -------------------------------------------------------- F: ranking
        ranked = combiner.rank(
            scored,
            policy=policy,
            locality_of={
                tutor_id: proximity.cluster_key(tutor) for tutor_id, tutor in by_id.items()
            },
        )
        shortlisted = combiner.shortlist_cutoff(ranked, policy=policy)

        if not shortlisted:
            return self._no_match(
                requirement=requirement,
                session_id=session_id,
                trace_id=trace_id,
                policy=policy,
                policy_reason=policy_reason,
                stamp=stamp,
                candidate_ids=tuple(t.tutor_id for t in pool),
                rejections=filtered.rejections,
                reason="below_quality_threshold",
                explainer=explainer,
                scored=tuple(ranked),
                degraded=degraded,
            )

        # ----------------------------------------- G + H: explain and link
        entries, refusals, violations, published = self._build_entries(
            shortlisted=shortlisted,
            by_id=by_id,
            requirement=requirement,
            context=context,
            explainer=explainer,
        )

        if not entries:
            # Every survivor failed link resolution — real when a projection is
            # aging out. Escalate rather than send a shortlist with no links.
            return self._no_match(
                requirement=requirement,
                session_id=session_id,
                trace_id=trace_id,
                policy=policy,
                policy_reason=policy_reason,
                stamp=stamp,
                candidate_ids=tuple(t.tutor_id for t in pool),
                rejections=filtered.rejections,
                reason="no_resolvable_profile_links",
                explainer=explainer,
                scored=tuple(ranked),
                degraded=degraded,
                requires_human=True,
            )

        needs_human = bool(violations) or any(
            c.weight_coverage < HUMAN_REVIEW_COVERAGE_FLOOR for c in published
        )
        message = (
            explainer.compose_handoff("thin_evidence")
            if violations
            else explainer.compose_message(entries, requirement)
        )

        decision = MatchDecisionV1(
            match_session_id=session_id,
            conversation_id=requirement.conversation_id,
            trace_id=trace_id,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            policy_checksum=policy.checksum,
            candidate_ids=tuple(t.tutor_id for t in pool),
            rejections=filtered.rejections,
            scored=tuple(ranked),
            shortlist=entries,
            requires_human_review=needs_human,
            degraded_sources=degraded.as_tuple(),
            oldest_source_data_at=_oldest(by_id.values()),
            generated_at=stamp,
        )
        return MatchOutcome(
            decision=decision,
            message=message,
            policy=policy,
            policy_reason=policy_reason,
            guard_refusals=refusals,
            fabrication_violations=violations,
        )

    # ------------------------------------------------------------- internals
    def _score_tutor(
        self, tutor: TutorCandidate, context: EvaluationContext
    ) -> dict[Dimension, SkillScore]:
        """Run all eight skills. One misbehaving skill must not fail the match."""
        from tutor_match_meta.contracts.scoring import missing_score

        scores: dict[Dimension, SkillScore] = {}
        for dimension, evaluator in self._evaluators.items():
            try:
                scores[dimension] = evaluator.evaluate(tutor, context)
            except Exception:
                scores[dimension] = missing_score(
                    dimension,
                    reason="evaluator_error",
                    neutral=context.policy.thresholds.neutral_score,
                    confidence=0.0,
                )
        return scores

    def _build_entries(
        self,
        *,
        shortlisted: list[ScoredCandidate],
        by_id: dict[str, TutorCandidate],
        requirement: MatchRequirementV1,
        context: EvaluationContext,
        explainer: ExplanationBuilder,
    ) -> tuple[tuple[ShortlistEntry, ...], tuple[str, ...], tuple[str, ...], list[ScoredCandidate]]:
        availability = AvailabilityEvaluator()
        entries: list[ShortlistEntry] = []
        refusals: list[str] = []
        violations: list[str] = []
        published: list[ScoredCandidate] = []
        rank = 1

        for candidate in shortlisted:
            tutor = by_id[candidate.tutor_id]
            # H: a link we cannot verify is not published. Dropping the tutor is
            # better than sending a 404 to a parent.
            profile_url = self._links.try_resolve(tutor)
            if profile_url is None:
                refusals.append(f"unresolvable_link:{tutor.tutor_id}")
                continue

            slots = availability.suggested_slots(tutor, context)
            label = ", ".join(slot.label() for slot in slots[:2]) if slots else None

            entry, entry_refusals = explainer.build_entry(
                rank=rank,
                tutor=tutor,
                candidate=candidate,
                profile_url=profile_url,
                requirement=requirement,
                availability_label=label,
            )
            rendered = " ".join(
                [
                    entry.name,
                    *entry.reasons,
                    entry.availability_label or "",
                    entry.fee_label or "",
                    entry.locality_label or "",
                ]
            )
            violations.extend(
                f"{tutor.tutor_id}:{v}"
                for v in assert_no_fabrication(rendered, tutor, candidate.scores)
            )
            refusals.extend(entry_refusals)
            entries.append(entry)
            published.append(candidate)
            rank += 1

        return tuple(entries), tuple(refusals), tuple(violations), published

    def _no_match(
        self,
        *,
        requirement: MatchRequirementV1,
        session_id: str,
        trace_id: str,
        policy: ScoringPolicy,
        policy_reason: str,
        stamp: datetime,
        candidate_ids: tuple[str, ...],
        rejections: tuple[HardFilterRejection, ...],
        reason: str,
        explainer: ExplanationBuilder,
        degraded: DegradedSources,
        scored: tuple[ScoredCandidate, ...] = (),
        requires_human: bool = False,
    ) -> MatchOutcome:
        message = (
            explainer.compose_handoff(reason)
            if requires_human
            else explainer.compose_no_match(requirement, blocking_rule=reason)
        )
        decision = MatchDecisionV1(
            match_session_id=session_id,
            conversation_id=requirement.conversation_id,
            trace_id=trace_id,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            policy_checksum=policy.checksum,
            candidate_ids=candidate_ids,
            rejections=rejections,
            scored=scored,
            shortlist=(),
            no_match_reason=reason,
            requires_human_review=requires_human,
            degraded_sources=degraded.as_tuple(),
            generated_at=stamp,
        )
        return MatchOutcome(
            decision=decision, message=message, policy=policy, policy_reason=policy_reason
        )

    def _diagnose(self, rejections: tuple[HardFilterRejection, ...]) -> str:
        """Which constraint actually emptied the pool.

        The dominant rejection reason is what a coordinator needs to know, and
        it is what tells the parent which single constraint to relax.
        """
        summary = rejection_summary(rejections)
        if not summary:
            return "all_candidates_filtered"
        top_rule, count = next(iter(summary.items()))
        return f"filtered_by:{top_rule.lower()}:{count}"


def _oldest(tutors: object) -> datetime | None:
    """Oldest source timestamp in the pool — the freshness floor of a decision."""
    stamps = [
        t.source_updated_at
        for t in tutors  # type: ignore[attr-defined]
        if getattr(t, "source_updated_at", None) is not None
    ]
    return min(stamps) if stamps else None


def freshness_for(
    synced_at: datetime | None, *, now: datetime, fresh_hours: int, aging_hours: int
) -> Freshness:
    """Classify a projection row's age. Used by the projection repository."""
    if synced_at is None:
        return Freshness.UNKNOWN
    age_hours = (now - synced_at).total_seconds() / 3600
    if age_hours <= fresh_hours:
        return Freshness.FRESH
    if age_hours <= aging_hours:
        return Freshness.AGING
    return Freshness.STALE
