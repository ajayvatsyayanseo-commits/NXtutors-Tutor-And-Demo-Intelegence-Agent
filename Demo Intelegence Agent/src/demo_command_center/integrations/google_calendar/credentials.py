"""Google access tokens for the calendar client.

`GoogleCalendarClient` takes a `token_provider` and calls `.token()` before
every request. Nothing implemented one, so `_access_token` raised
`no google credential provider configured` on every call — the calendar was
structurally unreachable even with a valid credential configured, and a demo
could never get a Meet link. This is that provider.

**Only `oauth_refresh` is implemented, deliberately.** A service-account
credential is a JWT the client must sign with RS256, which needs the
`cryptography` package. This package ships inside every Lambda, where each
dependency is a cold-start cost paid on every invocation, and the refresh-token
exchange is a plain HTTPS POST needing no signing at all. `service_account`
therefore raises a message naming exactly what is missing, rather than failing
obscurely at the first booking.

The credential is never an environment variable in a deployed environment. It
lives in Secrets Manager under `google_credentials_secret` and is fetched once
per container. `DCC_GOOGLE_CREDENTIALS_JSON` exists for local development and
is refused outside `local`, so a laptop convenience cannot quietly become a
credential sitting in a Lambda's environment block.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Final

from demo_command_center.contracts.ports import ProviderRejected, ProviderUnavailable
from demo_command_center.observability.logging import get_logger
from demo_command_center.resilience.http import HttpClient, HttpConfig
from demo_command_center.security.urls import UrlPolicy

logger = get_logger("integration.google.credentials")

PROVIDER: Final = "google"

# hostname and a URL path, and neither is a credential.
TOKEN_HOST: Final = "oauth2.googleapis.com"  # noqa: S105
TOKEN_PATH: Final = "/token"  # noqa: S105

#: Refresh this long before the token actually expires. A token that dies
#: mid-flight fails the calendar call it was fetched for, and Google's clock is
#: not ours — a minute of slack costs one extra exchange per hour at most.
EXPIRY_SKEW_SECONDS: Final = 60

#: Fields a refresh-token credential must carry. Checked up front so a
#: half-populated secret fails with the missing key named, rather than as an
#: opaque 400 from Google on the first real booking.
REQUIRED_FIELDS: Final = ("client_id", "client_secret", "refresh_token")


class GoogleTokenProvider:
    """Exchanges a refresh token for an access token, and caches it.

    One instance per container. The cache is guarded by a lock because the
    scheduling worker processes an SQS batch concurrently: without it a cold
    container fires one token exchange per in-flight booking, and Google
    rate-limits that endpoint, so the excess bookings fail rather than merely
    costing time.
    """

    def __init__(
        self,
        *,
        auth_mode: str = "oauth_refresh",
        credentials_secret: str = "",
        credentials_json: str = "",
        secrets_client: Any = None,
        http: HttpClient | None = None,
        timeout_seconds: float = 12.0,
    ) -> None:
        self._auth_mode = auth_mode
        self._secret_name = credentials_secret
        self._inline_json = credentials_json
        self._secrets = secrets_client
        self._credential: dict[str, str] | None = None
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._http = http or HttpClient(
            HttpConfig(
                provider=PROVIDER,
                base_url=f"https://{TOKEN_HOST}",
                timeout_seconds=timeout_seconds,
                max_retries=2,
            ),
            url_policy=UrlPolicy(allowed_hosts=frozenset({TOKEN_HOST})),
        )

    async def token(self) -> str:
        """A valid access token, served from cache while one is still good."""
        if self._usable():
            return self._token

        async with self._lock:
            # Re-checked inside the lock: several callers queue on a cold
            # container and only the first should perform the exchange.
            if self._usable():
                return self._token

            credential = self._load_credential()
            body = await self._http.request(
                "POST",
                TOKEN_PATH,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                form_body={
                    "client_id": credential["client_id"],
                    "client_secret": credential["client_secret"],
                    "refresh_token": credential["refresh_token"],
                    "grant_type": "refresh_token",
                },
                # Safe to retry: the exchange creates no resource, and a repeat
                # simply returns another valid token.
                idempotent=True,
            )

            token = str(body.get("access_token") or "")
            if not token:
                raise ProviderUnavailable(PROVIDER, "token response carried no access_token")

            self._token = token
            # Google returns seconds-to-live, not an absolute expiry.
            self._expires_at = time.time() + float(body.get("expires_in") or 3600)
            logger.info("google access token refreshed")
            return self._token

    def _usable(self) -> bool:
        return bool(self._token) and time.time() < self._expires_at - EXPIRY_SKEW_SECONDS

    # ------------------------------------------------------------ credential
    def _load_credential(self) -> dict[str, str]:
        if self._credential is not None:
            return self._credential

        if self._auth_mode != "oauth_refresh":
            raise ProviderRejected(
                PROVIDER,
                f"google_auth_mode={self._auth_mode!r} is not implemented. A service "
                "account credential is a JWT signed with RS256, which needs the "
                "cryptography package this Lambda deliberately does not ship. Set "
                "DCC_GOOGLE_AUTH_MODE=oauth_refresh and supply client_id, "
                "client_secret and refresh_token.",
                code="unsupported_auth_mode",
            )

        raw = self._inline_json or self._fetch_secret()
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ProviderRejected(
                PROVIDER, "google credential is not valid JSON", code="bad_credential"
            ) from exc
        if not isinstance(parsed, dict):
            raise ProviderRejected(
                PROVIDER, "google credential must be a JSON object", code="bad_credential"
            )

        # Accept the shape the Google console downloads for an installed app,
        # which nests the client fields under "installed" or "web". The refresh
        # token is added alongside, so outer keys win on conflict.
        for wrapper in ("installed", "web"):
            nested = parsed.get(wrapper)
            if isinstance(nested, dict):
                parsed = {**nested, **{k: v for k, v in parsed.items() if k != wrapper}}
                break

        missing = [field for field in REQUIRED_FIELDS if not parsed.get(field)]
        if missing:
            raise ProviderRejected(
                PROVIDER,
                f"google credential is missing {', '.join(missing)}",
                code="incomplete_credential",
            )

        self._credential = {field: str(parsed[field]) for field in REQUIRED_FIELDS}
        return self._credential

    def _fetch_secret(self) -> str:
        if not self._secret_name:
            raise ProviderRejected(
                PROVIDER,
                "no google credential configured: point DCC_GOOGLE_CREDENTIALS_SECRET at "
                "a Secrets Manager secret holding client_id, client_secret and "
                "refresh_token",
                code="no_credential",
            )
        client = self._secrets or _secrets_client()
        try:
            response = client.get_secret_value(SecretId=self._secret_name)
        except Exception as exc:  # pragma: no cover - AWS-gated
            raise ProviderUnavailable(PROVIDER, f"secrets manager: {type(exc).__name__}") from exc
        return str(response.get("SecretString") or "")


def _secrets_client() -> Any:  # pragma: no cover - AWS-gated
    import boto3

    return boto3.client("secretsmanager")


def build_token_provider(settings: Any) -> GoogleTokenProvider | None:
    """The provider the calendar client needs, or None when Google is off.

    Returning None rather than a stub keeps the failure honest: the client
    already refuses to act without a provider, whereas a stub returning ""
    would turn "not configured" into an authentication failure against Google
    and send an operator looking in the wrong place.
    """
    if not settings.google_enabled:
        return None

    inline = os.environ.get("DCC_GOOGLE_CREDENTIALS_JSON", "")
    if inline and settings.environment.value != "local":
        raise ProviderRejected(
            PROVIDER,
            "DCC_GOOGLE_CREDENTIALS_JSON is a local-development convenience and must "
            "not be set outside local; use DCC_GOOGLE_CREDENTIALS_SECRET",
            code="inline_credential_outside_local",
        )

    return GoogleTokenProvider(
        auth_mode=settings.google_auth_mode,
        credentials_secret=settings.google_credentials_secret,
        credentials_json=inline,
        timeout_seconds=settings.google_timeout_seconds,
    )
