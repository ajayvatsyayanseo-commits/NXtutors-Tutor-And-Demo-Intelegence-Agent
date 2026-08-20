"""The Tutor Intelligence boundary: envelope compatibility and return-only mode.

These are the tests that protect the *other* service. If Demo's envelope drifts
from Tutor's, routing breaks silently; if Demo ever calls a Tutor path that can
send, a parent gets two replies to one message.

`tutor_match_meta` is imported inside the tests that need it and skipped when
absent, so this suite runs both in the monorepo and in a Demo-only deployment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from demo_command_center.contracts.envelope import (
    ENVELOPE_VERSION,
    MAX_HOPS,
    AgentEnvelopeV1,
    AgentId,
    root_envelope,
)
from demo_command_center.contracts.ownership import SELF, CapabilityCall, Owner
from demo_command_center.contracts.tutor_match import (
    MAX_RESULT_AGE,
    TutorCandidateV1,
    TutorMatchRequestV1,
    TutorMatchResultV1,
)
from demo_command_center.integrations.tutor_intelligence.fake import FakeTutorIntelligence

pytestmark = pytest.mark.contract

tutor_match_meta = pytest.importorskip(
    "tutor_match_meta.contracts.envelope",
    reason="tutor_match_meta is not installed in this runtime",
)


def request_for(**overrides: object) -> TutorMatchRequestV1:
    base: dict[str, object] = {
        "trace_id": "tr_1",
        "correlation_id": "cv_1",
        "conversation_ref": "cv_1",
        "subject": "Mathematics",
        "student_class": "10",
        "board": "CBSE",
        "mode": "online",
    }
    base.update(overrides)
    return TutorMatchRequestV1.model_validate(base)


# ------------------------------------------------------ envelope compatibility


def test_agent_ids_match_the_tutor_service_exactly() -> None:
    """A value present in one enum and not the other is an unroutable event."""
    theirs = {member.value for member in tutor_match_meta.AgentId}
    ours = {member.value for member in AgentId}
    assert ours == theirs


def test_demo_command_center_is_a_known_agent_upstream() -> None:
    assert tutor_match_meta.AgentId.DEMO_COMMAND_CENTER.value == "demo_command_center_agent"
    assert AgentId.DEMO_COMMAND_CENTER.value == "demo_command_center_agent"


def test_envelope_version_and_hop_budget_agree() -> None:
    assert ENVELOPE_VERSION == tutor_match_meta.ENVELOPE_VERSION
    assert MAX_HOPS == tutor_match_meta.MAX_HOPS


def test_every_field_the_tutor_envelope_requires_is_present_in_ours() -> None:
    """Ours may add fields (`expires_at`, `tenant_id`); it may not drop any.

    Demo never posts its own model to Tutor — the adapter emits only fields
    Tutor declares — but a *missing* field would mean we cannot represent an
    envelope Tutor sends us, which breaks the inbound direction.
    """
    theirs = set(tutor_match_meta.AgentEnvelopeV1.model_fields)
    ours = set(AgentEnvelopeV1.model_fields)
    assert theirs - ours == set()


def test_trace_headers_are_identical() -> None:
    """Header names are the wire contract for correlation across services."""
    common = {
        "trace_id": "tr_1",
        "correlation_id": "cv_1",
        "conversation_ref": "cvref",
        "event_type": "demo.requested",
        "purpose": "demo",
        "idempotency_key": "idem_1",
    }
    ours = root_envelope(
        source=AgentId.LEAD_INTAKE, destination=AgentId.DEMO_COMMAND_CENTER, **common
    )
    theirs = tutor_match_meta.root_envelope(
        source=tutor_match_meta.AgentId.LEAD_INTAKE,
        destination=tutor_match_meta.AgentId.DEMO_COMMAND_CENTER,
        **common,
    )
    assert set(ours.headers()) == set(theirs.headers())


def test_demo_cannot_hand_a_conversation_back_to_lead_intake() -> None:
    """Handing a paying customer to intake restarts their funnel."""
    assert Owner.LEAD_INTAKE not in _transfer_targets()


def _transfer_targets() -> frozenset[Owner]:
    from demo_command_center.contracts.ownership import TRANSFER_TO

    return TRANSFER_TO


# -------------------------------------------------------------- return-only


def test_a_match_request_cannot_be_built_without_return_only() -> None:
    with pytest.raises(ValueError, match="double-send"):
        request_for(return_only=False)


def test_capability_call_cannot_authorise_the_callee_to_send() -> None:
    with pytest.raises(ValueError, match="may not authorise"):
        CapabilityCall(
            conversation_ref="cv_1",
            callee=AgentId.TUTOR_MATCH,
            purpose="tutor_match",
            trace_id="tr_1",
            correlation_id="cv_1",
            return_only=False,
        )


async def test_a_result_claiming_the_tutor_agent_spoke_is_refused() -> None:
    """The double-send guard. A silent sender is the only acceptable answer."""
    fake = FakeTutorIntelligence(claims_it_sent=True)
    result = await fake.match_tutors(request_for())
    problems = result.validate_for_presentation(now=datetime.now(UTC))
    assert "tutor_agent_may_have_sent_a_message" in problems


def test_the_tutor_orchestrator_has_no_sender_or_state_writer() -> None:
    """Structural proof that the chosen boundary cannot send.

    `TutorMatchOrchestrator.match()` is a pure function of its inputs. If a
    future change gave it an outbox, a sender or a conversation store, this
    fails — and that change would make Demo's calls capable of a second reply.
    """
    from tutor_match_meta.orchestration.orchestrator import TutorMatchOrchestrator

    forbidden = {"outbox", "sender", "conversations", "whatsapp", "notifier"}
    attributes = {name.lstrip("_") for name in TutorMatchOrchestrator.__init__.__annotations__}
    assert forbidden & attributes == set()
    assert not hasattr(TutorMatchOrchestrator, "send")


def test_demo_is_the_conversation_owner_not_the_tutor_agent() -> None:
    assert SELF is Owner.DEMO_COMMAND_CENTER
    assert Owner.TUTOR_MATCH not in _transfer_targets()


# ------------------------------------------------------- result validation


async def test_a_stale_result_is_not_presentable() -> None:
    fake = FakeTutorIntelligence(stale_by=MAX_RESULT_AGE + timedelta(minutes=1))
    result = await fake.match_tutors(request_for())
    problems = result.validate_for_presentation(now=datetime.now(UTC))
    assert any(problem.startswith("result_stale") for problem in problems)


async def test_a_fresh_result_is_presentable() -> None:
    fake = FakeTutorIntelligence()
    result = await fake.match_tutors(request_for())
    assert result.validate_for_presentation(now=datetime.now(UTC)) == ()
    assert len(result.presentable(now=datetime.now(UTC))) == 3


async def test_a_result_from_the_future_is_refused() -> None:
    """Clock skew or a fabricated result. Neither may be shown."""
    fake = FakeTutorIntelligence(stale_by=timedelta(minutes=-30))
    result = await fake.match_tutors(request_for())
    assert "result_generated_in_the_future" in result.validate_for_presentation(
        now=datetime.now(UTC)
    )


async def test_exclusions_are_honoured_so_show_me_others_shows_others() -> None:
    fake = FakeTutorIntelligence()
    first = await fake.match_tutors(request_for())
    seen = tuple(c.tutor_ref for c in first.candidates)
    second = await fake.match_tutors(request_for(exclude_tutor_refs=seen[:1]))
    assert seen[0] not in {c.tutor_ref for c in second.candidates}


def test_presentable_reranks_densely_after_dropping_a_stale_candidate() -> None:
    """Presenting "option 1" and "option 3" reads as a bug to a parent."""
    now = datetime.now(UTC)
    candidates = tuple(
        TutorCandidateV1(
            rank=rank,
            tutor_ref=f"tut_{rank}",
            name=f"Tutor {rank}",
            profile_url=f"https://nxtutors.example/t/{rank}",
            final_score=0.5,
            freshness="stale" if rank == 2 else "fresh",
        )
        for rank in (1, 2, 3)
    )
    result = TutorMatchResultV1(
        trace_id="tr",
        correlation_id="cv",
        conversation_ref="cv",
        match_session_id="ms",
        candidates=candidates,
        generated_at=now,
    )
    assert [c.rank for c in result.presentable(now=now)] == [1, 2]
    assert [c.tutor_ref for c in result.presentable(now=now)] == ["tut_1", "tut_3"]


def test_a_candidate_with_no_quotable_dimension_makes_no_claims() -> None:
    """A new tutor may be offered; nothing may be asserted about them."""
    candidate = TutorCandidateV1(
        rank=1,
        tutor_ref="tut_new",
        name="New Tutor",
        profile_url="https://nxtutors.example/t/new",
        reasons=("Highly experienced",),
        scores=(),
        final_score=0.5,
    )
    assert candidate.quotable_reasons() == ()
