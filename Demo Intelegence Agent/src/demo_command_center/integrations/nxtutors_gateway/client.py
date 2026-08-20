"""The NXTutors website gateway client.

The only route to NXTutors data. There is no MySQL driver in this project's
dependencies, which makes "no direct database access" a property of the build
rather than a rule people have to remember.

**Every endpoint here is unverified.** The Laravel package
`packages/nxtutors/demo-command-center-adapter` was not readable from this
workspace, so paths, verbs and field names are pinned to the fixture contracts
in `tests/fixtures/gateway/` and recorded in `docs/integration-gaps.md`. They are
deliberately gathered into the `_ENDPOINTS` table below so correcting them once
the package is readable is a single-place edit rather than a search.

Requests are signed with the same HMAC scheme the Tutor Intelligence service
uses against the same gateway, so the Laravel side needs one verifier, not two.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlencode

from demo_command_center.contracts.ports import ProviderRejected
from demo_command_center.domain.pricing import PlanQuote
from demo_command_center.domain.slots import TimeSlot
from demo_command_center.observability.logging import get_logger
from demo_command_center.resilience.http import HttpClient, HttpConfig
from demo_command_center.security.signatures import SignedRequest, sign_internal
from demo_command_center.security.urls import UrlPolicy

logger = get_logger("integration.gateway")

PROVIDER: Final = "nxtutors_gateway"

#: UNVERIFIED. See the module docstring and docs/integration-gaps.md.
_ENDPOINTS: Final[dict[str, tuple[str, str]]] = {
    "resolve_identity": ("POST", "/api/agent/v1/identity/resolve"),
    "tutor_contacts": ("GET", "/api/agent/v1/tutors/{tutor_ref}/contacts"),
    "tutor_availability": ("GET", "/api/agent/v1/tutors/{tutor_ref}/availability"),
    "plan_quote": ("GET", "/api/agent/v1/plans/quote"),
    "discount_eligibility": ("GET", "/api/agent/v1/customers/discount-eligibility"),
    "record_demo": ("POST", "/api/agent/v1/demos"),
    "activate_subscription": ("POST", "/api/agent/v1/subscriptions/activate"),
    "region_authorization": ("GET", "/api/agent/v1/operators/{operator_ref}/regions"),
}


class NxtutorsGatewayClient:
    def __init__(
        self,
        *,
        base_url: str,
        signing_secret: str,
        signing_key_id: str = "v1",
        source_id: str = "demo_command_center_agent",
        timeout_seconds: float = 8.0,
        max_retries: int = 2,
        http: HttpClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret = signing_secret
        self._key_id = signing_key_id
        self._source = source_id
        self._enabled = bool(base_url and signing_secret)
        host = _host_of(base_url)
        self._http = http or HttpClient(
            HttpConfig(
                provider=PROVIDER,
                base_url=self._base_url,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            ),
            url_policy=UrlPolicy(allowed_hosts=frozenset({host})) if host else UrlPolicy(),
        )

    # ------------------------------------------------------------ operations
    async def resolve_identity(self, *, phone_hash: str, conversation_ref: str) -> dict[str, Any]:
        return await self._call(
            "resolve_identity",
            body={"phone_hash": phone_hash, "conversation_ref": conversation_ref},
            idempotent=True,
        )

    async def resolve_tutor_contacts(self, *, tutor_ref: str) -> dict[str, Any]:
        return await self._call(
            "tutor_contacts", path_args={"tutor_ref": tutor_ref}, idempotent=True
        )

    async def tutor_availability(
        self, *, tutor_ref: str, from_at: datetime, to_at: datetime
    ) -> list[TimeSlot]:
        body = await self._call(
            "tutor_availability",
            path_args={"tutor_ref": tutor_ref},
            params={"from": from_at.isoformat(), "to": to_at.isoformat()},
            idempotent=True,
        )
        return _parse_slots(body.get("slots") or [])

    async def plan_quote(self, *, student_ref: str | None, plan_ref: str | None) -> PlanQuote:
        params = {k: v for k, v in {"student_ref": student_ref, "plan_ref": plan_ref}.items() if v}
        body = await self._call("plan_quote", params=params, idempotent=True)
        try:
            return PlanQuote(
                plan_ref=str(body["plan_ref"]),
                plan_name=str(body.get("plan_name") or body["plan_ref"]),
                # Minor units are demanded, never derived from a float price.
                # Reconstructing paise from a JSON float is where 4799.99 * 100
                # becomes 479998.
                list_price_minor=int(body["list_price_minor"]),
                currency=str(body.get("currency") or "INR"),
                billing_period=str(body.get("billing_period") or "monthly"),
                inclusions=tuple(str(item) for item in (body.get("inclusions") or [])),
                fetched_at=datetime.now().astimezone(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderRejected(
                PROVIDER, "plan quote response was malformed", code="bad_quote"
            ) from exc

    async def discount_eligibility(
        self, *, student_ref: str | None, lookback_days: int
    ) -> dict[str, Any]:
        return await self._call(
            "discount_eligibility",
            params={"student_ref": student_ref or "", "lookback_days": str(lookback_days)},
            idempotent=True,
        )

    async def record_demo(self, *, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return await self._call(
            "record_demo", body=payload, idempotency_key=idempotency_key, idempotent=True
        )

    async def activate_subscription(
        self, *, order_ref: str, student_ref: str | None, idempotency_key: str
    ) -> dict[str, Any]:
        """Idempotent by contract. The key is what makes a retry safe.

        `idempotent=True` on a POST is correct *because* of the key: without it
        a timeout-then-retry would create a second subscription for one payment.
        """
        return await self._call(
            "activate_subscription",
            body={"order_ref": order_ref, "student_ref": student_ref},
            idempotency_key=idempotency_key,
            idempotent=True,
        )

    async def region_authorization(self, *, operator_ref: str) -> list[str]:
        body = await self._call(
            "region_authorization", path_args={"operator_ref": operator_ref}, idempotent=True
        )
        return [str(region) for region in (body.get("regions") or [])]

    # ------------------------------------------------------------- internals
    async def _call(
        self,
        operation: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        path_args: dict[str, str] | None = None,
        idempotency_key: str = "",
        idempotent: bool = False,
    ) -> dict[str, Any]:
        if not self._enabled:
            raise ProviderRejected(
                PROVIDER, "gateway is not configured", code="gateway_not_configured"
            )
        method, template = _ENDPOINTS[operation]
        path = template
        for key, value in (path_args or {}).items():
            # Path segments are refs matched by the opaque `_REF` pattern, so
            # they cannot contain a slash or a traversal sequence.
            path = path.replace("{" + key + "}", _safe_segment(value))

        # The signed path MUST include the query string.
        #
        # The verifier builds its canonical string from Laravel's
        # `getRequestUri()`, which is path *plus* query exactly as sent. Signing
        # the bare path while letting httpx append `params` produced a valid
        # signature over a different string, so every call carrying a query —
        # `plan_quote`, `discount_eligibility`, `tutor_availability` — came back
        # 401 while the parameterless ones worked. That reads as an
        # intermittent auth fault rather than a signing bug.
        #
        # Encoded here, once, and then handed to the transport already in the
        # URL: letting httpx re-encode `params` separately is how the two
        # strings drift apart again on the first value containing a space.
        signed_path = f"{path}?{urlencode(params)}" if params else path

        raw = json.dumps(body, separators=(",", ":")).encode("utf-8") if body else b""
        timestamp = int(time.time())
        signature = sign_internal(
            self._secret,
            SignedRequest(method=method, path=signed_path, timestamp=timestamp, body=raw),
        )
        headers = {
            "X-Nxt-Signature": signature,
            "X-Nxt-Timestamp": str(timestamp),
            "X-Nxt-Key-Id": self._key_id,
            "X-Nxt-Source": self._source,
        }
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        if body:
            headers["Content-Type"] = "application/json"

        return await self._http.request(
            method, signed_path, headers=headers, json_body=body, idempotent=idempotent
        )


def _parse_slots(rows: list[Any]) -> list[TimeSlot]:
    """Malformed rows are skipped, not fatal.

    A gateway that returns one bad slot among twenty should cost us that slot,
    not the whole booking flow.
    """
    slots: list[TimeSlot] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            slots.append(
                TimeSlot(
                    starts_at=datetime.fromisoformat(str(row["starts_at"])),
                    duration_minutes=int(row.get("duration_minutes") or 45),
                    timezone=str(row.get("timezone") or "Asia/Kolkata"),
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("skipping malformed availability slot")
    return slots


def _safe_segment(value: str) -> str:
    if "/" in value or ".." in value:
        raise ProviderRejected(PROVIDER, "unsafe path segment", code="bad_ref")
    return value


def _host_of(base_url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(base_url).hostname or "").lower()
