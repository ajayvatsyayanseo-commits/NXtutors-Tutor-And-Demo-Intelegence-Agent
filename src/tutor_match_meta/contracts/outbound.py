"""The outbox → outbound-worker delivery contract.

One typed shape, produced by `TurnService`, persisted in `outbox_event.payload`,
relayed onto SQS by the outbox job, and consumed by `outbound_worker_handler`.

It exists because those four places previously agreed on nothing. The turn
service wrote `{"text": ..., "conversation_hash": ...}`; the worker read
`payload["to"]` and `payload["body"]`. Every relayed reply hit a `KeyError`,
was logged as "unparseable outbound record", and was dropped without ever
reaching the DLQ — a parent's shortlist silently vanished. A shared model plus
`tests/contract/test_outbound_delivery.py` makes that drift a test failure
instead of a production silence.

`recipient` is the one place in this service that holds a raw E.164 phone
number at rest. It is unavoidable — the Meta Cloud API addresses by phone — and
it is bounded: the field is only populated under
`outbound_ownership=tutor_match_sends`, it is never logged, and
`sync/retention.py` purges delivered rows aggressively so an undeliverable
message is the only thing that keeps one alive. See
docs/data-classification.md.
"""

from __future__ import annotations

import re
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tutor_match_meta.contracts.common import SCHEMA_VERSION

#: Meta's hard limit for a text body.
MAX_BODY_CHARS = 4_096

#: We own delivery. A recipient is mandatory and the relay pushes it to SQS.
OUTBOX_KIND_REPLY = "whatsapp_reply"
#: The caller already delivered this text (`outbound_ownership=caller_sends`).
#: The row exists purely as the audit record of what a parent was told — which
#: is worth keeping for dispute resolution and for reconstructing a
#: conversation during an incident — and the relay closes it without sending.
#: Modelling it as a distinct kind rather than "a reply with a null recipient"
#: is what stops the outbound worker from ever seeing an unaddressable row.
OUTBOX_KIND_CALLER_REPLY = "caller_delivered_reply"

#: A memory deed for the Chitragupta service.
#:
#: Deeds go through the outbox for the same reason replies do, plus one more.
#: The reason they share: the match worker runs inside the VPC with no NAT
#: Gateway, so it has no route to the memory service — it can only write a row
#: and let an internet-side worker deliver it. The reason that is an
#: improvement rather than a workaround: a deed used to be an inline
#: fire-and-forget HTTP call, so a memory blip lost the deed permanently and
#: left nothing but a `degraded` marker. Now it retries, and an undeliverable
#: deed lands in the DLQ where someone can see it.
OUTBOX_KIND_DEED = "memory_deed"

#: Kinds the relay pushes to the outbound queue. Audit-only replies are closed
#: in place and never pushed.
DELIVERABLE_KINDS: frozenset[str] = frozenset({OUTBOX_KIND_REPLY, OUTBOX_KIND_DEED})
#: Of those, the kinds that address a person and therefore need a recipient.
ADDRESSED_KINDS: frozenset[str] = frozenset({OUTBOX_KIND_REPLY})

_E164 = re.compile(r"^\+?[1-9]\d{7,14}$")


class OutboundDeliveryV1(BaseModel):
    """One reply. Either ours to send, or the record that the caller sent it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: str = SCHEMA_VERSION
    kind: str = Field(default=OUTBOX_KIND_REPLY, max_length=48)
    #: Raw E.164 recipient. PII. Never logged, never exported to analytics.
    #: Absent for `caller_delivered_reply`, where we never held it.
    recipient: str | None = Field(default=None, max_length=32)
    body: str = Field(max_length=MAX_BODY_CHARS)
    #: Provider-side idempotency key. The same key must never send twice.
    dedup_key: str = Field(max_length=128)
    trace_id: str = Field(max_length=64)
    #: Pseudonymous handle, safe for logs and metrics.
    conversation_ref: str = Field(default="", max_length=64)
    preview_url: bool = True

    @property
    def deliverable(self) -> bool:
        return self.kind in DELIVERABLE_KINDS

    @model_validator(mode="after")
    def _deliverable_rows_are_addressable(self) -> Self:
        if not self.body.strip():
            raise ValueError("refusing to store an empty message body")
        if self.kind not in ADDRESSED_KINDS:
            return self
        if not self.recipient or not _E164.match(self.recipient.strip()):
            raise ValueError(f"kind={self.kind} requires an E.164 recipient")
        return self

    def to_payload(self) -> dict[str, Any]:
        """What goes into `outbox_event.payload` and onto SQS."""
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OutboundDeliveryV1:
        return cls.model_validate(payload)


class MemoryDeedV1(BaseModel):
    """One deed, queued for the internet-side worker to record.

    Carries no free text from the parent and no raw identifiers: `entity_scope`
    holds the pseudonymised conversation reference and public tutor refs, and
    `summary` is written by this service, never by a model or a parent.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: str = SCHEMA_VERSION
    kind: str = Field(default=OUTBOX_KIND_DEED, max_length=48)
    deed_type: str = Field(max_length=64)
    purpose: str = Field(default="tutor_matching", max_length=64)
    summary: str = Field(max_length=500)
    #: Entity references, as `chitragupta.entity_scope` builds them:
    #: a list of `{entity_type, entity_id}` pairs, hashed, never a raw phone.
    entity_scope: list[dict[str, str]] = Field(default_factory=list, max_length=16)
    dedup_key: str = Field(max_length=128)
    trace_id: str = Field(max_length=64)
    conversation_ref: str = Field(default="", max_length=64)

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MemoryDeedV1:
        return cls.model_validate(payload)


def payload_kind(payload: dict[str, Any]) -> str:
    """Read the kind before validating, so each kind picks its own model."""
    return str(payload.get("kind") or OUTBOX_KIND_REPLY)


__all__ = [
    "ADDRESSED_KINDS",
    "DELIVERABLE_KINDS",
    "MAX_BODY_CHARS",
    "OUTBOX_KIND_CALLER_REPLY",
    "OUTBOX_KIND_DEED",
    "OUTBOX_KIND_REPLY",
    "MemoryDeedV1",
    "OutboundDeliveryV1",
    "payload_kind",
]
