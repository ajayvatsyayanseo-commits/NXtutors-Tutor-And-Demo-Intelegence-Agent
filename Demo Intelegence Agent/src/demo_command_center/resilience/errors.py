"""Error classification and the retry policy that reads it.

One taxonomy, used everywhere. The point is not tidiness — it is that *whether
to retry* is a property of the error class, decided once here, rather than a
judgement each call site makes differently.

Two rules the classification exists to enforce:

* **A malformed request, a bad credential, an authorization failure and an
  amount mismatch are never transient.** Retrying any of them burns quota,
  delays the real failure, and in the amount-mismatch case risks re-processing
  money. They are `terminal`.
* **Every asynchronous message ends somewhere.** `Disposition` is a closed set:
  success, retry, DLQ, HITL or terminal failure. There is no "swallowed".

Backoff is exponential with **full jitter**. Equal backoff across a fleet
re-synchronises every retry into a thundering herd against a provider that is
already struggling — jitter is what spreads them.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from demo_command_center.contracts.ports import (
    ProviderError,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)


class ErrorClass(StrEnum):
    """What kind of failure this is. Drives retry, alarm and disposition."""

    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    CONFLICT = "conflict"
    DUPLICATE = "duplicate"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    TRANSIENT_PROVIDER = "transient_provider"
    PERMANENT_PROVIDER = "permanent_provider"
    DATABASE_TRANSIENT = "database_transient"
    BUSINESS_POLICY = "business_policy"
    CIRCUIT_OPEN = "circuit_open"
    UNKNOWN = "unknown"


#: Classes worth retrying. Everything else fails now rather than later.
RETRYABLE: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.RATE_LIMIT,
        ErrorClass.TIMEOUT,
        ErrorClass.TRANSIENT_PROVIDER,
        ErrorClass.DATABASE_TRANSIENT,
        ErrorClass.CIRCUIT_OPEN,
        # UNKNOWN is retried a *bounded* number of times: an unclassified error
        # is usually a new transient shape, and treating it as terminal loses
        # real work. The bound is what stops it looping forever.
        ErrorClass.UNKNOWN,
    }
)

#: Classes that need a person, not another attempt.
NEEDS_HUMAN: frozenset[ErrorClass] = frozenset(
    {ErrorClass.CONFLICT, ErrorClass.BUSINESS_POLICY, ErrorClass.AUTHORIZATION}
)

#: Classes that are already-handled non-events. Never alarmed, never retried.
BENIGN: frozenset[ErrorClass] = frozenset({ErrorClass.DUPLICATE})


class Disposition(StrEnum):
    """Where an asynchronous message ended up. A closed set, by design."""

    SUCCESS = "success"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"
    HUMAN_REVIEW = "human_review"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff with full jitter."""

    max_attempts: int = 3
    base_seconds: float = 0.25
    max_seconds: float = 20.0
    #: Injected in tests so a backoff assertion is deterministic. Production
    #: leaves it None and uses `random.random`.
    jitter: Callable[[], float] | None = None

    def should_retry(self, error_class: ErrorClass, attempt: int) -> bool:
        return error_class in RETRYABLE and attempt < self.max_attempts

    def delay_for(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Seconds to wait before attempt `attempt + 1`.

        A provider's own `Retry-After` wins when it is present and sane — it
        knows when it will be ready and we do not. It is still capped, because
        a provider asking us to wait an hour inside a Lambda with a 30-second
        timeout is not a wait we can honour.
        """
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.max_seconds)
        # `2.0` rather than `2`: mypy types `int ** int` as `Any` because the
        # result could be either, which would silently make this whole
        # expression untyped.
        ceiling = min(self.base_seconds * (2.0**attempt), self.max_seconds)
        roll = (
            random.random()  # noqa: S311 - backoff jitter, not a security decision
            if self.jitter is None
            else self.jitter()
        )
        # Full jitter: uniform in [0, ceiling]. Equal backoff across a fleet
        # re-synchronises every retry into one spike.
        return ceiling * roll


def classify(error: BaseException) -> ErrorClass:
    """Map any exception onto the taxonomy. Never raises."""
    from demo_command_center.contracts.ownership import OwnershipError
    from demo_command_center.domain.payments import PaymentReconciliationError
    from demo_command_center.domain.slots import SlotConflict
    from demo_command_center.guardrails.tutor_selection import TutorSelectionRejected
    from demo_command_center.resilience.circuit import CircuitOpen
    from demo_command_center.security.rate_limit import RateLimited
    from demo_command_center.security.signatures import SignatureError
    from demo_command_center.state.machine import ConcurrencyConflict, TransitionRejected

    if isinstance(error, SignatureError):
        return ErrorClass.AUTHENTICATION
    if isinstance(error, OwnershipError | TutorSelectionRejected):
        return ErrorClass.AUTHORIZATION
    if isinstance(error, RateLimited):
        return ErrorClass.RATE_LIMIT
    if isinstance(error, CircuitOpen):
        return ErrorClass.CIRCUIT_OPEN
    if isinstance(error, SlotConflict | ConcurrencyConflict):
        return ErrorClass.CONFLICT
    if isinstance(error, PaymentReconciliationError):
        # Explicitly NOT transient. A retried amount mismatch is a second
        # chance to mis-process money.
        return ErrorClass.VALIDATION
    if isinstance(error, TransitionRejected):
        return ErrorClass.BUSINESS_POLICY
    if isinstance(error, ProviderTimeout):
        return ErrorClass.TIMEOUT
    if isinstance(error, ProviderUnavailable):
        return ErrorClass.TRANSIENT_PROVIDER
    if isinstance(error, ProviderRejected):
        return _classify_status(error)
    if isinstance(error, ProviderError):
        return ErrorClass.TRANSIENT_PROVIDER if error.retryable else ErrorClass.PERMANENT_PROVIDER
    if isinstance(error, ValueError | TypeError | KeyError):
        return ErrorClass.VALIDATION
    return ErrorClass.UNKNOWN


def _classify_status(error: ProviderRejected) -> ErrorClass:
    status = error.status_code or 0
    if status == 401:
        return ErrorClass.AUTHENTICATION
    if status == 403:
        return ErrorClass.AUTHORIZATION
    if status == 409:
        return ErrorClass.CONFLICT
    if status == 429:
        return ErrorClass.RATE_LIMIT
    if 400 <= status < 500:
        return ErrorClass.PERMANENT_PROVIDER
    return ErrorClass.TRANSIENT_PROVIDER


def disposition_for(error_class: ErrorClass, *, attempt: int, policy: RetryPolicy) -> Disposition:
    """Where this failure sends the message. Total over the taxonomy."""
    if error_class in BENIGN:
        return Disposition.SUCCESS
    if error_class in NEEDS_HUMAN:
        return Disposition.HUMAN_REVIEW
    if policy.should_retry(error_class, attempt):
        return Disposition.RETRY
    if error_class in RETRYABLE:
        # Retryable but exhausted. The DLQ is where a person finds it.
        return Disposition.DEAD_LETTER
    return Disposition.TERMINAL_FAILURE


@dataclass(frozen=True, slots=True)
class Outcome:
    """The classified result of one attempt. What gets logged and metered."""

    error_class: ErrorClass
    disposition: Disposition
    attempt: int
    provider: str = ""
    detail: str = ""

    @property
    def alarm_worthy(self) -> bool:
        """Whether this should page anyone.

        A duplicate is not an incident. A retry that will succeed is not an
        incident. A DLQ, a terminal failure or anything needing a human is.
        """
        return self.disposition in (
            Disposition.DEAD_LETTER,
            Disposition.HUMAN_REVIEW,
            Disposition.TERMINAL_FAILURE,
        )


def evaluate(
    error: BaseException, *, attempt: int, policy: RetryPolicy, provider: str = ""
) -> Outcome:
    """Classify one failure and decide what happens to it."""
    error_class = classify(error)
    return Outcome(
        error_class=error_class,
        disposition=disposition_for(error_class, attempt=attempt, policy=policy),
        attempt=attempt,
        provider=provider or getattr(error, "provider", ""),
        # The type name only. An exception message routinely contains the
        # request body that caused it, which for us includes customer data.
        detail=type(error).__name__,
    )
