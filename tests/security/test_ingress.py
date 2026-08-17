"""The ingress service: authenticate, parse, limit, pause, enqueue.

This is the only internet-facing code path, and it was previously untested end
to end — including the two layers added during the hardening pass (the identity
rate limit and the kill switch).

The ordering assertions matter as much as the outcomes. Authentication runs
before parsing so an unsigned flood costs no CPU; the conversation limit runs
before the global one so a single spammer cannot drain the emergency brake; the
kill switch runs after authentication so a pause cannot be used to probe which
conversations exist, and before the enqueue so a paused system does not
accumulate a backlog to drain at full speed on unpause.
"""

from __future__ import annotations

import json
import time

import pytest

from tutor_match_meta.config.kill_switches import KillSwitches, NoKillSwitches, Switch
from tutor_match_meta.handlers.ingress import (
    IngressConfig,
    IngressService,
    Rejection,
    sqs_message_from,
)
from tutor_match_meta.security import signing
from tutor_match_meta.security.pii import Pseudonymiser
from tutor_match_meta.security.rate_limit import (
    InMemoryBucketStore,
    LayeredRateLimiter,
    LimitPolicy,
    LimitScope,
)

pytestmark = pytest.mark.security

KEY = "ingress-test-key"
PATH = "/ingress"


class Recorder:
    """Captures what would have been enqueued."""

    def __init__(self) -> None:
        self.envelopes: list[object] = []

    async def __call__(self, envelope: object) -> None:
        self.envelopes.append(envelope)


class PausedSwitches:
    def __init__(self, *paused: Switch) -> None:
        self._paused = {s.value for s in paused}

    async def paused(self, switch: str) -> bool:
        return switch in self._paused

    async def all_paused(self) -> dict[str, bool]:
        return dict.fromkeys(self._paused, True)


def build(
    *,
    limits: dict[LimitScope, LimitPolicy] | None = None,
    switches: object | None = None,
) -> tuple[IngressService, Recorder]:
    enqueue = Recorder()
    service = IngressService(
        config=IngressConfig(
            signing_key=KEY,
            timestamp_tolerance_seconds=300,
            max_body_bytes=65_536,
            per_conversation_per_minute=12,
            global_per_minute=600,
        ),
        limiter=LayeredRateLimiter(
            InMemoryBucketStore(),
            limits
            or {
                LimitScope.CALLER: LimitPolicy(per_minute=600),
                LimitScope.CONVERSATION: LimitPolicy(per_minute=600),
                LimitScope.IDENTITY: LimitPolicy(per_minute=600),
                LimitScope.GLOBAL: LimitPolicy(per_minute=600),
            },
        ),
        pseudonymiser=Pseudonymiser("test-pepper"),
        enqueue=enqueue,
        switches=KillSwitches(switches or NoKillSwitches()),  # type: ignore[arg-type]
    )
    return service, enqueue


def whatsapp_body(*, conversation: str = "c1", message_id: str = "m1", **extra: object) -> bytes:
    payload: dict[str, object] = {
        "event_id": message_id,
        "conversation_id": conversation,
        "provider_message_id": message_id,
        "text": "class 10 cbse maths gurgaon home tuition",
    }
    payload.update(extra)
    return json.dumps(payload).encode()


def signed(body: bytes, *, now: float | None = None, key: str = KEY) -> dict[str, str]:
    stamp = int(now if now is not None else time.time())
    request = signing.SignedRequest("POST", PATH, stamp, body)
    return {
        signing.SIGNATURE_HEADER: signing.sign(key, request),
        signing.TIMESTAMP_HEADER: str(stamp),
        signing.AGENT_HEADER: "lead_intake_agent",
    }


async def call(service: IngressService, body: bytes, headers: dict[str, str]):
    return await service.handle(
        method="POST", path=PATH, headers=headers, body=body, now=time.time()
    )


class TestAuthentication:
    async def test_a_signed_request_is_accepted(self) -> None:
        service, enqueue = build()
        body = whatsapp_body()
        response = await call(service, body, signed(body))
        assert response.status_code == 202
        assert len(enqueue.envelopes) == 1

    async def test_an_unsigned_request_is_refused(self) -> None:
        service, enqueue = build()
        body = whatsapp_body()
        response = await call(service, body, {})
        assert response.status_code == 401
        assert response.body["error"] == Rejection.BAD_SIGNATURE.value
        assert enqueue.envelopes == []

    async def test_a_wrong_key_is_refused(self) -> None:
        service, _ = build()
        body = whatsapp_body()
        response = await call(service, body, signed(body, key="not-the-key"))
        assert response.status_code == 401

    async def test_a_stale_timestamp_is_refused(self) -> None:
        service, _ = build()
        body = whatsapp_body()
        response = await call(service, body, signed(body, now=time.time() - 3_600))
        assert response.status_code == 401

    async def test_every_signature_failure_returns_the_same_error(self) -> None:
        """Distinguishing 'bad signature' from 'stale timestamp' tells an
        attacker which half to fix."""
        service, _ = build()
        body = whatsapp_body()
        wrong_key = await call(service, body, signed(body, key="x"))
        stale = await call(service, body, signed(body, now=time.time() - 3_600))
        assert wrong_key.body == stale.body

    async def test_a_tampered_body_is_refused(self) -> None:
        service, _ = build()
        body = whatsapp_body()
        headers = signed(body)
        response = await call(service, whatsapp_body(conversation="somewhere-else"), headers)
        assert response.status_code == 401


