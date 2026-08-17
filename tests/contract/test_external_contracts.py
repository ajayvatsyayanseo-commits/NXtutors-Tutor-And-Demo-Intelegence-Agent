"""Contracts with systems this service does not own.

Each test pins a shape that another team can change without telling us. When one
of these fails, the other side moved — that is the signal, not a flaky test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from tutor_match_meta.contracts.inbound import LeadEventV1
from tutor_match_meta.integrations.chitragupta.client import (
    DEED_TYPE_RE,
    LIFECYCLE_STATUSES,
    REQUIRED_KEYS,
    DeedType,
    UnsafeEventError,
    make_event,
)
from tutor_match_meta.integrations.website.commands import (
    AUTO_EXECUTABLE,
    CreateDemoRequestCommand,
    CreateTutorMatchCommand,
    PublishTutorLeadCommand,
    RecordParentSelectionCommand,
)

pytestmark = pytest.mark.contract

#: The lead-intake agent's checkout, if it is present next to this repo. The
#: contract tests that depend on it skip cleanly when it is not.
LEAD_INTAKE_EVENTS = (
    Path(__file__).resolve().parents[3]
    / "nxtutors-lead-intake-agent"
    / "app"
    / "services"
    / "integrations"
    / "events.py"
)
CHITRAGUPTA_SDK = (
    Path(__file__).resolve().parents[3]
    / "nxtutors-chitragupta-memory"
    / "sdk"
    / "python"
    / "chitragupta_client"
    / "events.py"
)


class TestLeadIntakeContract:
    """Pinned to `LeadCapturedEvent.to_payload()` in the lead-intake agent."""

    PAYLOAD: ClassVar[dict] = {
        "lead_id": "L-1",
        "phone_hash": "a" * 64,
        "class": "Class 10",
        "subject": "Maths",
        "board": "CBSE",
        "city": "Gurgaon",
        "tuition_mode": "home",
        "missing_fields": [],
        "confidence_score": 0.82,
        "event_id": "e-1",
        "event_type": "lead.captured",
        "created_at": "2026-06-01T10:00:00+00:00",
    }

    def test_the_real_payload_shape_parses(self) -> None:
        event = LeadEventV1.model_validate(self.PAYLOAD)
        assert event.student_class == "Class 10"
        assert event.lead_id == "L-1"

    def test_the_class_alias_is_required(self) -> None:
        """Upstream serialises the Python keyword `class_` as `"class"`."""
        assert "class" in LeadEventV1.model_json_schema()["properties"]

    def test_lead_updated_is_accepted(self) -> None:
        assert LeadEventV1.model_validate({**self.PAYLOAD, "event_type": "lead.updated"})

    def test_unknown_upstream_fields_do_not_break_us(self) -> None:
        """Additive changes upstream must not take this service down."""
        assert LeadEventV1.model_validate({**self.PAYLOAD, "brand_new_field": 1})

    def test_upstream_sends_a_hash_not_a_number(self) -> None:
        """If this ever changes, our PII posture changes with it."""
        if not LEAD_INTAKE_EVENTS.is_file():
            pytest.skip("lead-intake checkout not present")
        source = LEAD_INTAKE_EVENTS.read_text(encoding="utf-8")
        assert "phone_hash" in source
        assert "hashlib.sha256" in source

    def test_the_upstream_event_names_are_unchanged(self) -> None:
        if not LEAD_INTAKE_EVENTS.is_file():
            pytest.skip("lead-intake checkout not present")
        source = LEAD_INTAKE_EVENTS.read_text(encoding="utf-8")
        assert 'LEAD_CAPTURED = "lead.captured"' in source
        assert 'LEAD_UPDATED = "lead.updated"' in source


class TestChitraguptaContract:
    """Pinned to the official SDK's `make_event` validation rules."""

    def test_our_deed_types_satisfy_the_gateway_regex(self) -> None:
        for deed in DeedType.ALL:
            assert DEED_TYPE_RE.match(deed), deed

    def test_a_well_formed_event_validates(self) -> None:
        event = make_event(
            trace_id="t-1",
            source_service="tutor-match-meta",
            agent_id="tutor-match-meta",
            deed_type=DeedType.SHORTLIST_GENERATED,
            lifecycle_status="completed",
            purpose="tutor_matching",
            entity_scope=[{"entity_type": "conversation", "entity_id": "cv_abc"}],
        )
        for key in REQUIRED_KEYS:
            assert event.get(key)
        assert event["schema_version"] == "1.0"
        assert event["tenant_id"] == "nxtutors"

    def test_missing_required_fields_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="missing required"):
            make_event(deed_type=DeedType.DEMO_REQUESTED, lifecycle_status="completed")

    def test_an_invalid_lifecycle_status_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="lifecycle_status"):
            make_event(
                trace_id="t",
                source_service="s",
                agent_id="a",
                deed_type=DeedType.DEMO_REQUESTED,
                lifecycle_status="finished",
                purpose="p",
            )

    def test_secret_shaped_fields_are_refused(self) -> None:
        with pytest.raises(UnsafeEventError):
            make_event(
                trace_id="t",
                source_service="s",
                agent_id="a",
                deed_type=DeedType.DEMO_REQUESTED,
                lifecycle_status="completed",
                purpose="p",
                api_key="leaked",
            )

    def test_oversized_summaries_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            make_event(
                trace_id="t",
                source_service="s",
                agent_id="a",
                deed_type=DeedType.DEMO_REQUESTED,
                lifecycle_status="completed",
                purpose="p",
                output_summary="x" * 2100,
            )

    def test_our_rules_still_match_the_official_sdk(self) -> None:
        """The vendored client must not drift from the real SDK."""
        if not CHITRAGUPTA_SDK.is_file():
            pytest.skip("chitragupta checkout not present")
        source = CHITRAGUPTA_SDK.read_text(encoding="utf-8")
        assert 'DEED_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_\\.]{2,63}$")' in source
        for status in LIFECYCLE_STATUSES:
            assert f'"{status}"' in source
        for key in REQUIRED_KEYS:
            assert f'"{key}"' in source


