"""Structured JSON logging with an enforced PII filter.

Two things make this more than a formatter:

* **The filter is not optional.** `PiiScrubbingFilter` is attached to the
  handler, not applied at call sites, because the call site that leaks is always
  the one nobody remembered to wrap — typically an exception message carrying a
  phone number from a validation error.
* **Trace fields are ambient.** `bind()` puts correlation/conversation/demo ids
  into a `ContextVar`, so every log line in an invocation carries them without
  each call passing them along. Async-safe: `ContextVar` is per-task, so two
  concurrent conversations in one container cannot cross-contaminate.

Log lines are one JSON object per line, which is what CloudWatch Logs Insights
parses natively.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from typing import Any

from demo_command_center.security.pii import redact

#: Extras are prefixed so they cannot collide with LogRecord's own attributes.
EXTRA_PREFIX = "dcc_"

_STANDARD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Ambient identifiers for one invocation. All non-identifying."""

    trace_id: str = ""
    correlation_id: str = ""
    #: Pseudonymised. The raw conversation id never appears in a log line.
    conversation_ref: str = ""
    demo_id: str = ""
    capability: str = ""
    handler: str = ""

    def fields(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v}


# B039 flags a shared default. `TraceContext` is a frozen slotted dataclass, so
# the shared instance cannot be mutated — `bind()` always replaces it.
_context: ContextVar[TraceContext] = ContextVar(
    "dcc_trace_context",
    default=TraceContext(),  # noqa: B039
)


def bind(**fields: str) -> TraceContext:
    """Merge fields into the ambient context for the rest of this task."""
    current = _context.get()
    updated = replace(current, **{k: v for k, v in fields.items() if v})
    _context.set(updated)
    return updated


def current_context() -> TraceContext:
    return _context.get()


def reset_context() -> None:
    _context.set(TraceContext())


class PiiScrubbingFilter(logging.Filter):
    """Redacts identifiers from the message and every extra before emission."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(
                redact(arg) if isinstance(arg, str) else arg
                for arg in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        for key, value in list(record.__dict__.items()):
            if key.startswith(EXTRA_PREFIX) and isinstance(value, str):
                record.__dict__[key] = redact(value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        payload.update(current_context().fields())
        for key, value in record.__dict__.items():
            if key.startswith(EXTRA_PREFIX):
                payload[key[len(EXTRA_PREFIX) :]] = value
            elif key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            # The type and a redacted message only. A full traceback in a log
            # line routinely contains the request body that caused it.
            exc_type, exc_value, _ = record.exc_info
            payload["error_type"] = exc_type.__name__ if exc_type else "unknown"
            payload["error"] = redact(str(exc_value))[:500]
        return json.dumps(payload, default=str, separators=(",", ":"))


_configured = False


def configure(level: str = "INFO") -> None:
    """Idempotent root configuration. Safe to call on every cold start."""
    global _configured
    root = logging.getLogger()
    if _configured:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(PiiScrubbingFilter())
    # Lambda pre-installs its own handler; replacing it is what stops every line
    # being emitted twice, once JSON and once plain.
    root.handlers = [handler]
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"dcc.{name}")