class TestParsing:
    async def test_malformed_json_is_refused_without_enqueueing(self) -> None:
        service, enqueue = build()
        body = b"{not json"
        response = await call(service, body, signed(body))
        assert response.status_code == 422
        assert enqueue.envelopes == []

    async def test_a_json_array_is_refused(self) -> None:
        service, _ = build()
        body = b'["not", "an", "object"]'
        response = await call(service, body, signed(body))
        assert response.status_code == 422

    async def test_an_unrecognised_shape_is_refused(self) -> None:
        service, _ = build()
        body = json.dumps({"something": "else"}).encode()
        response = await call(service, body, signed(body))
        assert response.status_code == 422

    async def test_an_empty_message_is_refused(self) -> None:
        service, _ = build()
        body = whatsapp_body(text="   ")
        response = await call(service, body, signed(body))
        assert response.status_code == 422

    async def test_a_lead_event_is_recognised(self) -> None:
        service, enqueue = build()
        body = json.dumps(
            {
                "event_id": "e1",
                "event_type": "lead.captured",
                "lead_id": "L1",
                "subject": "Mathematics",
            }
        ).encode()
        response = await call(service, body, signed(body))
        assert response.status_code == 202
        assert enqueue.envelopes[0].kind.value == "lead_event"  # type: ignore[attr-defined]

    async def test_a_parent_selection_is_recognised(self) -> None:
        service, enqueue = build()
        body = json.dumps(
            {
                "event_id": "e1",
                "conversation_id": "c1",
                "match_session_id": "s1",
                "selected_public_ref": "TlhUMTAwMDEtbnh0",
            }
        ).encode()
        response = await call(service, body, signed(body))
        assert response.status_code == 202
        assert enqueue.envelopes[0].kind.value == "parent_selection"  # type: ignore[attr-defined]


class TestRateLimits:
    async def test_the_conversation_limit_refuses(self) -> None:
        service, enqueue = build(
            limits={LimitScope.CONVERSATION: LimitPolicy(per_minute=60, burst=2)}
        )
        for index in range(2):
            body = whatsapp_body(message_id=f"m{index}")
            assert (await call(service, body, signed(body))).status_code == 202

        body = whatsapp_body(message_id="m3")
        response = await call(service, body, signed(body))
        assert response.status_code == 429
        assert response.body["scope"] == "conversation"
        assert response.body["retry_after"] > 0
        assert len(enqueue.envelopes) == 2

    async def test_the_identity_limit_catches_conversation_hopping(self) -> None:
        """The evasion a per-conversation limit cannot see.

        One phone opening a fresh conversation per message is unlimited under
        the conversation bucket alone.
        """
        service, enqueue = build(
            limits={
                LimitScope.CONVERSATION: LimitPolicy(per_minute=600),
                LimitScope.IDENTITY: LimitPolicy(per_minute=60, burst=2),
            }
        )
        for index in range(2):
            body = whatsapp_body(conversation=f"c{index}", phone_hash="ph_same")
            assert (await call(service, body, signed(body))).status_code == 202

        body = whatsapp_body(conversation="c99", phone_hash="ph_same")
        response = await call(service, body, signed(body))
        assert response.status_code == 429
        assert response.body["scope"] == "identity"
        assert len(enqueue.envelopes) == 2

    async def test_a_payload_without_a_phone_hash_skips_the_identity_layer(self) -> None:
        service, _ = build(limits={LimitScope.IDENTITY: LimitPolicy(per_minute=60, burst=1)})
        for index in range(3):
            body = whatsapp_body(message_id=f"m{index}")
            assert (await call(service, body, signed(body))).status_code == 202

    async def test_a_spammer_does_not_drain_the_global_brake(self) -> None:
        """Narrowest-first ordering, stated as a test.

        The conversation limit refuses first and short-circuits, so the global
        bucket still has capacity for everyone else.
        """
        service, _ = build(
            limits={
                LimitScope.CONVERSATION: LimitPolicy(per_minute=60, burst=1),
                LimitScope.GLOBAL: LimitPolicy(per_minute=60, burst=3),
            }
        )
        body = whatsapp_body(conversation="spammer", message_id="m0")
        assert (await call(service, body, signed(body))).status_code == 202
        for index in range(1, 5):
            body = whatsapp_body(conversation="spammer", message_id=f"m{index}")
            assert (await call(service, body, signed(body))).status_code == 429

        # An unrelated parent is still served.
        body = whatsapp_body(conversation="innocent", message_id="x1")
        assert (await call(service, body, signed(body))).status_code == 202


