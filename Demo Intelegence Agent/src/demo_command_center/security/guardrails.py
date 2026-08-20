"""Input guardrails — everything that runs *before* a model sees a message.

Cheap, deterministic checks first. A message that fails here never reaches the
LLM, so hostile traffic costs a few microseconds of regex rather than a token
bill. Prompt-injection detection here is **not** a security control on its own —
the actual control is that the LLM cannot authorize anything (`orchestration/`
validates every proposed tool call against the state machine and policy before
execution). Detection exists to flag, throttle and route to a human, and to stop
obviously hostile text being echoed back into a prompt.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: WhatsApp's own cap is 4096; anything larger did not come from WhatsApp.
MAX_MESSAGE_CHARS: Final = 4_096
MAX_JSON_DEPTH: Final = 12


class RejectionReason(StrEnum):
    TOO_LONG = "too_long"
    EMPTY = "empty"
    UNSUPPORTED_TYPE = "unsupported_type"
    MALFORMED_UNICODE = "malformed_unicode"
    JSON_TOO_DEEP = "json_too_deep"


class InputRejected(ValueError):
    def __init__(self, reason: RejectionReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


#: Message types we can act on. Anything else gets a polite "send me text"
#: rather than being fed to a model that will hallucinate a transcription.
SUPPORTED_MESSAGE_TYPES: frozenset[str] = frozenset({"text", "interactive", "button", "reaction"})

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+(?:the\s+)?(?:system|previous)\s+(?:prompt|instructions)", re.I),
    re.compile(r"you\s+are\s+now\s+(?:a|an|in)\s+", re.I),
    re.compile(r"\b(?:system|developer)\s*(?:prompt|message)\s*[:=]", re.I),
    re.compile(r"reveal|print|show\s+(?:me\s+)?(?:your\s+)?(?:system\s+prompt|instructions)", re.I),
    re.compile(r"\b(?:database|db|api)\s+(?:credentials|password|key|secret)\b", re.I),
    re.compile(r"\bDROP\s+TABLE\b|\bUNION\s+SELECT\b|--\s*$", re.I),
    re.compile(r"<\s*script\b|javascript\s*:", re.I),
    re.compile(r"\{\{.*?\}\}|\$\{.*?\}"),  # template injection into our own prompts
)

#: Zero-width and bidirectional-override characters. Used to hide an injection
#: from a human reviewer while the model still reads it.
#:
#: Written as escapes, not as literal characters. A source file containing real
#: bidi controls is the same trick aimed at *us* — a reviewer reading this file
#: could not see what the class contained, and bandit flags it (B613) for
#: exactly that reason. The escapes are also simply readable.
_INVISIBLE: Final = re.compile(
    "["
    "\u200b-\u200f"  # zero-width space .. right-to-left mark
    "\u202a-\u202e"  # bidirectional embeddings and overrides
    "\u2066-\u2069"  # bidirectional isolates
    "\ufeff"  # zero-width no-break space (BOM)
    "]"
)


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """The result of screening one inbound message."""

    text: str
    suspicious: bool
    signals: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.suspicious


def normalise(text: str) -> str:
    """NFKC + strip invisibles + collapse whitespace.

    NFKC first so a fullwidth `ｉｇｎｏｒｅ` collapses to `ignore` and the
    injection patterns below actually match it.
    """
    folded = unicodedata.normalize("NFKC", text)
    folded = _INVISIBLE.sub("", folded)
    return re.sub(r"[ \t]{2,}", " ", folded).strip()


def screen_message(raw: str | None, *, message_type: str = "text") -> GuardVerdict:
    """Validate and normalise one inbound message, or raise `InputRejected`."""
    if message_type not in SUPPORTED_MESSAGE_TYPES:
        raise InputRejected(RejectionReason.UNSUPPORTED_TYPE)
    if raw is None or not raw.strip():
        raise InputRejected(RejectionReason.EMPTY)
    if len(raw) > MAX_MESSAGE_CHARS:
        raise InputRejected(RejectionReason.TOO_LONG)
    try:
        text = normalise(raw)
    except (UnicodeError, ValueError) as exc:
        raise InputRejected(RejectionReason.MALFORMED_UNICODE) from exc

    signals = tuple(
        f"pattern_{index}"
        for index, pattern in enumerate(_INJECTION_PATTERNS)
        if pattern.search(text)
    )
    return GuardVerdict(text=text, suspicious=bool(signals), signals=signals)


def json_depth(value: object, *, _depth: int = 0) -> int:
    """Depth of a decoded JSON value.

    A deeply nested webhook body is a cheap way to make a recursive validator
    blow the stack; checking depth before validation costs one linear pass.
    """
    if _depth > MAX_JSON_DEPTH:
        return _depth
    if isinstance(value, dict):
        return max((json_depth(v, _depth=_depth + 1) for v in value.values()), default=_depth)
    if isinstance(value, list):
        return max((json_depth(v, _depth=_depth + 1) for v in value), default=_depth)
    return _depth


def assert_depth_ok(value: object) -> None:
    if json_depth(value) > MAX_JSON_DEPTH:
        raise InputRejected(RejectionReason.JSON_TOO_DEEP)


def wrap_untrusted(text: str) -> str:
    """Fence user text before it enters a prompt.

    Delimiting is not a security boundary and is not claimed to be one; it is
    there so the model has an unambiguous marker for "this is data", which
    measurably reduces instruction-following on the contents. The real boundary
    is that nothing the model returns is executed without deterministic
    authorization.
    """
    fenced = text.replace("</untrusted_user_message>", "")
    return f"<untrusted_user_message>\n{fenced}\n</untrusted_user_message>"
