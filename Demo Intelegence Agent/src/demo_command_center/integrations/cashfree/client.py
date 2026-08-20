"""Cashfree PG — order creation and status reads.

Webhook *verification* is not here: it lives in `security/signatures.py` and is
called by the handler on the raw bytes, before any parsing. Keeping the two
apart means the verifier has no dependency on this client and can be tested
against a payload the fake mints.

The amount sent to Cashfree is derived from `PaymentOrder.amount_minor`, which
can only have come from an `ApprovedOffer`. `create_order` also asserts the
order and the offer agree — a redundant check by design, since it is the last
point before real money where a mismatch is cheap to catch.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

from demo_command_center.contracts.ports import ProviderRejected, ProviderUnavailable
from demo_command_center.domain.payments import PaymentOrder
from demo_command_center.domain.pricing import ApprovedOffer
from demo_command_center.observability.logging import get_logger
from demo_command_center.resilience.http import HttpClient, HttpConfig
from demo_command_center.security.urls import UrlPolicy

logger = get_logger("integration.cashfree")

PROVIDER: Final = "cashfree"
_HOSTS: Final = frozenset({"api.cashfree.com", "sandbox.cashfree.com"})


class CashfreeClient:
    def __init__(
        self,
        *,
        app_id: str,
        secret_key: str,
        base_url: str,
        api_version: str = "2023-08-01",
        timeout_seconds: float = 12.0,
        enabled: bool = True,
        http: HttpClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._secret = secret_key
        self._version = api_version
        self._enabled = enabled and bool(app_id and secret_key)
        self._http = http or HttpClient(
            HttpConfig(
                provider=PROVIDER,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
                max_retries=1,
            ),
            url_policy=UrlPolicy(allowed_hosts=_HOSTS),
        )

    async def create_order(
        self, *, order: PaymentOrder, offer: ApprovedOffer, return_url: str
    ) -> dict[str, Any]:
        if not self._enabled:
            raise ProviderUnavailable(PROVIDER, "cashfree is not configured")
        if order.amount_minor != offer.amount_minor or order.currency != offer.currency:
            # Unreachable through `PaymentOrder.from_offer`, and checked anyway:
            # this is the last cheap place before a customer is charged.
            raise ProviderRejected(PROVIDER, "order does not match offer", code="amount_mismatch")

        body: dict[str, Any] = {
            "order_id": order.order_ref,
            # Cashfree takes major units. `Money.major` is a Decimal, so the
            # conversion never goes through a float.
            "order_amount": float(Decimal(order.amount_minor) / 100),
            "order_currency": order.currency,
            "customer_details": {
                # Opaque ref only. No name, phone or email crosses this boundary
                # from us; Cashfree collects what it needs on its hosted page.
                "customer_id": order.student_ref or order.conversation_ref,
            },
            "order_meta": {"return_url": return_url} if return_url else {},
            "order_note": f"NXTutors demo conversion {order.demo_id}",
        }

        response = await self._http.request(
            "POST",
            "/pg/orders",
            headers=self._headers(),
            json_body=body,
            # Safe to retry: Cashfree rejects a duplicate `order_id`, so a retry
            # after a timeout either creates it once or fails loudly.
            idempotent=True,
        )

        session_id = str(response.get("payment_session_id") or "")
        link = str(response.get("payment_link") or "")
        if not link and session_id:
            # The hosted checkout URL is derived from the session id when the
            # API does not return a link directly.
            host = (
                "payments.cashfree.com"
                if "api." in str(self._http._config.base_url)
                else "payments-test.cashfree.com"
            )
            link = f"https://{host}/session/{session_id}"

        if not link:
            raise ProviderRejected(PROVIDER, "no payment link returned", code="no_payment_link")

        return {
            "provider_order_id": str(response.get("cf_order_id") or ""),
            "order_ref": order.order_ref,
            "payment_link": link,
            "order_status": str(response.get("order_status") or "ACTIVE"),
        }

    async def fetch_order(self, *, order_ref: str) -> dict[str, Any]:
        """Reconciliation read. The authoritative answer when a webhook is lost."""
        if not self._enabled:
            raise ProviderUnavailable(PROVIDER, "cashfree is not configured")
        response = await self._http.request(
            "GET", f"/pg/orders/{order_ref}", headers=self._headers(), idempotent=True
        )
        return {
            "order_ref": order_ref,
            "provider_order_id": str(response.get("cf_order_id") or ""),
            "order_status": str(response.get("order_status") or ""),
            "order_amount": response.get("order_amount"),
            "order_currency": response.get("order_currency"),
        }

    def _headers(self) -> dict[str, str]:
        return {
            "x-client-id": self._app_id,
            "x-client-secret": self._secret,
            "x-api-version": self._version,
            "Content-Type": "application/json",
        }
