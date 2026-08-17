"""Opaque identifier generation.

ULID-shaped ids (48-bit millisecond timestamp + 80 bits of randomness, Crockford
base32) rather than UUID4 for anything that lands in a table with a time-ordered
index. Random UUIDs scatter B-tree inserts across the whole index; a monotonic
prefix keeps demo/event/outbox inserts appending to the right-hand page, which
matters once `outbox_events` and `audit_events` are the two biggest tables.

Ids are opaque to the outside world and carry no PII by construction.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from typing import Final

_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
_TIME_CHARS: Final = 10
_RANDOM_CHARS: Final = 16


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def new_ulid(*, now: datetime | None = None) -> str:
    """A 26-character, lexicographically sortable, collision-safe id."""
    moment = now or datetime.now(UTC)
    millis = int(moment.timestamp() * 1000)
    return _encode(millis, _TIME_CHARS) + _encode(
        int.from_bytes(secrets.token_bytes(10), "big"), _RANDOM_CHARS
    )


def prefixed(prefix: str, *, now: datetime | None = None) -> str:
    """`dmo_01J...` — the prefix makes a stray id in a log self-describing."""
    if not prefix or not prefix.isalnum():
        raise ValueError("id prefix must be a short alphanumeric token")
    return f"{prefix}_{new_ulid(now=now)}"


def demo_id(*, now: datetime | None = None) -> str:
    return prefixed("dmo", now=now)


def hold_id(*, now: datetime | None = None) -> str:
    return prefixed("hld", now=now)


def order_ref(*, now: datetime | None = None) -> str:
    """Merchant order reference sent to Cashfree.

    Must be unique per order attempt forever: Cashfree rejects a reused
    reference, and reusing one across two attempts is how a second payment gets
    matched to the first order.
    """
    return prefixed("nxo", now=now)


def trace_id() -> str:
    return f"tr_{secrets.token_hex(8)}"


def conference_request_id() -> str:
    """Google's `conferenceData.createRequest.requestId`.

    Google treats a repeated requestId on the same event as "you already asked
    for this conference" and returns the existing one — which is exactly the
    idempotency we want on retry, and exactly the bug we must avoid across two
    different demos. Never derive this from anything shared between demos.
    """
    return f"nxt-{os.urandom(8).hex()}"
