"""The two agents composing for real, not against a double.

`test_tutor_integration.py` pins the envelope *shapes*. This file pins the
*composition*: that Demo can actually drive the real `tutor_match_meta`
orchestrator in one process and get usable candidates back.

The bug that motivated this file is worth stating, because nothing in the
type system or the envelope tests could have caught it. Tutor resolves its
relative `policy_dir` against `Path.cwd()` first. When Demo is the caller the
cwd is `Demo Intelegence Agent/`, which has a `config/policies/` of its own
holding discount, forecast, monitoring and reminder policies. Tutor therefore
loaded Demo's directory and died with:

    PolicyError: policy 'board_exam_prep.v1' not found in
    .../Demo Intelegence Agent/config/policies;
    available: ['discount.v1', 'forecast.v1', 'monitoring.v1', 'reminder.v1']

Two agents in one process cannot share a cwd-relative config path. Each test
below fails if that regresses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from demo_command_center.contracts.common import DemoMode
from demo_command_center.contracts.tutor_match import TutorMatchRequestV1
from demo_command_center.integrations.tutor_intelligence.local_adapter import (
    LocalTutorIntelligenceAdapter,
    _tutor_policy_dir,
    available,
)

pytestmark = pytest.mark.contract

pytest.importorskip(
    "tutor_match_meta",
    reason="tutor_match_meta is not installed in this runtime",
)


def a_request(**overrides: object) -> TutorMatchRequestV1:
    base: dict[str, object] = {
        "trace_id": "tr_sync",
        "correlation_id": "cor_sync",
        "conversation_ref": "cv_sync",
        "student_ref": "stu_sync",
        "subject": "Mathematics",
        "board": "CBSE",
        "student_class": "10",
        "learning_goal": "Board exam preparation",
        "mode": DemoMode.ONLINE,
        "timezone": "Asia/Kolkata",
        "limit": 3,
        "return_only": True,
    }
    base.update(overrides)
    return TutorMatchRequestV1(**base)  # type: ignore[arg-type]


def test_the_tutor_agent_is_importable_here() -> None:
    assert available()


class TestPolicyDirectoryIsolation:
    """Tutor must load ITS policies, whatever directory Demo happens to be in."""

    def test_resolves_to_a_directory_holding_the_tutor_default_policy(self) -> None:
        from tutor_match_meta.config.settings import get_settings

        settings = get_settings()
        resolved = _tutor_policy_dir(settings)

        assert resolved.is_dir(), f"policy dir does not exist: {resolved}"
        assert (resolved / f"{settings.default_policy}.yaml").is_file()

    def test_does_not_resolve_to_demos_own_policy_directory(self) -> None:
        from tutor_match_meta.config.settings import get_settings

        resolved = _tutor_policy_dir(get_settings()).resolve()
        demo_policies = (Path(__file__).resolve().parents[2] / "config" / "policies").resolve()

        assert resolved != demo_policies, (
            "Tutor resolved Demo's policy directory. Two agents in one process "
            "cannot share a cwd-relative config path."
        )

    def test_the_two_policy_sets_are_actually_disjoint(self) -> None:
        """Guards the assumption the isolation rests on.

        If someone later adds `board_exam_prep.v1.yaml` to Demo's directory,
        the test above still passes while the collision quietly returns.
        """
        from tutor_match_meta.config.settings import get_settings

        tutor_dir = _tutor_policy_dir(get_settings())
        demo_dir = Path(__file__).resolve().parents[2] / "config" / "policies"

        tutor_names = {p.stem for p in tutor_dir.glob("*.yaml")}
        demo_names = {p.stem for p in demo_dir.glob("*.yaml")}

        assert tutor_names and demo_names
        assert not (tutor_names & demo_names), (
            f"policy names collide across the two agents: {sorted(tutor_names & demo_names)}"
        )

    def test_cwd_does_not_change_the_answer(self, tmp_path: Path, monkeypatch: object) -> None:
        """The resolution is anchored on the package, so cwd is irrelevant."""
        from tutor_match_meta.config.settings import get_settings

        settings = get_settings()
        before = _tutor_policy_dir(settings)
        monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
        assert _tutor_policy_dir(settings) == before


class TestTheRealAgentsCompose:
    async def test_a_real_match_returns_usable_candidates(self) -> None:
        result = await LocalTutorIntelligenceAdapter().match_tutors(a_request())

        assert result.candidates, "the real Tutor agent returned no candidates"
        assert result.policy_ref, "no policy was stamped on the decision"
        assert result.no_match_reason is None

        for candidate in result.candidates:
            assert candidate.tutor_ref
            assert candidate.name
            assert candidate.profile_url.startswith("http")

    async def test_the_boundary_reports_that_tutor_sent_nothing(self) -> None:
        result = await LocalTutorIntelligenceAdapter().match_tutors(a_request())
        assert result.sender_was_silent is True

    async def test_ranks_are_dense_and_start_at_one(self) -> None:
        """Demo presents by ordinal, so a gap in the ranks mis-selects a tutor."""
        result = await LocalTutorIntelligenceAdapter().match_tutors(a_request())
        assert [c.rank for c in result.candidates] == list(range(1, len(result.candidates) + 1))

    async def test_the_learning_goal_selects_the_policy(self) -> None:
        """Not a cosmetic field: it picks the weighting the ranking uses."""
        board = await LocalTutorIntelligenceAdapter().match_tutors(
            a_request(learning_goal="Board exam preparation")
        )
        assert "board_exam_prep" in board.policy_ref

    async def test_excluded_tutors_do_not_come_back(self) -> None:
        """The re-match path after a parent declines everyone."""
        adapter = LocalTutorIntelligenceAdapter()
        first = await adapter.match_tutors(a_request())
        dropped = first.candidates[0].tutor_ref

        again = await adapter.match_tutors(a_request(exclude_tutor_refs=(dropped,)))
        assert dropped not in {c.tutor_ref for c in again.candidates}

    async def test_internal_only_dimensions_never_cross_the_boundary(self) -> None:
        """`replacement_risk` is a workforce signal. A parent must never see it."""
        result = await LocalTutorIntelligenceAdapter().match_tutors(a_request())
        dimensions = {s.dimension for c in result.candidates for s in c.scores}
        assert "replacement_risk" not in dimensions

    async def test_a_profile_url_is_on_the_public_site(self) -> None:
        """The outbound guard allowlists the real host. A placeholder host is
        silently blocked at send time, which looks like a missing message."""
        result = await LocalTutorIntelligenceAdapter().match_tutors(a_request())
        for candidate in result.candidates:
            assert "example" not in candidate.profile_url, (
                f"placeholder host would be blocked by the URL guard: {candidate.profile_url}"
            )