class TestKillSwitch:
    async def test_a_paused_ingress_returns_503_and_enqueues_nothing(self) -> None:
        service, enqueue = build(switches=PausedSwitches(Switch.MATCHING_PAUSED))
        body = whatsapp_body()
        response = await call(service, body, signed(body))
        assert response.status_code == 503
        assert response.body["error"] == Rejection.PAUSED.value
        assert enqueue.envelopes == [], "a paused ingress still built a backlog"

    async def test_an_unsigned_request_is_still_401_while_paused(self) -> None:
        """The pause must not become an oracle.

        Authentication runs first, so an unauthenticated caller learns nothing
        about whether the service is paused.
        """
        service, _ = build(switches=PausedSwitches(Switch.MATCHING_PAUSED))
        response = await call(service, whatsapp_body(), {})
        assert response.status_code == 401

    async def test_an_unrelated_switch_does_not_pause_ingress(self) -> None:
        service, enqueue = build(switches=PausedSwitches(Switch.RAG_PAUSED))
        body = whatsapp_body()
        assert (await call(service, body, signed(body))).status_code == 202
        assert len(enqueue.envelopes) == 1

    async def test_an_unreadable_switch_store_does_not_stop_traffic(self) -> None:
        class Broken:
            async def paused(self, switch: str) -> bool:
                raise RuntimeError("switch store unreachable")

            async def all_paused(self) -> dict[str, bool]:
                raise RuntimeError("switch store unreachable")

        service, enqueue = build(switches=Broken())
        body = whatsapp_body()
        assert (await call(service, body, signed(body))).status_code == 202
        assert len(enqueue.envelopes) == 1


class TestEnvelopeAndQueueing:
    async def test_the_dedup_key_comes_from_the_provider_message_id(self) -> None:
        service, enqueue = build()
        body = whatsapp_body(message_id="wamid.ABC")
        await call(service, body, signed(body))
        first = enqueue.envelopes[0]

        body = whatsapp_body(message_id="wamid.ABC", conversation="c1")
        await call(service, body, signed(body))
        second = enqueue.envelopes[1]
        assert first.dedup_key == second.dedup_key  # type: ignore[attr-defined]

    async def test_the_trace_id_is_propagated_when_supplied(self) -> None:
        service, enqueue = build()
        body = whatsapp_body()
        headers = signed(body) | {signing.TRACE_HEADER: "trace-from-caller"}
        response = await call(service, body, headers)
        assert response.body["trace_id"] == "trace-from-caller"
        assert enqueue.envelopes[0].trace_id == "trace-from-caller"  # type: ignore[attr-defined]

    async def test_a_trace_id_is_minted_when_absent(self) -> None:
        service, enqueue = build()
        body = whatsapp_body()
        response = await call(service, body, signed(body))
        assert response.body["trace_id"]
        assert enqueue.envelopes[0].trace_id == response.body["trace_id"]  # type: ignore[attr-defined]

    async def test_no_reply_to_is_set_by_the_signed_path(self) -> None:
        """Ingress never holds a raw phone number.

        The inbound contract carries `phone_hash` only, so the outbox row this
        produces is audit-only. Delivery ownership arrives through the handoff
        path, which does receive `wa_phone`.
        """
        service, enqueue = build()
        body = whatsapp_body(phone_hash="ph_abc")
        await call(service, body, signed(body))
        assert enqueue.envelopes[0].reply_to is None  # type: ignore[attr-defined]

    def test_the_sqs_message_groups_by_conversation(self) -> None:
        from datetime import UTC, datetime

        from tutor_match_meta.contracts.inbound import (
            InboundEnvelope,
            InboundKind,
            WhatsAppTurnV1,
        )

        envelope = InboundEnvelope(
            kind=InboundKind.WHATSAPP_TURN,
            trace_id="t",
            conversation_id="wa:+919876543210",
            dedup_key="d1",
            received_at=datetime.now(UTC),
            source_agent="lead_intake_agent",
            payload=WhatsAppTurnV1(
                event_id="e",
                conversation_id="wa:+919876543210",
                provider_message_id="m",
                text="hello there",
            ),
        )
        message = sqs_message_from(envelope)
        # FIFO grouping by conversation is what serialises a conversation's
        # turns; dedup is supplied, never content-derived.
        assert message["MessageGroupId"] == "wa:+919876543210"
        assert message["MessageDeduplicationId"] == "d1"

    async def test_the_api_gateway_shape_is_valid_json(self) -> None:
        service, _ = build()
        body = whatsapp_body()
        response = await call(service, body, signed(body))
        gateway = response.to_api_gateway()
        assert gateway["statusCode"] == 202
        assert json.loads(gateway["body"])["accepted"] is True
