"""The outbox → relay → worker delivery contract.

Four components used to agree on nothing. The turn service wrote
`{"text", "conversation_hash"}`; the outbound worker read `payload["to"]` and
`payload["body"]`, hit a `KeyError`, logged "unparseable outbound record", and
`continue`d — so a relayed reply was dropped without reaching the DLQ and
without alarming. Nothing in the suite noticed, because no test ever ran a row
from the producer through to the consumer.

These tests do exactly that: they take what `TurnService` actually writes,
serialise it the way the relay does, and parse it the way the worker does.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tutor_match_meta.contracts.inbound import InboundEnvelope, InboundKind, WhatsAppTurnV1
from tutor_match_meta.contracts.outbound import (
    OUTBOX_KIND_CALLER_REPLY,
    OUTBOX_KIND_REPLY,
    OutboundDeliveryV1,
)
from tutor_match_meta.orchestration.turn_service import TurnService

pytestmark = pytest.mark.contract


def envelope(*, reply_to: str | None, message_id: str = "m1") -> InboundEnvelope:
    return InboundEnvelope(
        kind=InboundKind.WHATSAPP_TURN,
        trace_id="delivery",
        conversation_id="c-delivery",
        dedup_key=f"delivery:{message_id}",
        received_at=datetime.now(UTC),
        source_agent="lead_intake_agent",
        reply_to=reply_to,
        payload=WhatsAppTurnV1(
            event_id=message_id,
            conversation_id="c-delivery",
            provider_message_id=message_id,
            text="class 10 cbse maths gurgaon home tuition",
        ),
    )


def relay_body(row) -> str:
    """Exactly what `sync/outbox_relay._delivery` does before sending."""
    delivery = OutboundDeliveryV1.from_payload(
        {**dict(row.payload), "kind": row.kind, "trace_id": row.trace_id}
    )
    return delivery.model_dump_json()


class TestProducerConsumerAgreement:
    async def test_a_row_we_own_round_trips_to_the_worker(self, turn_deps) -> None:
        """The regression. Producer → relay → worker, no adapters in between."""
        await TurnService(turn_deps).handle(envelope(reply_to="+919876543210"))
        rows = turn_deps.outbox.pending
        assert len(rows) == 1

        body = relay_body(rows[0])
        # The worker's parse, verbatim.
        delivery = OutboundDeliveryV1.from_payload(json.loads(body))
        assert delivery.kind == OUTBOX_KIND_REPLY
        assert delivery.deliverable
        assert delivery.recipient == "+919876543210"
        assert delivery.body
        assert delivery.dedup_key.startswith("reply:")
        assert delivery.trace_id == "delivery"

    async def test_a_caller_owned_row_is_audit_only(self, turn_deps) -> None:
        """Default ownership: we hold no recipient, so we address nothing."""
        await TurnService(turn_deps).handle(envelope(reply_to=None))
        row = turn_deps.outbox.pending[0]
        delivery = OutboundDeliveryV1.from_payload(json.loads(relay_body(row)))
        assert delivery.kind == OUTBOX_KIND_CALLER_REPLY
        assert not delivery.deliverable
        assert delivery.recipient is None
        # The text is still recorded: it is the audit trail of what the parent
        # was told, which is what a dispute or an incident needs.
        assert delivery.body


class TestDeliveryValidation:
    def test_a_deliverable_row_without_a_recipient_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="E.164"):
            OutboundDeliveryV1(kind=OUTBOX_KIND_REPLY, body="hi there", dedup_key="d", trace_id="t")

    def test_a_malformed_recipient_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="E.164"):
            OutboundDeliveryV1(
                kind=OUTBOX_KIND_REPLY,
                recipient="not-a-number",
                body="hi there",
                dedup_key="d",
                trace_id="t",
            )

    def test_an_empty_body_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty message body"):
            OutboundDeliveryV1(
                kind=OUTBOX_KIND_CALLER_REPLY, body="   ", dedup_key="d", trace_id="t"
            )

    def test_an_unknown_field_from_a_newer_producer_is_tolerated(self) -> None:
        """Additive changes upstream must not become an outage downstream."""
        delivery = OutboundDeliveryV1.from_payload(
            {
                "kind": OUTBOX_KIND_CALLER_REPLY,
                "body": "hello",
                "dedup_key": "d",
                "trace_id": "t",
                "some_future_field": 1,
            }
        )
        assert delivery.body == "hello"


class TestIdempotentDelivery:
    async def test_a_duplicate_turn_produces_exactly_one_outbox_row(self, turn_deps) -> None:
        service = TurnService(turn_deps)
        message = envelope(reply_to="+919876543210")
        await service.handle(message)
        await service.handle(message)
        assert len(turn_deps.outbox.pending) == 1

    async def test_the_dedup_key_is_derived_from_the_provider_message_id(self, turn_deps) -> None:
        """Two different turns must not collide, one turn must not split."""
        service = TurnService(turn_deps)
        await service.handle(envelope(reply_to="+919876543210", message_id="m1"))
        await service.handle(envelope(reply_to="+919876543210", message_id="m2"))
        keys = {row.dedup_key for row in turn_deps.outbox.pending}
        assert len(keys) == 2
