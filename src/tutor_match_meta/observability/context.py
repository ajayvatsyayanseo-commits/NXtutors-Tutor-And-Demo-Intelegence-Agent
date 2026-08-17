"""Request context and structured logging.

Every log line in a request carries the same correlation fields, set once at the
edge and propagated through a `ContextVar` so no function has to thread a logger
argument through five layers.

The hard rule: **no raw PII in logs or metric labels.** The context holds
`conversation_id_hash`, never `conversation_id`; there is no field for a phone
number or a message body. `security.pii.assert_label_safe` enforces the metric
side at runtime.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any

_CONTEXT: ContextVar[RequestContext | None] = ContextVar("tmm_request_context", default=None)


@dataclass(slots=True)
class RequestContext:
    """The correlation envelope for one inbound event."""

    trace_id: str
    #: Peppered hash. The raw conversation id never enters a log line.
    conversation_id_hash: str
    message_id: str | None = None
    source_agent: str | None = None
    match_session_id: str | None = None
    state_before: str | None = None
    state_after: str | None = None
    policy_version: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    latency_ms: int | None = None
    retry_count: int = 0
    error_code: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_log_fields(self) -> dict[str, Any]:
        fields = {k: v for k, v in asdict(self).items() if v is not None and k != "extra"}
        fields.update(self.extra)
        return fields


def new_trace_id() -> str:
    return uuid.uuid4().hex


def current() -> RequestContext | None:
    return _CONTEXT.get()


@contextmanager
def request_context(context: RequestContext) -> Iterator[RequestContext]:
    """Bind a context for the duration of one event."""
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the request context merged in.

    CloudWatch parses this natively, so a trace can be followed across the
    ingress, worker and outbound Lambdas by `trace_id` alone.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = current()
        if context is not None:
            payload.update(context.as_log_fields())
        for key, value in getattr(record, "__dict__", {}).items():
            if key.startswith("tmm_"):
                payload[key[4:]] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    """Idempotent: safe to call on every warm Lambda invocation."""
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    # These log full request bodies at INFO, which would defeat PII redaction.
    for noisy in ("httpx", "httpcore", "botocore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"tutor_match_meta.{name}")


class Timer:
    """Wall-clock timing for a block, in milliseconds."""

    __slots__ = ("_start", "elapsed_ms")

    def __init__(self) -> None:
        self.elapsed_ms = 0
        self._start = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = int((time.perf_counter() - self._start) * 1000)