class TestWebsiteCommandContract:
    """The four typed write-backs. There is no generic mutation command."""

    def _commands(self) -> list:
        return [
            CreateTutorMatchCommand(
                match_session_id="s1",
                conversation_hash="cv_x",
                tutor_ids=("T1",),
                policy_ref="home_tuition.v1",
            ),
            RecordParentSelectionCommand(
                match_session_id="s1", tutor_id="T1", conversation_hash="cv_x"
            ),
            CreateDemoRequestCommand(
                match_session_id="s1", tutor_id="T1", conversation_hash="cv_x"
            ),
            PublishTutorLeadCommand(match_session_id="s1", tutor_id="T1", conversation_hash="cv_x"),
        ]

    def test_every_envelope_carries_the_audit_fields(self) -> None:
        for command in self._commands():
            envelope = command.envelope(trace_id="t-1")
            assert envelope.command and envelope.schema_version
            assert envelope.source_agent == "tutor-match-meta"
            assert envelope.trace_id == "t-1"
            assert envelope.idempotency_key
            assert envelope.risk

    def test_idempotency_keys_are_stable_and_distinct(self) -> None:
        keys = set()
        for command in self._commands():
            first = command.envelope(trace_id="t-1").idempotency_key
            second = command.envelope(trace_id="t-2").idempotency_key
            # Stable across retries with a different trace...
            assert first == second
            keys.add(first)
        # ...and distinct across commands.
        assert len(keys) == len(self._commands())

    def test_no_command_transmits_a_phone_number(self) -> None:
        for command in self._commands():
            body = json.dumps(command.payload())
            assert "phone" not in body.lower()
            assert "email" not in body.lower()

    def test_every_command_is_explicitly_allowlisted(self) -> None:
        for command in self._commands():
            assert command.name in AUTO_EXECUTABLE

    def test_the_envelope_serialises_deterministically(self) -> None:
        command = self._commands()[0]
        envelope = command.envelope(trace_id="t-1")
        assert envelope.to_json() == envelope.to_json()


class TestWebsiteUrlContract:
    """Pinned to the live Laravel route and blade template."""

    def test_the_token_scheme_matches_the_site(self) -> None:
        from tutor_match_meta.domain.identity import decode_public_ref, encode_public_ref

        # From resources/views/tutor/partials/cards.blade.php:
        #   rtrim(strtr(base64_encode($t->user_id . '-nxt'), '+/', '-_'), '=')
        assert encode_public_ref("NXT10001") == "TlhUMTAwMDEtbnh0"
        assert decode_public_ref("TlhUMTAwMDEtbnh0") == "NXT10001"

    def test_the_controller_suffix_check_is_honoured(self) -> None:
        """`showsingletutornew` 404s unless the decoded value ends in '-nxt'."""
        import base64

        from tutor_match_meta.domain.identity import decode_public_ref

        no_suffix = base64.b64encode(b"NXT10001").decode().rstrip("=")
        assert decode_public_ref(no_suffix) is None


class TestPolicyContract:
    def test_every_policy_document_loads_and_validates(self, registry) -> None:
        loaded = registry.load_all()
        assert loaded, "no policies found"
        for name, policy in loaded.items():
            assert abs(sum(policy.weights.values()) - 1.0) < 1e-6, name

    def test_the_seven_required_variants_exist(self, registry) -> None:
        required = {
            "home_tuition.v1",
            "online_tuition.v1",
            "hybrid_tuition.v1",
            "board_exam_prep.v1",
            "competitive_exam.v1",
            "regular_school_support.v1",
            "urgent_tuition.v1",
        }
        assert required <= set(registry.available())

    def test_a_policy_edit_changes_its_checksum(self, registry, tmp_path: Path) -> None:
        """The checksum is what makes an unversioned edit detectable."""
        from tutor_match_meta.scoring.policy import load_policy

        source = registry.get("home_tuition.v1")
        (tmp_path / "edited.yaml").write_text(
            (Path(registry._dir) / "_base.yaml")
            .read_text(encoding="utf-8")
            .replace("min_final_score: 0.45", "min_final_score: 0.46"),
            encoding="utf-8",
        )
        edited = load_policy("edited", policy_dir=tmp_path)
        assert edited.checksum != source.checksum
