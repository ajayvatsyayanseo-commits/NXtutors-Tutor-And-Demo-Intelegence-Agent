"""Recipient-ref → deliverable address, resolved at send time.

Deliberately the *only* place a real phone number is produced, and it produces
one exactly when a message is about to go out. Nothing upstream stores one: the
domain works in opaque refs, the outbox holds refs, and the message log holds
refs. A dump of any Demo table therefore contains no phone numbers.

Resolutions are cached briefly. A revoked opt-out taking up to a minute to
apply is a real, bounded exposure; the bound is what makes it acceptable, and
the cache is what stops every reminder in a batch making its own gateway call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from demo_command_center.contracts.ports import NxtutorsGatewayPort, ProviderError
from demo_command_center.observability.logging import get_logger

logger = get_logger("integration.contacts")

DEFAULT_TTL = timedelta(seconds=60)


@dataclass(slots=True)
class _Entry:
    phone: str | None
    opted_out: bool
    fetched_at: datetime


class GatewayContactResolver:
    def __init__(self, gateway: NxtutorsGatewayPort, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._gateway = gateway
        self._ttl = ttl
        self._cache: dict[str, _Entry] = {}

    async def resolve(self, recipient_ref: str) -> str | None:
        entry = await self._entry(recipient_ref)
        if entry is None or entry.opted_out:
            return None
        return entry.phone

    async def opted_out(self, recipient_ref: str) -> bool:
        entry = await self._entry(recipient_ref)
        # Unknown means opted out. Failing closed is right here: the cost of a
        # missed reminder is one annoyed parent; the cost of messaging someone
        # who opted out is a WABA quality-rating hit.
        return entry is None or entry.opted_out

    async def _entry(self, recipient_ref: str) -> _Entry | None:
        now = datetime.now(UTC)
        cached = self._cache.get(recipient_ref)
        if cached is not None and now - cached.fetched_at < self._ttl:
            return cached

        try:
            record = await self._gateway.resolve_tutor_contacts(tutor_ref=recipient_ref)
        except ProviderError as exc:
            logger.warning(
                "contact resolution failed", extra={"dcc_code": exc.code or "unavailable"}
            )
            # A stale entry beats no entry when the gateway is down: it lets an
            # in-flight reminder batch finish rather than silently dropping it.
            return cached

        entry = _Entry(
            phone=_first_str(record, "whatsapp", "phone", "msisdn"),
            opted_out=bool(record.get("opted_out") or record.get("do_not_contact")),
            fetched_at=now,
        )
        self._cache[recipient_ref] = entry
        # Bounded: a warm container resolving many refs must not grow forever.
        if len(self._cache) > 2_000:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1].fetched_at)
            for ref, _ in oldest[:500]:
                self._cache.pop(ref, None)
        return entry


def _first_str(record: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
