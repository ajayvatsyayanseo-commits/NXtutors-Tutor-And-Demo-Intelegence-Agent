"""The internal HTTP surface Lead Intake calls.

Pinned as a contract because Lead Intake branches on the response shape. The
non-obvious rule this file exists to protect: **failures are 200s with a status
in the body.** Lead Intake treats a non-2xx as retryable, so returning 500 for
a message we deliberately declined would loop.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tutor_match_meta.api.app import create_app
from tutor_match_meta.contracts.handoff import HandoffStatus

pytestmark = pytest.mark.contract

SECRET = "internal-test-secret"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A real app over the in-memory stack.

    `conftest.py` has already neutralised the environment (no DSN, stub
    provider), so this exercises the real FastAPI routing, the real handoff
    service and the real matching pipeline without touching infrastructure.
    """
    monkeypatch.setenv("TMM_INTERNAL_SECRET", SECRET)
    # Fully rolled out and out of shadow mode: these tests are about the
    # *contract*, and a flagged-off service returns DECLINED for everything,
    # which would make them pass without exercising anything.
    monkeypatch.setenv("TMM_FLAG_ENABLED", "true")
    monkeypatch.setenv("TMM_FLAG_SHADOW_MODE", "false")
    monkeypatch.setenv("TMM_FLAG_PERCENTAGE_ROLLOUT", "100")
    from tutor_match_meta.config.settings import get_settings

    get_settings.cache_clear()
    try:
        yield TestClient(create_app())
    finally:
        get_settings.cache_clear()


def handoff_body(**extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "source": "lead_intake_agent",
        "wa_message_id": "wamid.TEST1",
        "wa_phone": "+919876543210",
        "message_text": "class 10 cbse maths gurgaon home tuition",
        "message_type": "text",
    }
    body.update(extra)
    return body


class TestAuthentication:
    def test_a_missing_secret_is_401(self, client: TestClient) -> None:
        response = client.post("/internal/v1/handoff", json=handoff_body())
        assert response.status_code == 401
        assert response.json()["status"] == "unauthorized"

    def test_a_wrong_secret_is_401(self, client: TestClient) -> None:
        response = client.post(
            "/internal/v1/handoff",
            json=handoff_body(),
            headers={"X-NXTUTORS-INTERNAL-SECRET": "wrong"},
        )
        assert response.status_code == 401

    def test_the_version_endpoint_requires_the_secret(self, client: TestClient) -> None:
        """Not secret data, but an unauthenticated build-identity endpoint
        hands an attacker a version to look up known issues against."""
        assert client.get("/internal/v1/version").status_code == 401


class TestHandoffContract:
    def test_a_valid_handoff_returns_a_status_and_reply_text(self, client: TestClient) -> None:
        response = client.post(
            "/internal/v1/handoff",
            json=handoff_body(),
            headers={"X-NXTUTORS-INTERNAL-SECRET": SECRET},
        )
        assert response.status_code == 200
        body = response.json()
        # The two fields Lead Intake actually reads.
        assert "status" in body
        assert "reply_text" in body
        assert body["status"] in {s.value for s in HandoffStatus}
        assert "schema_version" in body

    def test_a_malformed_payload_is_422_not_500(self, client: TestClient) -> None:
        response = client.post(
            "/internal/v1/handoff",
            json={"wa_message_id": ""},
            headers={"X-NXTUTORS-INTERNAL-SECRET": SECRET},
        )
        assert response.status_code == 422
        assert response.json()["status"] == "invalid_payload"

    def test_a_non_json_body_is_400_not_500(self, client: TestClient) -> None:
        response = client.post(
            "/internal/v1/handoff",
            content=b"not json",
            headers={
                "X-NXTUTORS-INTERNAL-SECRET": SECRET,
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 400

    def test_the_trace_id_is_echoed_when_supplied(self, client: TestClient) -> None:
        response = client.post(
            "/internal/v1/handoff",
            json=handoff_body(),
            headers={
                "X-NXTUTORS-INTERNAL-SECRET": SECRET,
                "X-Trace-Id": "trace-from-lead-intake",
            },
        )
        assert response.json().get("trace_id") == "trace-from-lead-intake"

    def test_a_redelivery_is_marked_duplicate_and_says_nothing(self, client: TestClient) -> None:
        """Recomputing could produce a different shortlist for one message."""
        headers = {"X-NXTUTORS-INTERNAL-SECRET": SECRET}
        body = handoff_body(wa_message_id="wamid.DUPE", conversation_id="c-dupe")
        client.post("/internal/v1/handoff", json=body, headers=headers)
        second = client.post("/internal/v1/handoff", json=body, headers=headers).json()
        assert second.get("duplicate") is True
        assert second["reply_text"] is None


class TestHealthSurfaces:
    def test_health_reports_posture_without_secrets(self, client: TestClient) -> None:
        body = client.get("/internal/v1/health").json()
        assert body["status"] == "ok"
        assert body["integrations"]["internal_secret_configured"] is True
        # A boolean, never the value.
        assert SECRET not in str(body)

    def test_ready_reports_problems_rather_than_crashing(self, client: TestClient) -> None:
        response = client.get("/internal/v1/ready")
        assert response.status_code in (200, 503)
        body = response.json()
        if response.status_code == 503:
            assert body["problems"], "a 503 must name what is not ready"

    def test_version_reports_every_rollback_axis(self, client: TestClient) -> None:
        body = client.get(
            "/internal/v1/version", headers={"X-NXTUTORS-INTERNAL-SECRET": SECRET}
        ).json()
        for field in (
            "app_version",
            "git_sha",
            "schema_revision",
            "default_policy",
            "prompt_versions",
            "models",
            "kill_switches",
            "holding_work",
        ):
            assert field in body, f"/version is missing {field}"

    def test_version_exposes_no_credential(self, client: TestClient) -> None:
        body = client.get(
            "/internal/v1/version", headers={"X-NXTUTORS-INTERNAL-SECRET": SECRET}
        ).text
        assert SECRET not in body
        assert "sk-" not in body

    def test_no_public_docs_are_exposed_when_deployed(self) -> None:
        """`docs_url` is None outside local. A schema dump is a free map of the
        internal surface."""
        import inspect

        from tutor_match_meta.api import app as app_module

        source = inspect.getsource(app_module.create_app)
        assert "docs_url=None if settings.is_deployed" in source
        assert "openapi_url=None if settings.is_deployed" in source
