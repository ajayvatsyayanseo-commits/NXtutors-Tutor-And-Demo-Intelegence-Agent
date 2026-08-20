"""The Google token provider.

Filed under `security` because it is the one place a Google credential is read,
and because the bug it fixes was total: the calendar client takes a
`token_provider`, nothing built one, and `_access_token` raised on every call.
Scheduling could never produce a Meet link no matter how it was configured.
"""

from __future__ import annotations

import json

import pytest

from demo_command_center.contracts.ports import ProviderRejected, ProviderUnavailable
from demo_command_center.integrations.google_calendar.credentials import (
    REQUIRED_FIELDS,
    GoogleTokenProvider,
    build_token_provider,
)

pytestmark = pytest.mark.security

CREDENTIAL = {
    "client_id": "cid.apps.googleusercontent.com",
    "client_secret": "gsecret",
    "refresh_token": "1//refresh",
}


class FakeHttp:
    """Counts exchanges so the caching contract is observable."""

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses) or [{"access_token": "ya29.token", "expires_in": 3600}]
        self.calls: list[dict] = []

    async def request(self, method: str, path: str, **kw: object) -> dict:
        self.calls.append({"method": method, "path": path, **kw})
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def provider(http: FakeHttp | None = None, **kw: object) -> GoogleTokenProvider:
    return GoogleTokenProvider(
        credentials_json=json.dumps(CREDENTIAL),
        http=http or FakeHttp(),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


class TestTheExchange:
    async def test_it_returns_an_access_token(self) -> None:
        assert await provider().token() == "ya29.token"

    async def test_it_posts_form_encoded_not_json(self) -> None:
        """RFC 6749 4.1.3 specifies form encoding and Google rejects a JSON
        body, so a client that can only send JSON never obtains a token."""
        http = FakeHttp()
        await provider(http).token()
        call = http.calls[0]
        assert call["method"] == "POST"
        assert call["form_body"]["grant_type"] == "refresh_token"
        assert call.get("json_body") is None
        assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"

    async def test_every_credential_field_is_sent(self) -> None:
        http = FakeHttp()
        await provider(http).token()
        for field in REQUIRED_FIELDS:
            assert http.calls[0]["form_body"][field] == CREDENTIAL[field]

    async def test_the_exchange_is_retryable(self) -> None:
        """It creates no resource; a repeat just returns another valid token."""
        http = FakeHttp()
        await provider(http).token()
        assert http.calls[0]["idempotent"] is True


class TestCaching:
    async def test_a_live_token_is_reused(self) -> None:
        http = FakeHttp()
        p = provider(http)
        for _ in range(5):
            await p.token()
        assert len(http.calls) == 1, "one exchange should serve every caller"

    async def test_a_token_expiring_within_the_skew_is_refreshed(self) -> None:
        """A token that dies mid-flight fails the booking it was fetched for."""
        http = FakeHttp({"access_token": "first", "expires_in": 1})
        p = provider(http)
        assert await p.token() == "first"
        assert await p.token() == "first"
        assert len(http.calls) == 2, "expires_in=1 is inside the skew, so refresh"

    async def test_a_response_without_a_token_is_unavailable_not_empty(self) -> None:
        http = FakeHttp({"expires_in": 3600})
        with pytest.raises(ProviderUnavailable):
            await provider(http).token()


class TestCredentialShapes:
    async def test_the_console_download_shape_is_accepted(self) -> None:
        """The Google console nests client fields under `installed`."""
        nested = {
            "installed": {"client_id": "cid", "client_secret": "sec"},
            "refresh_token": "1//r",
        }
        p = GoogleTokenProvider(credentials_json=json.dumps(nested), http=FakeHttp())  # type: ignore[arg-type]
        assert await p.token() == "ya29.token"

    @pytest.mark.parametrize("missing", sorted(REQUIRED_FIELDS))
    async def test_a_missing_field_is_named(self, missing: str) -> None:
        partial = {k: v for k, v in CREDENTIAL.items() if k != missing}
        p = GoogleTokenProvider(credentials_json=json.dumps(partial), http=FakeHttp())  # type: ignore[arg-type]
        with pytest.raises(ProviderRejected) as exc:
            await p.token()
        assert missing in str(exc.value), "the operator must be told which field"

    async def test_malformed_json_is_rejected_clearly(self) -> None:
        p = GoogleTokenProvider(credentials_json="{not json", http=FakeHttp())  # type: ignore[arg-type]
        with pytest.raises(ProviderRejected):
            await p.token()

    async def test_no_credential_at_all_names_the_setting_to_fix(self) -> None:
        p = GoogleTokenProvider(http=FakeHttp())  # type: ignore[arg-type]
        with pytest.raises(ProviderRejected) as exc:
            await p.token()
        assert "DCC_GOOGLE_CREDENTIALS_SECRET" in str(exc.value)

    async def test_service_account_mode_explains_why_it_is_unsupported(self) -> None:
        p = GoogleTokenProvider(
            auth_mode="service_account",
            credentials_json=json.dumps(CREDENTIAL),
            http=FakeHttp(),  # type: ignore[arg-type]
        )
        with pytest.raises(ProviderRejected) as exc:
            await p.token()
        assert "oauth_refresh" in str(exc.value), "say what to switch to, not just 'no'"


class _Environment:
    def __init__(self, value: str) -> None:
        self.value = value


class _settings_stub:
    """The four attributes `build_token_provider` actually reads."""

    def __init__(
        self,
        *,
        environment: str = "local",
        google_enabled: bool = True,
        google_auth_mode: str = "oauth_refresh",
        google_credentials_secret: str = "demo/google",  # noqa: S107 - a secret NAME, not a secret
        google_timeout_seconds: float = 12.0,
    ) -> None:
        self.environment = _Environment(environment)
        self.google_enabled = google_enabled
        self.google_auth_mode = google_auth_mode
        self.google_credentials_secret = google_credentials_secret
        self.google_timeout_seconds = google_timeout_seconds


class TestWiring:
    def test_no_provider_when_google_is_disabled(self) -> None:
        from demo_command_center.config.settings import Settings

        assert build_token_provider(Settings(google_enabled=False)) is None

    def test_an_inline_credential_is_refused_outside_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A laptop convenience must not become a deployed Lambda env var.

        Built from a stub rather than a real `Settings`: a staging Settings
        demands a dozen unrelated deployment values, and a test that has to
        satisfy all of them to reach one branch breaks whenever any of them
        changes.
        """
        monkeypatch.setenv("DCC_GOOGLE_CREDENTIALS_JSON", json.dumps(CREDENTIAL))
        with pytest.raises(ProviderRejected) as exc:
            build_token_provider(_settings_stub(environment="staging"))
        assert "must not be set outside local" in str(exc.value)

    def test_an_inline_credential_is_allowed_locally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DCC_GOOGLE_CREDENTIALS_JSON", json.dumps(CREDENTIAL))
        assert build_token_provider(_settings_stub(environment="local")) is not None

    def test_the_calendar_client_is_given_a_provider(self) -> None:
        """The regression itself: a client built without one can never work."""
        import inspect

        from demo_command_center import bootstrap

        source = inspect.getsource(bootstrap.build_dependencies)
        assert "token_provider=build_token_provider" in source
