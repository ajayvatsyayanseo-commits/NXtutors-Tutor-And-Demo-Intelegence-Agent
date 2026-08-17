"""Chitragupta Memory Gateway client.

Wire-compatible with the official SDK
(`nxtutors-chitragupta-memory/sdk/python/chitragupta_client`), vendored rather
than imported so the Lambda bundle does not depend on that repo being
pip-installable. `tests/contract/test_chitragupta_contract.py` pins the two
together — the deed-type regex, the lifecycle vocabulary, the required fields
and the secret-shaped-key refusal are all asserted against the real SDK's rules.

Failure posture matches the SDK's documented acknowledgement contract:

* a durable gateway ack, or
* a local WAL spool, or
* a raised error — and this service converts that last case into
  `record() -> False` so **memory being down never blocks a match**.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.repositories.ports import MemoryFact, MemoryPacket

logger = get_logger("chitragupta")

#: Mirrors the SDK's validation exactly.
DEED_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_\.]{2,63}$")
LIFECYCLE_STATUSES = frozenset({"started", "progress", "completed", "failed"})
REQUIRED_KEYS = (
    "trace_id",
    "source_service",
    "agent_id",
    "deed_type",
    "lifecycle_status",
    "purpose",
)
_SECRET_KEY_RE = re.compile(r"otp|password|passwd|api_?key|token|secret|private_key", re.IGNORECASE)
_META_TOKEN_RE = re.compile(r"^EAA[A-Za-z0-9]{20,}$")
_SUMMARY_FIELDS = ("input_summary", "decision_summary", "output_summary")
MAX_SUMMARY_CHARS = 2_000


class DeedType:
    """The deeds this agent emits. Named constants, not scattered strings."""

    REQUIREMENT_CAPTURED = "MATCH_REQUIREMENT_CAPTURED"
    SHORTLIST_GENERATED = "MATCH_SHORTLIST_GENERATED"
    CANDIDATE_SELECTED = "MATCH_CANDIDATE_SELECTED"
    DEMO_REQUESTED = "DEMO_REQUESTED"
    FAILED_NO_CANDIDATES = "MATCH_FAILED_NO_CANDIDATES"
    HUMAN_HANDOFF_REQUESTED = "HUMAN_HANDOFF_REQUESTED"

    ALL = (
        REQUIREMENT_CAPTURED,
        SHORTLIST_GENERATED,
        CANDIDATE_SELECTED,
        DEMO_REQUESTED,
        FAILED_NO_CANDIDATES,
        HUMAN_HANDOFF_REQUESTED,
    )


class UnsafeEventError(ValueError):
    """The event carries secret-shaped keys or values. Refuse to send it."""


class ChitraguptaError(Exception):
    pass


class ChitraguptaRejected(ChitraguptaError):
    """4xx other than 429. A producer bug — never retried, never spooled."""


class ChitraguptaUnavailable(ChitraguptaError):
    """Transient. Retried, then spooled to the WAL."""


def assert_safe(payload: dict[str, Any], path: str = "") -> None:
    for key, value in payload.items():
        where = f"{path}.{key}" if path else str(key)
        if _SECRET_KEY_RE.search(str(key)):
            raise UnsafeEventError(f"secret-shaped key not allowed: {where}")
        if isinstance(value, dict):
            assert_safe(value, where)
        elif isinstance(value, str) and _META_TOKEN_RE.match(value):
            raise UnsafeEventError(f"token-shaped value not allowed: {where}")


def make_event(**fields: Any) -> dict[str, Any]:
    """Build a contract-valid MemoryEvent, validating before it can be sent."""
    event: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "idempotency_key": str(uuid.uuid4()),
        "schema_version": "1.0",
        "tenant_id": "nxtutors",
        "environment": "production",
        "created_at": datetime.now(UTC).isoformat(),
        "entity_scope": [],
    }
    event.update(fields)

    missing = [key for key in REQUIRED_KEYS if not event.get(key)]
    if missing:
        raise ValueError(f"missing required event fields: {missing}")
    if not DEED_TYPE_RE.match(event["deed_type"]):
        raise ValueError(f"invalid deed_type: {event['deed_type']!r}")
    if event["lifecycle_status"] not in LIFECYCLE_STATUSES:
        raise ValueError(f"invalid lifecycle_status: {event['lifecycle_status']!r}")
    for field in _SUMMARY_FIELDS:
        value = event.get(field)
        if value and len(value) > MAX_SUMMARY_CHARS:
            raise ValueError(f"{field} exceeds {MAX_SUMMARY_CHARS} chars")
    assert_safe(event)
    return event


class FileWal:
    """Append-only spool for events the gateway could not accept.

    Line-delimited JSON so a partially-written tail costs one event, not the
    whole file. Bounded: an unbounded WAL in a Lambda /tmp fills the 512 MB disk
    and takes the function down with it.
    """

    def __init__(self, path: Path, *, max_bytes: int = 8 * 1024 * 1024) -> None:
        self._path = path
        self._max_bytes = max_bytes

    def append(self, event: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists() and self._path.stat().st_size > self._max_bytes:
            raise OSError(f"WAL full: {self._path}")
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, default=str) + "\n")

    def drain(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn final line
        self._path.unlink(missing_ok=True)
        return events


class ChitraguptaMemory:
    """The `MemoryPort` implementation. Degrades, never raises into matching."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        agent_id: str = "tutor-match-meta",
        source_service: str = "tutor-match-meta",
        environment: str = "production",
        timeout_seconds: float = 3.0,
        wal: FileWal | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent_id = agent_id
        self._source_service = source_service
        self._environment = environment
        self._wal = wal
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
            headers={"X-Agent-Id": agent_id, "X-Api-Key": api_key},
        )
        self.failures = 0

    async def recall(
        self, *, entity_type: str, entity_id: str, purpose: str, trace_id: str
    ) -> MemoryPacket:
        """Purpose-scoped read. An outage returns a degraded packet, not an error."""
        request = {
            "agent_id": self._agent_id,
            "purpose": purpose,
            "trace_id": trace_id,
            "entity": {"entity_type": entity_type, "entity_id": entity_id},
            "include_session_summary": True,
            "include_recent_events": True,
            "include_conflicts": True,
            "max_recent_events": 10,
        }
        try:
            response = await self._client.post("/v1/memory/query", json=request)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.failures += 1
            logger.warning("memory recall failed", extra={"tmm_error_code": type(exc).__name__})
            return MemoryPacket(available=False, warnings=("memory_unavailable",))

        facts = tuple(
            MemoryFact(
                key=str(item.get("field") or item.get("key") or ""),
                value=str(item.get("value", "")),
                confidence=float(item.get("confidence", 0.0) or 0.0),
                source=str(item.get("source", "chitragupta")),
                observed_at=_parse_dt(item.get("observed_at") or item.get("verified_at")),
            )
            for item in body.get("confirmed_facts", [])
        )
        return MemoryPacket(
            facts=facts,
            session_summary=body.get("session_summary"),
            # An explicit denial is information: it means "not permitted", which
            # is different from "nothing known" and must not be silently merged.
            denied_fields=tuple(body.get("denied_fields", [])),
            warnings=tuple(body.get("warnings", [])),
            available=True,
        )

    async def record(
        self,
        *,
        deed_type: str,
        purpose: str,
        trace_id: str,
        entity_scope: list[dict[str, str]],
        summary: str | None = None,
        lifecycle_status: str = "completed",
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Emit a deed. Returns False when it could not be durably acknowledged."""
        try:
            event = make_event(
                trace_id=trace_id,
                source_service=self._source_service,
                environment=self._environment,
                agent_id=self._agent_id,
                deed_type=deed_type,
                lifecycle_status=lifecycle_status,
                purpose=purpose,
                entity_scope=entity_scope,
                channel="whatsapp",
                output_summary=(summary or "")[:MAX_SUMMARY_CHARS] or None,
                **(extra or {}),
            )
        except (ValueError, UnsafeEventError) as exc:
            # A malformed event is our bug. Log loudly, do not spool — replaying
            # it can never succeed.
            logger.error(
                "refusing to emit invalid memory event",
                extra={"tmm_error_code": type(exc).__name__, "tmm_deed_type": deed_type},
            )
            return False

        try:
            response = await self._client.post("/v1/memory/events", json=event)
            if response.status_code < 300:
                return True
            if 400 <= response.status_code < 500 and response.status_code != 429:
                logger.error(
                    "memory gateway rejected event", extra={"tmm_status": response.status_code}
                )
                return False
            raise ChitraguptaUnavailable(f"gateway_{response.status_code}")
        except (httpx.HTTPError, ChitraguptaUnavailable) as exc:
            self.failures += 1
            return self._spool(event, exc)

    def _spool(self, event: dict[str, Any], exc: Exception) -> bool:
        if self._wal is None:
            logger.warning(
                "memory event dropped, no WAL", extra={"tmm_error_code": type(exc).__name__}
            )
            return False
        try:
            self._wal.append(event)
        except OSError as wal_exc:
            logger.error(
                "memory WAL write failed", extra={"tmm_error_code": type(wal_exc).__name__}
            )
            return False
        logger.warning(
            "memory event spooled to WAL", extra={"tmm_deed_type": event.get("deed_type")}
        )
        # Spooled, not acknowledged. The caller records a degraded source.
        return False

    async def close(self) -> None:
        await self._client.aclose()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def entity_scope(
    *, conversation_hash: str, lead_id: str | None = None, tutor_ids: list[str] | None = None
) -> list[dict[str, str]]:
    """Entity references for a deed. Hashed conversation, never a raw phone."""
    scope = [{"entity_type": "conversation", "entity_id": conversation_hash}]
    if lead_id:
        scope.append({"entity_type": "lead", "entity_id": lead_id})
    for tutor_id in (tutor_ids or [])[:5]:
        scope.append({"entity_type": "tutor", "entity_id": tutor_id})
    return scope
