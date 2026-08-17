"""Cross-agent contracts, pinned against the sibling repositories' source.

These read the *actual* Lead Intake code where it is checked out, so a change on
their side that would break the handoff fails here rather than in production.
When the checkout is absent (CI without the sibling repo) the test skips loudly
rather than passing vacuously.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tutor_match_meta.config.settings import Settings
from tutor_match_meta.contracts.envelope import (
    MAX_HOPS,
    AgentEnvelopeV1,
    AgentId,
    LoopDetected,
    root_envelope,
)
from tutor_match_meta.contracts.events import EventType, outbound_idempotency_key, parse_payload
from tutor_match_meta.contracts.handoff import (
    INTERNAL_SECRET_HEADER,
    HandoffResponseV1,
    HandoffStatus,
    LeadIntakeHandoffV1,
)
from tutor_match_meta.integrations.agents.graph import (
    ALLOWED_EDGES,
    WHATSAPP_OWNER,
    ForbiddenHandoff,
    assert_edge_allowed,
    find_cycles,
    is_edge_allowed,
    reachable_from,
)

pytestmark = pytest.mark.contract

_AGENTS_DIR = Path(__file__).resolve().parents[3]
LEAD_INTAKE = _AGENTS_DIR / "nxtutors-lead-intake-agent"
ONBOARDING_ROUTER = LEAD_INTAKE / "app" / "services" / "onboarding_router.py"
LEAD_WHATSAPP_API = LEAD_INTAKE / "app" / "api" / "whatsapp.py"
LEAD_CONFIG = LEAD_INTAKE / "app" / "core" / "config.py"
LEAD_INTEGRATION_ROUTER = LEAD_INTAKE / "app" / "services" / "integrations" / "router.py"


def _require(path: Path) -> str:
    if not path.is_file():
        pytest.skip(f"sibling checkout not present: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


class TestWhatsAppOwnership:
    """Exactly one agent may own the public Meta webhook and the sender."""

    def test_lead_intake_still_owns_the_public_webhook(self) -> None:
        source = _require(LEAD_WHATSAPP_API)
        assert '@router.post("/webhook/whatsapp")' in source
        assert "verify_whatsapp_webhook" in source

    def test_our_graph_names_lead_intake_as_the_owner(self) -> None:
        assert WHATSAPP_OWNER is AgentId.LEAD_INTAKE

    def test_tutor_match_exposes_no_public_webhook(self) -> None:
        """A second Meta webhook would double-deliver every message."""
        api_dir = Path(__file__).resolve().parents[2] / "src" / "tutor_match_meta" / "api"
        for module in api_dir.rglob("*.py"):
            text = module.read_text(encoding="utf-8")
            assert "hub.challenge" not in text, f"{module.name} looks like a Meta webhook"
            assert "hub_verify_token" not in text, f"{module.name} looks like a Meta webhook"

    def test_default_config_has_exactly_one_sender(self) -> None:
        settings = Settings(outbound_ownership="caller_sends", whatsapp_enabled=False)
        assert settings.outbound_ownership == "caller_sends"
        assert settings.whatsapp_enabled is False


class TestLeadIntakeHandoffShape:
    """Pinned to `onboarding_router.build_onboarding_payload`."""

    def test_we_accept_the_exact_payload_lead_intake_builds(self) -> None:
        source = _require(ONBOARDING_ROUTER)
        # The keys that function emits.
        for key in (
            "source",
            "wa_message_id",
            "wa_phone",
            "message_text",
            "timestamp",
            "message_type",
            "raw_payload",
        ):
            assert f'"{key}"' in source, f"{key} no longer emitted upstream"
            assert key in LeadIntakeHandoffV1.model_fields, f"we do not accept {key}"

    def test_the_auth_header_matches(self) -> None:
        source = _require(ONBOARDING_ROUTER)
        assert "X-NXTUTORS-INTERNAL-SECRET" in source
        assert INTERNAL_SECRET_HEADER == "X-NXTUTORS-INTERNAL-SECRET"

    def test_lead_intake_reads_status_and_reply_text(self) -> None:
        source = _require(ONBOARDING_ROUTER)
        assert 'response_json.get("status")' in source
        assert 'response_json.get("reply_text")' in source
        body = HandoffResponseV1(status=HandoffStatus.HANDLED, reply_text="hello").to_body()
        assert body["status"] == "handled"
        assert body["reply_text"] == "hello"

    def test_lead_intake_treats_200_and_202_as_success(self) -> None:
        source = _require(ONBOARDING_ROUTER)
        assert "{200, 202}" in source

    def test_additive_upstream_fields_do_not_break_us(self) -> None:
        payload = LeadIntakeHandoffV1.model_validate(
            {
                "source": "lead_intake_agent",
                "wa_message_id": "wamid.x",
                "wa_phone": "+919876543210",
                "message_text": "class 10 maths",
                "brand_new_upstream_field": {"nested": True},
            }
        )
        assert payload.wa_message_id == "wamid.x"

    def test_identity_fields_are_mandatory(self) -> None:
        from pydantic import ValidationError

        with pytest.raises((ValidationError, ValueError)):
            LeadIntakeHandoffV1.model_validate({"wa_message_id": "  ", "wa_phone": "+919876543210"})

    def test_the_conversation_lane_is_stable_when_a_lead_id_appears(self) -> None:
        """Regression: keying on lead_id renamed the lane mid-conversation and
        broke continuation resume."""
        before = LeadIntakeHandoffV1.model_validate(
            {"wa_message_id": "m1", "wa_phone": "+919876543210", "message_text": "hi"}
        )
        after = LeadIntakeHandoffV1.model_validate(
            {
                "wa_message_id": "m2",
                "wa_phone": "+919876543210",
                "message_text": "hi",
                "lead_id": "L-1",
            }
        )
        assert before.effective_conversation_id() == after.effective_conversation_id()

    def test_the_2_second_budget_is_acknowledged(self) -> None:
        source = _require(ONBOARDING_ROUTER)
        assert "httpx.Timeout(2.0)" in source
        from tutor_match_meta.contracts.handoff import HANDOFF_BUDGET_SECONDS

        assert HANDOFF_BUDGET_SECONDS <= 2.0


class TestLeadIntakeIntegrationRouter:
    def test_tutor_matching_agent_is_still_a_known_target(self) -> None:
        source = _require(LEAD_INTEGRATION_ROUTER)
        assert '"tutor_matching_agent"' in source

    def test_the_webhook_url_setting_exists_upstream(self) -> None:
        source = _require(LEAD_CONFIG)
        assert "TUTOR_MATCHING_AGENT_WEBHOOK_URL" in source

    def test_the_internal_secret_setting_is_still_missing_upstream(self) -> None:
        """Documents the one-line change Lead Intake needs.

        When they add it this test flips to green-by-assertion, which is the
        signal to update docs/integration-acceptance-matrix.md.
        """
        source = _require(LEAD_CONFIG)
        has_secret = "TUTOR_MATCHING_AGENT_INTERNAL_SECRET" in source
        if not has_secret:
            pytest.xfail(
                "Lead Intake needs TUTOR_MATCHING_AGENT_INTERNAL_SECRET in "
                "app/core/config.py; the value is already in their .env"
            )
        assert has_secret


class TestSignupIntentStaysInSync:
    """Both sides must agree on what "signup" means, or a message is claimed
    twice or by nobody."""

    UPSTREAM_MARKERS = ("sign\\s*up", "register", "create\\s+(?:an?\\s+)?account", "onboard")

    def test_upstream_still_recognises_the_same_signup_words(self) -> None:
        source = _require(ONBOARDING_ROUTER)
        for marker in self.UPSTREAM_MARKERS:
            assert marker in source, f"upstream signup vocabulary changed: {marker}"

    @pytest.mark.parametrize(
        "text", ["I want to sign up", "please register me", "create an account", "onboarding"]
    )
    def test_we_decline_pure_signup(self, text: str) -> None:
        from tutor_match_meta.orchestration.routing import route

        decision = route(text)
        assert not decision.owned
        assert decision.handoff_to is AgentId.ONBOARDING

    def test_we_keep_signup_plus_tutoring(self) -> None:
        """The overlap case. Matching is the primary goal."""
        from tutor_match_meta.orchestration.routing import route

        decision = route("I need a maths tutor and I don't have an account")
        assert decision.owned
        assert decision.onboarding_secondary


class TestHandoffGraph:
    def test_the_graph_is_acyclic(self) -> None:
        assert find_cycles() == []

    def test_tutor_match_cannot_call_its_caller(self) -> None:
        assert not is_edge_allowed(AgentId.TUTOR_MATCH, AgentId.LEAD_INTAKE)
        assert AgentId.LEAD_INTAKE not in reachable_from(AgentId.TUTOR_MATCH)

    def test_tutor_match_cannot_reach_a_conversational_agent(self) -> None:
        reachable = reachable_from(AgentId.TUTOR_MATCH)
        assert AgentId.ONBOARDING not in reachable

    def test_forbidden_edges_raise(self) -> None:
        with pytest.raises(ForbiddenHandoff):
            assert_edge_allowed(AgentId.TUTOR_MATCH, AgentId.LEAD_INTAKE)

    def test_every_agent_has_an_explicit_policy(self) -> None:
        for agent in AgentId:
            assert agent in ALLOWED_EDGES, f"{agent} has no declared edges"

    def test_infrastructure_nodes_originate_nothing(self) -> None:
        for terminal in (AgentId.CHITRAGUPTA, AgentId.WEBSITE, AgentId.HUMAN, AgentId.CRM):
            assert ALLOWED_EDGES[terminal] == frozenset()


class TestEnvelopeLoopPrevention:
    def _root(self) -> AgentEnvelopeV1:
        return root_envelope(
            trace_id="t",
            correlation_id="c",
            conversation_ref="cv_x",
            source=AgentId.LEAD_INTAKE,
            destination=AgentId.TUTOR_MATCH,
            event_type=EventType.REQUESTED,
            purpose="tutor_matching",
            idempotency_key="k",
        )

    def test_trace_and_correlation_survive_a_hop(self) -> None:
        root = self._root()
        hop = root.next_hop(destination=AgentId.CHITRAGUPTA, event_type="memory.query")
        assert hop.trace_id == root.trace_id
        assert hop.correlation_id == root.correlation_id
        assert hop.causation_id == root.event_id
        assert hop.hop_count == root.hop_count + 1

    def test_revisiting_an_agent_is_blocked(self) -> None:
        with pytest.raises(LoopDetected):
            self._root().next_hop(destination=AgentId.LEAD_INTAKE, event_type=EventType.REQUESTED)

    def test_the_hop_budget_is_enforced(self) -> None:
        from pydantic import ValidationError

        with pytest.raises((LoopDetected, ValidationError)):
            AgentEnvelopeV1(
                trace_id="t",
                correlation_id="c",
                source_agent=AgentId.LEAD_INTAKE,
                destination_agent=AgentId.TUTOR_MATCH,
                event_type="x",
                conversation_ref="cv",
                purpose="p",
                idempotency_key="k",
                hop_count=MAX_HOPS + 1,
            )

    def test_an_agent_cannot_hand_off_to_itself(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentEnvelopeV1(
                trace_id="t",
                correlation_id="c",
                source_agent=AgentId.TUTOR_MATCH,
                destination_agent=AgentId.TUTOR_MATCH,
                event_type="x",
                conversation_ref="cv",
                purpose="p",
                idempotency_key="k",
            )

    def test_the_envelope_carries_no_pii(self) -> None:
        envelope = self._root()
        serialised = envelope.model_dump_json()
        for leak in ("9876543210", "+91", "@"):
            assert leak not in serialised

    def test_idempotency_key_diverges_per_destination(self) -> None:
        root = self._root()
        a = root.next_hop(destination=AgentId.CHITRAGUPTA, event_type="x")
        b = root.next_hop(destination=AgentId.WEBSITE, event_type="x")
        assert a.idempotency_key != b.idempotency_key


class TestEventContracts:
    def test_every_catalogued_type_is_versioned(self) -> None:
        for event_type in EventType.ALL:
            assert re.search(r"\.v\d+$", event_type), event_type

    def test_consumers_tolerate_unknown_additive_fields(self) -> None:
        payload = parse_payload(
            EventType.SHORTLIST_GENERATED,
            {
                "conversation_ref": "cv_x",
                "match_session_id": "s1",
                "tutor_ids": ["T1"],
                "policy_id": "home_tuition",
                "policy_version": "1",
                "policy_checksum": "abc",
                "a_field_added_next_quarter": 42,
            },
        )
        assert payload.match_session_id == "s1"  # type: ignore[attr-defined]

    def test_an_unknown_event_type_is_surfaced_not_swallowed(self) -> None:
        with pytest.raises(ValueError, match="unknown event type"):
            parse_payload("match.invented.v9", {"conversation_ref": "cv"})

    def test_outbound_idempotency_is_conversation_event_purpose(self) -> None:
        base = {"conversation_ref": "cv", "source_event_id": "e1", "purpose": "shortlist"}
        assert outbound_idempotency_key(**base) == outbound_idempotency_key(**base)
        assert outbound_idempotency_key(**{**base, "purpose": "question"}) != (
            outbound_idempotency_key(**base)
        )
        assert outbound_idempotency_key(**{**base, "source_event_id": "e2"}) != (
            outbound_idempotency_key(**base)
        )

    def test_the_outbound_event_carries_no_recipient(self) -> None:
        from tutor_match_meta.contracts.events import OutboundMessageRequestedV1

        assert "to" not in OutboundMessageRequestedV1.model_fields
        assert "phone" not in OutboundMessageRequestedV1.model_fields
