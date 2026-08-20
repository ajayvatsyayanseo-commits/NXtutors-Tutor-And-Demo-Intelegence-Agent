"""A per-container circuit breaker.

Deliberately in-process, not shared. A distributed breaker needs shared state on
the request path, which costs a database round trip per call to protect against
a failure mode — every container hammering a dead provider — that a local
breaker already handles well enough at this scale.

Half-open admits exactly `half_open_probes` calls. Admitting all of them is the
classic bug: the moment the reset timer fires, every queued request stampedes
the recovering provider and knocks it straight back over.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(Exception):
    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(f"circuit {name} is open; retry in {retry_after:.1f}s")
        self.name = name
        self.retry_after = retry_after


@dataclass(slots=True)
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    reset_seconds: float = 60.0
    half_open_probes: int = 1
    #: Injected in tests. Production reads the monotonic clock, which cannot go
    #: backwards — a wall-clock adjustment would otherwise reopen every breaker.
    monotonic: object = field(default=None)

    _state: State = State.CLOSED
    _failures: int = 0
    _opened_at: float = 0.0
    _probes_in_flight: int = 0

    def _now(self) -> float:
        return time.monotonic() if self.monotonic is None else float(self.monotonic())  # type: ignore[operator]

    @property
    def state(self) -> State:
        return self._state

    def before_call(self) -> None:
        """Raise `CircuitOpen` when the call must not be attempted."""
        if self._state is State.CLOSED:
            return

        elapsed = self._now() - self._opened_at
        if self._state is State.OPEN:
            if elapsed < self.reset_seconds:
                raise CircuitOpen(self.name, self.reset_seconds - elapsed)
            self._state = State.HALF_OPEN
            self._probes_in_flight = 0

        if self._probes_in_flight >= self.half_open_probes:
            raise CircuitOpen(self.name, self.reset_seconds)
        self._probes_in_flight += 1

    def record_success(self) -> None:
        self._state = State.CLOSED
        self._failures = 0
        self._probes_in_flight = 0

    def record_failure(self) -> None:
        self._failures += 1
        if self._state is State.HALF_OPEN or self._failures >= self.failure_threshold:
            # A half-open probe that fails reopens immediately: one failure
            # during recovery is enough evidence that it has not recovered.
            self._state = State.OPEN
            self._opened_at = self._now()
            self._probes_in_flight = 0
