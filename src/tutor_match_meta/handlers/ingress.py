"""Ingress Lambda — validate, dedup, rate-limit, enqueue, return.

Deliberately thin. It does no matching, no database work beyond an idempotency
claim, and no LLM calls, because the caller is waiting and a slow ingress makes
the upstream agent time out and retry — turning one message into three.

Order of checks is cheapest-first, so an unauthenticated flood is rejected
before any HMAC or database work happens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from tutor_match_meta.contracts.inbound import (
    InboundEnvelope,
    InboundKind,
    LeadEventV1,
    ParentSelectionV1,
    WhatsAppTurnV1,
)
from tutor_match_meta.observability.context import get_logger, new_trace_id
from tutor_match_meta.observability.metrics import Metric, MetricsEmitter
from tutor_match_meta.security import signing
from tutor_match_meta.security.pii import Pseudonymiser
from tutor_match_meta.security.rate_limit import LayeredRateLimiter, LimitScope

logger = get_logger("ingress")


class Rejection(StrEnum):
    BAD_SIGNATURE = "bad_signature"
    BAD_PAYLOAD = "bad_payload"
    RATE_LIMITED = "rate_limited"
    UNSUPPORTED_KIND = "unsupported_kind"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class IngressResponse:
    status_code: int
    body: dict[str, Any]

    def to_api_gateway(self) -> dict[str, Any]:
        return {
            "statusCode": self.status_code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(self.body),
        }


@dataclass(slots=True)
class IngressConfig:
    signing_key: str
    timestamp_tolerance_seconds: int
    max_body_bytes: int
    per_conversation_per_minute: int
    global_per_minute: int


class IngressService:
    """Everything the ingress Lambda does, testable without API Gateway."""

    def __init__(
        self,
        *,
        config: IngressConfig,
        limiter: LayeredRateLimiter,
        pseudonymiser: Pseudonymiser,
        enqueue: Any,
        metrics: MetricsEmitter | None = None,
        switches: Any | None = None,
    ) -> None:
        self._config = config
        self._limiter = limiter
        self._pseudonymiser = pseudonymiser
        self._enqueue = enqueue
        self._metrics = metrics or MetricsEmitter(enabled=False)
        self._switches = switches

    async def handle(
        self, *, method: str, path: str, headers: dict[str, str], body: bytes, now: float
    ) -> IngressResponse:
        trace_id = headers.get(signing.TRACE_HEADER) or new_trace_id()

        # 1. Authenticate before parsing: never spend CPU on an unsigned body.
        try:
            timestamp = signing.parse_timestamp(headers.get(signing.TIMESTAMP_HEADER))
            signing.verify(
                self._config.signing_key,
                signing.SignedRequest(method, path, timestamp, body),
                headers.get(signing.SIGNATURE_HEADER),
                tolerance_seconds=self._config.timestamp_tolerance_seconds,
                max_body_bytes=self._config.max_body_bytes,
                now=now,
            )
        except signing.SignatureError as exc:
            logger.warning("ingress rejected", extra={"tmm_reason": exc.reason.value})
            # 401 for every signature failure: distinguishing "bad signature"
            # from "stale timestamp" tells an attacker which half to fix.
            return IngressResponse(401, {"error": Rejection.BAD_SIGNATURE.value})

        # 2. Parse and validate.
        try:
            envelope = self._parse(body, trace_id, headers)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("ingress payload invalid", extra={"tmm_error": type(exc).__name__})
            return IngressResponse(422, {"error": Rejection.BAD_PAYLOAD.value})

        # 3. Layered limits, narrowest first. Ordering matters: attributing a
        #    single spammer to their own conversation bucket keeps them from
        #    draining the global emergency brake for everyone else.
        #
        #    The IDENTITY layer catches what CONVERSATION cannot: one phone
        #    opening a fresh conversation per message, which under a
        #    per-conversation limit alone is unlimited. It is keyed on the
        #    peppered phone hash the caller supplies, so it costs nothing and
        #    holds no phone number.
        checks: list[tuple[LimitScope, str]] = [
            (LimitScope.CALLER, headers.get(signing.AGENT_HEADER, "unknown")),
            (LimitScope.CONVERSATION, self._pseudonymiser.hash(envelope.conversation_id)),
        ]
        identity = getattr(envelope.payload, "phone_hash", None)
        if identity:
            checks.append((LimitScope.IDENTITY, self._pseudonymiser.hash(identity)))
        checks.append((LimitScope.GLOBAL, "all"))

        decision = await self._limiter.check_all(checks)
        if decision.limited:
            self._metrics.count(Metric.RATE_LIMITED)
            return IngressResponse(
                429,
                {
                    "error": Rejection.RATE_LIMITED.value,
                    "scope": decision.scope.value,
                    "retry_after": decision.retry_after_seconds,
                },
            )

        # 4. Kill switch, last of the checks and before the enqueue.
        #
        # Placed after authentication and rate limiting so a pause cannot be
        # used to probe which conversations exist, and before the enqueue so a
        # paused system does not accumulate a backlog it will have to drain at
        # full speed the moment an operator unpauses. 503 (not 4xx) tells the
        # caller this is retryable, and Lead Intake keeps the conversation.
        if await self._paused():
            self._metrics.count(Metric.KILL_SWITCH_ACTIVE)
            logger.warning("ingress paused by kill switch")
            return IngressResponse(503, {"error": Rejection.PAUSED.value, "retry_after": 60})

        # 5. Enqueue and return immediately. All real work happens in the worker.
        await self._enqueue(envelope)
        self._metrics.count(Metric.MESSAGES_RECEIVED)
        return IngressResponse(202, {"accepted": True, "trace_id": trace_id})

    async def _paused(self) -> bool:
        """MATCHING_PAUSED. An unreadable switch never stops traffic by itself."""
        if self._switches is None:
            return False
        try:
            from tutor_match_meta.config.kill_switches import Switch

            return bool(await self._switches.is_paused(Switch.MATCHING_PAUSED))
        except Exception:
            logger.warning("kill switch read failed at ingress")
            return False

    def _parse(self, body: bytes, trace_id: str, headers: dict[str, str]) -> InboundEnvelope:
        raw = json.loads(body)
        if not isinstance(raw, dict):
            raise ValueError("payload must be a JSON object")

        source_agent = headers.get(signing.AGENT_HEADER, "unknown")
        event_type = str(raw.get("event_type") or raw.get("kind") or "")

        if event_type.startswith("lead."):
            payload: LeadEventV1 | WhatsAppTurnV1 | ParentSelectionV1 = LeadEventV1.model_validate(
                raw
            )
            kind = InboundKind.LEAD_EVENT
            conversation_id = payload.conversation_id
            dedup_source = payload.event_id
        elif "selected_public_ref" in raw:
            payload = ParentSelectionV1.model_validate(raw)
            kind = InboundKind.PARENT_SELECTION
            conversation_id = payload.conversation_id
            dedup_source = payload.event_id
        elif "text" in raw:
            payload = WhatsAppTurnV1.model_validate(raw)
            kind = InboundKind.WHATSAPP_TURN
            conversation_id = payload.conversation_id
            # The provider message id is the authoritative dedup key: Meta
            # redelivers the same webhook with the same id.
            dedup_source = payload.provider_message_id
        else:
            raise ValueError("unrecognised payload shape")

        return InboundEnvelope(
            kind=kind,
            trace_id=trace_id,
            conversation_id=conversation_id,
            dedup_key=signing.idempotency_key(kind.value, conversation_id, dedup_source),
            received_at=datetime.now(UTC),
            source_agent=source_agent,
            payload=payload,
        )


def sqs_message_from(envelope: InboundEnvelope) -> dict[str, Any]:
    """SQS FIFO parameters for one envelope.

    `MessageGroupId = conversation_id` serialises a conversation so two turns
    cannot race; `MessageDeduplicationId` gives SQS-level dedup on top of the
    durable idempotency table.
    """
    return {
        "MessageBody": envelope.model_dump_json(),
        "MessageGroupId": envelope.conversation_id,
        "MessageDeduplicationId": envelope.dedup_key,
    }
