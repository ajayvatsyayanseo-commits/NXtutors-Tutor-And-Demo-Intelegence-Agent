"""Google Calendar + Meet.

Two behaviours here are not obvious from the API docs and both cause real bugs:

* **`conferenceData` is created asynchronously.** The create response often
  comes back with `createRequest.status = "pending"` and no `entryPoints`.
  Returning at that point yields a demo with no join link. So we poll the event
  a bounded number of times, and if the conference never materialises we say so
  — the scheduling capability then cancels the event rather than confirming an
  online demo nobody can join.
* **`requestId` is the idempotency key.** Reusing one on the same event returns
  the existing conference, which is exactly what a retry wants. Reusing one
  across two different demos silently gives them the same Meet room, which is
  why `shared/ids.conference_request_id()` is random per call.

Authentication is JWT-bearer against a service account with domain-wide
delegation, or an OAuth refresh token. The credential itself lives in Secrets
Manager and is fetched at cold start.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from demo_command_center.contracts.ports import ProviderRejected, ProviderUnavailable
from demo_command_center.domain.slots import TimeSlot
from demo_command_center.observability.logging import get_logger
from demo_command_center.resilience.http import HttpClient, HttpConfig
from demo_command_center.security.urls import UrlPolicy

logger = get_logger("integration.google")

PROVIDER: Final = "google_calendar"
API_HOST: Final = "www.googleapis.com"
_ALLOWED = frozenset({API_HOST, "oauth2.googleapis.com"})


class GoogleCalendarClient:
    def __init__(
        self,
        *,
        calendar_id: str = "primary",
        organizer_email: str = "",
        credentials_secret: str = "",
        timeout_seconds: float = 12.0,
        poll_attempts: int = 4,
        poll_delay_seconds: float = 1.5,
        enabled: bool = True,
        http: HttpClient | None = None,
        token_provider: Any = None,
    ) -> None:
        self._calendar_id = calendar_id
        self._organizer = organizer_email
        self._secret_name = credentials_secret
        self._poll_attempts = poll_attempts
        self._poll_delay = poll_delay_seconds
        self._enabled = enabled and bool(credentials_secret)
        self._token_provider = token_provider
        self._http = http or HttpClient(
            HttpConfig(
                provider=PROVIDER,
                base_url=f"https://{API_HOST}",
                timeout_seconds=timeout_seconds,
                max_retries=2,
            ),
            url_policy=UrlPolicy(allowed_hosts=_ALLOWED),
        )

    async def create_event(
        self,
        *,
        summary: str,
        description: str,
        slot: TimeSlot,
        attendee_emails: tuple[str, ...],
        with_conference: bool,
        conference_request_id: str,
        location: str | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        body: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": slot.starts_at.isoformat(), "timeZone": slot.timezone},
            "end": {"dateTime": slot.ends_at.isoformat(), "timeZone": slot.timezone},
            "attendees": [{"email": email} for email in attendee_emails],
            # Guests must not invite others or see each other's contact details.
            "guestsCanInviteOthers": False,
            "guestsCanSeeOtherGuests": False,
            "reminders": {"useDefault": False},
        }
        if location:
            body["location"] = location
        if with_conference:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": conference_request_id,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }

        created = await self._http.request(
            "POST",
            f"/calendar/v3/calendars/{self._calendar_id}/events",
            headers=await self._headers(),
            json_body=body,
            params={
                "conferenceDataVersion": "1" if with_conference else "0",
                "sendUpdates": "all",
            },
            # Safe to retry: Google dedupes on the conference requestId, and a
            # duplicate event would have the same one.
            idempotent=True,
        )

        event_id = str(created.get("id") or "")
        if not event_id:
            raise ProviderRejected(PROVIDER, "event created without an id", code="no_event_id")

        meet_url = _conference_uri(created)
        if with_conference and not meet_url:
            meet_url = await self._await_conference(event_id)

        return {
            "event_id": event_id,
            "meet_url": meet_url,
            "html_link": created.get("htmlLink", ""),
            "start": (created.get("start") or {}).get("dateTime", ""),
            "attendees": created.get("attendees") or [],
        }

    async def patch_event(
        self, *, event_id: str, slot: TimeSlot | None = None, description: str | None = None
    ) -> dict[str, Any]:
        self._require_enabled()
        body: dict[str, Any] = {}
        if slot is not None:
            body["start"] = {"dateTime": slot.starts_at.isoformat(), "timeZone": slot.timezone}
            body["end"] = {"dateTime": slot.ends_at.isoformat(), "timeZone": slot.timezone}
        if description is not None:
            body["description"] = description
        if not body:
            return {"event_id": event_id}

        patched = await self._http.request(
            "PATCH",
            f"/calendar/v3/calendars/{self._calendar_id}/events/{event_id}",
            headers=await self._headers(),
            json_body=body,
            params={"sendUpdates": "all"},
            idempotent=True,
        )
        return {
            "event_id": str(patched.get("id") or event_id),
            "meet_url": _conference_uri(patched),
        }

    async def cancel_event(self, *, event_id: str) -> None:
        self._require_enabled()
        await self._http.request(
            "DELETE",
            f"/calendar/v3/calendars/{self._calendar_id}/events/{event_id}",
            headers=await self._headers(),
            params={"sendUpdates": "all"},
            idempotent=True,
        )

    async def get_event(self, *, event_id: str) -> dict[str, Any]:
        self._require_enabled()
        event = await self._http.request(
            "GET",
            f"/calendar/v3/calendars/{self._calendar_id}/events/{event_id}",
            headers=await self._headers(),
            idempotent=True,
        )
        return {
            "event_id": event.get("id"),
            "attendees": [
                {
                    "email": a.get("email"),
                    "responseStatus": a.get("responseStatus"),
                    "role": _role(a),
                }
                for a in (event.get("attendees") or [])
            ],
            "status": event.get("status"),
            "meet_url": _conference_uri(event),
        }

    # ------------------------------------------------------------- internals
    async def _await_conference(self, event_id: str) -> str:
        """Re-read until the conference appears, or give up honestly."""
        for attempt in range(self._poll_attempts):
            await asyncio.sleep(self._poll_delay)
            event = await self._http.request(
                "GET",
                f"/calendar/v3/calendars/{self._calendar_id}/events/{event_id}",
                headers=await self._headers(),
                idempotent=True,
            )
            uri = _conference_uri(event)
            if uri:
                return uri
            logger.info("conference not ready", extra={"dcc_attempt": str(attempt + 1)})
        return ""

    async def _headers(self) -> dict[str, str]:
        token = await self._access_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _access_token(self) -> str:
        if self._token_provider is None:
            raise ProviderUnavailable(PROVIDER, "no google credential provider configured")
        token = await self._token_provider.token()
        if not token:
            raise ProviderUnavailable(PROVIDER, "google credential provider returned no token")
        return str(token)

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise ProviderUnavailable(PROVIDER, "google calendar is not configured")


def _conference_uri(event: dict[str, Any]) -> str:
    """The video entry point, or "".

    Only `entryPointType == "video"` is accepted. A conference also carries
    phone entry points, and returning one of those as "the Meet link" sends a
    parent a dial-in number where they expect a URL.
    """
    conference = event.get("conferenceData") or {}
    for entry in conference.get("entryPoints") or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return str(entry["uri"])
    return str(conference.get("hangoutLink") or event.get("hangoutLink") or "")


def _role(attendee: dict[str, Any]) -> str:
    if attendee.get("organizer"):
        return "organizer"
    return str(attendee.get("comment") or "attendee")
