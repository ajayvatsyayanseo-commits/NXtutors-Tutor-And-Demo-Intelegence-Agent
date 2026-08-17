"""This service is eight agents fused into one. All eight must actually run.

The NXTutors agent catalogue lists these as separate services:

    013 Tutor Availability            019 Tutor Proximity
    014 Tutor Subject Expertise       021 Tutor Negotiation Profile
    015 Tutor Past Performance Score  022 Tutor Replacement Risk
    016 Tutor Personality Compatibility
    017 Tutor Academic Compatibility

Collapsing them into one process is only a win if every one of them still
contributes. The failure mode this file guards against is quiet: an evaluator
that is written, tested in isolation, and never wired into `default_evaluators`
— or one that is wired but weighted to zero in every policy, which is the same
thing with extra steps.

So each agent is asserted three ways: it is registered, it is reachable on a
real scoring run, and the skills it claims in its docstring are the skills the
catalogue lists for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tutor_match_meta.contracts.common import Dimension
from tutor_match_meta.matching import default_evaluators

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
MATCHING = ROOT / "src" / "tutor_match_meta" / "matching"

#: agent id -> (module directory, dimension, the catalogue's five skills)
CATALOGUE: dict[str, tuple[str, Dimension, tuple[str, ...]]] = {
    "013": (
        "availability",
        Dimension.AVAILABILITY,
        (
            "parse schedules",
            "find free slots",
            "handle time zones",
            "check overlap",
            "suggest slots",
        ),
    ),
    "014": (
        "subject_expertise",
        Dimension.SUBJECT_EXPERTISE,
        ("map subjects", "align syllabus", "tag depth level", "score expertise", "flag mismatches"),
    ),
    "015": (
        "performance",
        Dimension.PERFORMANCE,
        (
            "aggregate reviews",
            "normalise ratings",
            "compute retention",
            "score outcomes",
            "flag risk",
        ),
    ),
    "016": (
        "personality",
        Dimension.PERSONALITY,
        (
            "profile communication",
            "map temperament",
            "score compatibility",
            "predict conflict",
            "suggest pairing",
        ),
    ),
    "017": (
        "academic",
        Dimension.ACADEMIC,
        ("match board", "match class", "match exam", "map topic coverage", "rate academic fit"),
    ),
    "019": (
        "proximity",
        Dimension.PROXIMITY,
        (
            "geocode address",
            "compute distance",
            "estimate travel time",
            "validate radius",
            "cluster locality",
        ),
    ),
    "021": (
        "negotiation",
        Dimension.NEGOTIATION,
        (
            "analyse fee history",
            "score flexibility",
            "model minimum fee",
            "detect overload risk",
            "suggest negotiation style",
        ),
    ),
    "022": (
        "replacement_risk",
        Dimension.REPLACEMENT_RISK,
        (
            "detect churn signals",
            "score dissatisfaction",
            "analyse conflict pattern",
            "raise early alerts",
            "suggest backup tutor",
        ),
    ),
}


def docstring_of(module_dir: str) -> str:
    return (MATCHING / module_dir / "evaluator.py").read_text(encoding="utf-8")


class TestEveryAgentIsRegistered:
    def test_all_eight_dimensions_have_an_evaluator(self) -> None:
        registered = set(default_evaluators())
        expected = {dimension for _, dimension, _ in CATALOGUE.values()}
        missing = sorted(d.value for d in expected - registered)
        assert missing == [], f"composed agents that never run: {missing}"

    def test_each_evaluator_is_filed_under_its_own_dimension(self) -> None:
        """A mis-keyed registry entry would apply one agent's weight to another
        agent's score, silently."""
        for dimension, evaluator in default_evaluators().items():
            assert evaluator.dimension is dimension

    @pytest.mark.parametrize("agent_id", sorted(CATALOGUE))
    def test_the_module_exists_and_names_its_agent_id(self, agent_id: str) -> None:
        module_dir, _, _ = CATALOGUE[agent_id]
        text = docstring_of(module_dir)
        assert re.search(rf"Agent {agent_id}\b", text), (
            f"{module_dir}/evaluator.py does not identify itself as agent {agent_id}"
        )


