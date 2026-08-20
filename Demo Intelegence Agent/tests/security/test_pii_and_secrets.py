"""Nothing identifying and nothing secret may reach a log line or a metric.

These drive the **real** logging stack — the configured handler, the attached
`PiiScrubbingFilter`, the real `JsonFormatter` — because the failure mode being
guarded against is precisely a call site that forgot to redact. Testing
`redact()` in isolation would prove nothing about that.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime

import pytest

from demo_command_center.observability import logging as log_config
from demo_command_center.observability.metrics import Metric, NullEmitter
from demo_command_center.security.pii import assert_label_safe

pytestmark = pytest.mark.security

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

#: Every shape that must never survive to a log line.
SECRETS = {
    "phone": "9876543210",
    "international_phone": "+91 98765 43210",
    "email": "parent@example.com",
    "meta_token": "EAAG1234567890abcdefghijklmnopqrstuvwxyz",
    "cashfree_secret": "cfsk_ma_prod_1234567890abcdef",
    "openai_key": "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890",
    "hmac_secret": "v1=deadbeefcafebabe0123456789abcdef0123456789abcdef",
    "aadhaar": "1234 5678 9012",
    "upi": "parent@okaxis",
}


@pytest.fixture
def captured() -> io.StringIO:
    """The real handler, filter and formatter, writing to a buffer."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(log_config.JsonFormatter())
    handler.addFilter(log_config.PiiScrubbingFilter())

    root = logging.getLogger()
    previous, previous_level = root.handlers, root.level
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.handlers, root.level = previous, previous_level
        log_config.reset_context()


def emitted(stream: io.StringIO) -> str:
    return stream.getvalue()


class TestLogRedaction:
    @pytest.mark.parametrize("kind", sorted(SECRETS))
    def test_an_identifier_in_a_message_is_scrubbed(self, captured, kind: str) -> None:  # type: ignore[no-untyped-def]
        value = SECRETS[kind]
        log_config.get_logger("t").warning("customer contact is %s", value)
        assert value not in emitted(captured), f"{kind} survived to the log"

    @pytest.mark.parametrize("kind", sorted(SECRETS))
    def test_an_identifier_in_an_extra_is_scrubbed(self, captured, kind: str) -> None:  # type: ignore[no-untyped-def]
        log_config.get_logger("t").info("resolved", extra={"dcc_detail": SECRETS[kind]})
        assert SECRETS[kind] not in emitted(captured)

    def test_an_exception_message_is_scrubbed(self, captured) -> None:  # type: ignore[no-untyped-def]
        """The call site that leaks is always the one nobody wrapped — usually
        a validation error carrying the value that failed."""
        try:
            raise ValueError(f"invalid phone {SECRETS['phone']} for {SECRETS['email']}")
        except ValueError:
            log_config.get_logger("t").exception("validation failed")

        output = emitted(captured)
        assert SECRETS["phone"] not in output
        assert SECRETS["email"] not in output

    def test_no_traceback_is_emitted(self, captured) -> None:  # type: ignore[no-untyped-def]
        """A full traceback routinely contains the request body that caused it."""
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            log_config.get_logger("t").exception("failed")

        output = emitted(captured)
        assert "Traceback" not in output
        assert json.loads(output)["error_type"] == "RuntimeError"

    def test_every_line_is_one_json_object(self, captured) -> None:  # type: ignore[no-untyped-def]
        """A newline inside a value must be escaped, not emitted — otherwise a
        crafted message can forge a second log entry."""
        log_config.get_logger("t").info("multi\nline\nmessage")
        lines = [line for line in emitted(captured).splitlines() if line.strip()]
        assert len(lines) == 1
        json.loads(lines[0])

    def test_trace_context_is_present_and_non_identifying(self, captured) -> None:  # type: ignore[no-untyped-def]
        log_config.bind(
            trace_id="tr_1",
            correlation_id="cv_1",
            conversation_ref="cv_hash",
            capability="scheduling",
        )
        log_config.get_logger("t").info("working")
        payload = json.loads(emitted(captured))

        assert payload["trace_id"] == "tr_1"
        assert payload["capability"] == "scheduling"
        # A pseudonymous reference, never a raw conversation id or phone.
        assert payload["conversation_ref"] == "cv_hash"

    def test_a_log_line_carries_the_fields_an_operator_needs(self, captured) -> None:  # type: ignore[no-untyped-def]
        log_config.bind(trace_id="tr_1", capability="payment", handler="payment-worker")
        log_config.get_logger("t").warning(
            "provider failed",
            extra={"dcc_provider": "cashfree", "dcc_attempt": "2", "dcc_error_class": "timeout"},
        )
        payload = json.loads(emitted(captured))
        for field in ("level", "logger", "message", "timestamp", "trace_id", "capability"):
            assert field in payload
        assert payload["provider"] == "cashfree"
        assert payload["error_class"] == "timeout"


