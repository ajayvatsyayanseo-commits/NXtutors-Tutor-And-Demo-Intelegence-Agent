"""What the gateway client signs must equal what it sends.

The verifier on the Laravel side builds its canonical string from
`getRequestUri()` — path *plus* query, exactly as received. The client signed
the bare path and let the transport append `params` separately, so every call
carrying a query came back 401 while the parameterless ones worked.

That shape of failure is the reason this file exists: it does not look like a
signing bug. `plan_quote` and `discount_eligibility` fail, `resolve_identity`
succeeds, and the obvious reading is an intermittent auth fault on the gateway.
"""

from __future__ import annotations

from typing import Any

import pytest

from demo_command_center.integrations.nxtutors_gateway.client import NxtutorsGatewayClient
from demo_command_center.security.signatures import SignedRequest, sign_internal

pytestmark = pytest.mark.contract

SECRET = "gateway-test-secret"


class RecordingHttp:
    """Captures the path the transport was actually given."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {}
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"method": method, "path": path, **kw})
        return self.response


def client(http: RecordingHttp) -> NxtutorsGatewayClient:
    return NxtutorsGatewayClient(
        base_url="https://www.nxtutors.com",
        signing_secret=SECRET,
        source_id="demo_command_center_agent",
        http=http,  # type: ignore[arg-type]
    )


def verify(call: dict[str, Any]) -> bool:
    """Re-derive the signature the way the Laravel middleware does."""
    expected = sign_internal(
        SECRET,
        SignedRequest(
            method=call["method"],
            # The verifier uses the URI as received, which is what the client
            # handed the transport.
            path=call["path"],
            timestamp=int(call["headers"]["X-Nxt-Timestamp"]),
            body=b"",
        ),
    )
    return expected == call["headers"]["X-Nxt-Signature"]


class TestTheSignatureCoversTheQuery:
    async def test_a_call_with_no_query_verifies(self) -> None:
        http = RecordingHttp({"tutor_ref": "2928", "whatsapp": "+919876543210"})
        await client(http).resolve_tutor_contacts(tutor_ref="2928")
        assert verify(http.calls[0])

    async def test_a_call_with_a_query_verifies(self) -> None:
        """The regression: this returned 401 in production."""
        http = RecordingHttp({"prior_offers": 0, "eligible": True})
        await client(http).discount_eligibility(student_ref="stu_abc", lookback_days=90)
        call = http.calls[0]
        assert "?" in call["path"], "the query must be part of the signed path"
        assert verify(call)

    async def test_the_query_is_not_also_passed_separately(self) -> None:
        """Two sources of truth for the query is how they drift apart again:
        the transport would re-encode `params` and produce a different URI from
        the one that was signed."""
        http = RecordingHttp({"prior_offers": 0, "eligible": True})
        await client(http).discount_eligibility(student_ref="stu_abc", lookback_days=90)
        assert http.calls[0].get("params") is None

    async def test_a_plan_quote_with_parameters_verifies(self) -> None:
        http = RecordingHttp(
            {
                "plan_ref": "plan_1",
                "plan_name": "Monthly",
                "list_price_minor": 480000,
                "currency": "INR",
                "billing_period": "monthly",
            }
        )
        await client(http).plan_quote(student_ref="stu_abc", plan_ref="plan_1")
        call = http.calls[0]
        assert "?" in call["path"]
        assert verify(call)

    async def test_a_path_argument_is_interpolated_before_signing(self) -> None:
        http = RecordingHttp({"tutor_ref": "2928"})
        await client(http).resolve_tutor_contacts(tutor_ref="2928")
        assert "/tutors/2928/contacts" in http.calls[0]["path"]
        assert "{tutor_ref}" not in http.calls[0]["path"]

    async def test_the_agent_identity_header_is_sent(self) -> None:
        """The verifier allowlists on it. An absent identity is a 403 even
        though the signature is perfect."""
        http = RecordingHttp({"tutor_ref": "2928"})
        await client(http).resolve_tutor_contacts(tutor_ref="2928")
        assert http.calls[0]["headers"]["X-Nxt-Source"] == "demo_command_center_agent"