class TestEverySkillIsClaimed:
    """The docstring is the contract with the catalogue.

    It is not decoration: `docs/agent-responsibility-matrix.md` is generated from
    this list, and a skill silently dropped from an evaluator is a capability
    this service stopped providing without anyone noticing.
    """

    @pytest.mark.parametrize("agent_id", sorted(CATALOGUE))
    def test_all_five_catalogue_skills_are_declared(self, agent_id: str) -> None:
        module_dir, _, skills = CATALOGUE[agent_id]
        # The header wraps across lines, so compare on collapsed whitespace.
        text = " ".join(docstring_of(module_dir).lower().split())
        missing = [skill for skill in skills if skill not in text]
        assert missing == [], f"agent {agent_id} no longer declares: {missing}"

    def test_the_catalogue_covers_forty_skills(self) -> None:
        total = sum(len(skills) for _, _, skills in CATALOGUE.values())
        assert total == 40


class TestEveryAgentActuallyScores:
    """Registered is not the same as reachable. Each evaluator must return a
    score for a candidate that exercises it, not silently no-op."""

    @pytest.mark.parametrize("agent_id", sorted(CATALOGUE))
    def test_the_evaluator_returns_a_score(self, agent_id: str, context) -> None:
        from tests.conftest import sample_tutors

        _, dimension, _ = CATALOGUE[agent_id]
        evaluator = default_evaluators()[dimension]

        scored = [evaluator.evaluate(tutor, context) for tutor in sample_tutors()]
        assert scored, "no sample tutors to evaluate"
        for score in scored:
            assert score.dimension is dimension
            assert 0.0 <= score.score <= 1.0
            assert 0.0 <= score.confidence <= 1.0

    @pytest.mark.parametrize("agent_id", sorted(CATALOGUE))
    def test_at_least_one_sample_tutor_yields_real_evidence(self, agent_id: str, context) -> None:
        """A dimension that can never produce evidence can never justify a
        recommendation, and the evidence guard would strip everything it said.

        All eight clear this bar, including availability (013) and replacement
        risk (022) — the two whose source data the website historically did not
        publish. 013 now reads schedules off the tutor feed; 022 grounds itself
        in the `reliability` review sub-score. Neither invents anything: a tutor
        with no data still reports MISSING, which is why this asserts *some*
        tutor yields evidence rather than every tutor.
        """
        from tests.conftest import sample_tutors

        _, dimension, _ = CATALOGUE[agent_id]
        evaluator = default_evaluators()[dimension]

        assert any(evaluator.evaluate(tutor, context).evidence for tutor in sample_tutors()), (
            f"agent {agent_id} produced no evidence for any sample tutor"
        )

    @pytest.mark.parametrize("agent_id", sorted(CATALOGUE))
    def test_a_tutor_with_no_data_reports_missing_rather_than_guessing(
        self, agent_id: str, context
    ) -> None:
        """The other half of the same property, and the more important one."""
        from tutor_match_meta.contracts.tutor import TutorCandidate
        from tutor_match_meta.domain.identity import encode_public_ref

        _, dimension, _ = CATALOGUE[agent_id]
        evaluator = default_evaluators()[dimension]
        blank = TutorCandidate(
            tutor_id="EMPTY", public_ref=encode_public_ref("EMPTY"), name="No Data"
        )
        score = evaluator.evaluate(blank, context)
        assert score.evidence == (), (
            f"agent {agent_id} produced evidence for a tutor with no data at all"
        )


class TestWeighting:
    """An agent weighted to zero everywhere is an agent that does not run."""

    def test_every_dimension_carries_weight_in_the_default_policy(self) -> None:
        from tutor_match_meta.scoring.policy import PolicyRegistry, _resolve_policy_dir

        registry = PolicyRegistry(
            _resolve_policy_dir(Path("config/policies")), "regular_school_support.v1"
        )
        policy = registry.get()
        zeroed = sorted(
            dimension.value
            for _, dimension, _ in CATALOGUE.values()
            if policy.weight(dimension) <= 0.0
        )
        assert zeroed == [], f"composed agents with no influence at all: {zeroed}"

    def test_the_weights_sum_to_one(self) -> None:
        from tutor_match_meta.scoring.policy import PolicyRegistry, _resolve_policy_dir

        registry = PolicyRegistry(
            _resolve_policy_dir(Path("config/policies")), "regular_school_support.v1"
        )
        for name, policy in registry.load_all().items():
            total = sum(policy.weight(d) for d in Dimension)
            assert abs(total - 1.0) < 1e-6, f"{name} weights sum to {total}, not 1.0"
