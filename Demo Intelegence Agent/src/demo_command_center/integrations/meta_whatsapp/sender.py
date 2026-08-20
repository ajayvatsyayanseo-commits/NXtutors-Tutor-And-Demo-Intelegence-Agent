"""The Meta Cloud API sender. Delivery only.

This class does exactly one thing: turn an `OutboundMessage` plus a resolved
phone number into a Graph API call. It makes no policy decision — ownership,
opt-out, session window, idempotency and guardrails were all settled by
`orchestration/outbound.py` before it was called.

That separation is why it is safe for this to be the only module in the service
holding a WhatsApp access token: it cannot decide to send anything on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from demo_command_center.contracts.ports import ProviderError, ProviderRejected
from demo_command_center.domain.messages import OutboundMessage, SendOutcome, SendResult
from demo_command_center.observability.logging import get_logger
from demo_command_center.resilience.http import HttpClient, HttpConfig
from demo_command_center.security.urls import UrlPolicy

logger = get_logger("integration.meta")

PROVIDER: Final = "meta_whatsapp"
GRAPH_HOST: Final = "graph.facebook.com"

#: Meta error codes that mean "this will never work", so a retry is pointless.
#: 131047 = outside the session window; 132000 = template does not exist.
_PERMANENT_CODES: frozenset[str] = frozenset({"131047", "131026", "132000", "132001", "133010"})


class MetaWhatsAppSender:
    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        graph_version: str = "v21.0",
        timeout_seconds: float = 10.0,
        enabled: bool = True,
        http: HttpClient | None = None,
    ) -> None:
        self._token = access_token
        self._phone_number_id = phone_number_id
        self._version = graph_version
        self._enabled = enabled and bool(access_token and phone_number_id)
        self._http = http or HttpClient(
            HttpConfig(
                provider=PROVIDER,
                base_url=f"https://{GRAPH_HOST}",
                timeout_seconds=timeout_seconds,
                # Never auto-retried. A WhatsApp send has no idempotency key at
                # the API, so a retried timeout can deliver twice. Redelivery is
                # the queue's job, gated by our own idempotency claim.
                max_retries=0,
            ),
            url_policy=UrlPolicy(allowed_hosts=frozenset({GRAPH_HOST})),
        )

    async def send(self, message: OutboundMessage, *, recipient: str) -> SendResult:
        if not self._enabled:
            logger.info("meta sender disabled; message not delivered")
            return SendResult(
                outcome=SendOutcome.FAILED,
                idempotency_key=message.idempotency_key,
                detail="meta_disabled",
            )

        payload = self._payload(message, recipient)
        try:
            response = await self._http.request(
                "POST",
                f"/{self._version}/{self._phone_number_id}/messages",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                json_body=payload,
                idempotent=False,
            )
        except ProviderError as exc:
            if exc.code in _PERMANENT_CODES:
                logger.warning("meta permanent rejection", extra={"dcc_code": exc.code})
                return SendResult(
                    outcome=SendOutcome.SUPPRESSED_NO_TEMPLATE
                    if exc.code in {"131047", "132000"}
                    else SendOutcome.FAILED,
                    idempotency_key=message.idempotency_key,
                    detail=f"meta:{exc.code}",
                )
            raise

        messages = response.get("messages") or []
        provider_id = str(messages[0].get("id")) if messages else ""
        if not provider_id:
            # A 200 with no message id is a shape we do not understand. Treating
            # it as success would leave a message we cannot correlate to a
            # delivery status.
            raise ProviderRejected(PROVIDER, "no message id in response", code="no_message_id")

        return SendResult(
            outcome=SendOutcome.SENT,
            idempotency_key=message.idempotency_key,
            provider_message_id=provider_id,
            sent_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------- internals
    def _payload(self, message: OutboundMessage, recipient: str) -> dict[str, Any]:
        base: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
        }

        if message.template is not None:
            return {
                **base,
                "type": "template",
                "template": {
                    "name": message.template.name,
                    "language": {"code": _language_code(message.template.language)},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": value}
                                for value in message.template.variables
                            ],
                        }
                    ]
                    if message.template.variables
                    else [],
                },
            }

        if message.buttons:
            return {
                **base,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": message.body[:1024]},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {"id": button.reply_id, "title": button.title},
                            }
                            for button in message.buttons
                        ]
                    },
                },
            }

        return {
            **base,
            "type": "text",
            # Link previews off: a preview renders attacker-controlled content
            # from any URL that reaches a message body.
            "text": {"body": message.body, "preview_url": False},
        }


def _language_code(language: str) -> str:
    """Meta language codes. `hinglish` is not one — it is sent as Hindi.

    Templates for Hinglish content are approved under `hi` with romanised text
    in the body, which is what the WABA actually holds.
    """
    return {"en": "en", "hi": "hi", "hinglish": "hi"}.get(language, "en")