class TestMetricLabels:
    @pytest.mark.parametrize(
        "label",
        ["phone", "email", "conversation_id", "student_ref", "tutor_ref", "name", "meet_url"],
    )
    def test_an_identifying_dimension_is_refused(self, label: str) -> None:
        with pytest.raises(ValueError, match="identifying metric labels"):
            assert_label_safe({label: "anything"})

    def test_low_cardinality_dimensions_are_allowed(self) -> None:
        assert_label_safe({"region": "north", "capability": "scheduling", "outcome": "sent"})

    def test_the_emitter_refuses_an_identifying_dimension(self) -> None:
        """The check is at the emit call, not left to the caller."""
        emitter = NullEmitter()
        with pytest.raises(ValueError, match="identifying metric labels"):
            emitter.emit(Metric.TURN_PROCESSED, phone="9876543210")

    def test_a_phone_hash_is_still_refused_as_a_label(self) -> None:
        """Non-identifying but unbounded cardinality — it would multiply the
        CloudWatch bill by the user count."""
        with pytest.raises(ValueError):
            assert_label_safe({"phone_hash": "ph_abc123"})


class TestHandoffPacketPrivacy:
    def test_a_packet_never_carries_a_secret_or_an_identifier(self) -> None:
        from demo_command_center.human_handoff.escalation import (
            EscalationTrigger,
            build_packet,
        )
        from demo_command_center.state.states import DemoState

        packet = build_packet(
            case_id="hc_1",
            conversation_ref="cv_1",
            trigger=EscalationTrigger.PAYMENT_MISMATCH,
            state=DemoState.PAYMENT_PENDING,
            now=NOW,
            problem=f"customer {SECRETS['phone']} disputes",
            evidence=(f"they emailed {SECRETS['email']}", f"token {SECRETS['meta_token']}"),
            excerpts=(f"my upi is {SECRETS['upi']}",),
        )
        rendered = packet.render()
        row = json.dumps(packet.as_row())

        for value in (SECRETS["phone"], SECRETS["email"], SECRETS["upi"]):
            assert value not in rendered
            assert value not in row


class TestOutboundBodyPrivacy:
    def test_a_customer_message_cannot_carry_someone_elses_identifier(self) -> None:
        from demo_command_center.contracts.common import Party
        from demo_command_center.domain.messages import MessageKind, OutboundMessage
        from demo_command_center.guardrails.output import (
            OutputGuard,
            customer_safe_url_policy,
        )

        guard = OutputGuard(customer_safe_url_policy(website_host="nxtutors.example"))
        for kind in ("phone", "email", "aadhaar", "upi"):
            message = OutboundMessage(
                conversation_ref="cv_1",
                recipient_ref="cv_1",
                audience=Party.STUDENT,
                kind=MessageKind.FOLLOWUP,
                body=f"Contact the tutor on {SECRETS[kind]}",
                idempotency_key="k",
                created_at=NOW,
            )
            assert not guard.check(message).allowed, kind
