"""Per-provider circuit breakers and their declared degradation.

One breaker per provider, never one shared. A struggling Cashfree must not trip
the circuit that guards Google — that would turn one provider's bad afternoon
into a total outage.

The `Degradation` table is the interesting part. Each provider declares what
the system does when it is unavailable, and every one of those answers is
"degrade honestly", never "pretend it worked":

* **OpenAI** — deterministic parsing continues; the assistant asks a clarifying
  question instead of guessing. Scheduling is unaffected.
* **Google** — we never claim a confirmed meeting. The booking stays retryable.
* **Meta** — outbound queues and respects message expiry. A T-15m reminder that
  can no longer be delivered on time is dropped, not sent late.
* **Cashfree** — no payment state is invented. The customer is told to try
  again shortly.
* **Website gateway** — no tutor contact, price or activation is invented.
* **Tutor Intelligence** — a human is offered, never a fabricated tutor.
* **Onboarding** — the handoff stays in the outbox and is retried; the customer
  has already paid and is already welcomed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from demo_command_center.resilience.circuit import CircuitBreaker, State


class Provider(StrEnum):
    """Every external dependency with its own failure domain."""

    TUTOR_INTELLIGENCE = "tutor_intelligence"
    NXTUTORS_GATEWAY = "nxtutors_gateway"
    META_WHATSAPP = "meta_whatsapp"
    GOOGLE_CALENDAR = "google_calendar"
    CASHFREE = "cashfree"
    OPENAI = "openai"
    ONBOARDING = "onboarding"
    AURORA_DATA_API = "aurora_data_api"


class Fallback(StrEnum):
    """What the system does instead. A closed set — dispatched on, not read."""

    DETERMINISTIC_ONLY = "deterministic_only"
    ASK_CLARIFYING_QUESTION = "ask_clarifying_question"
    QUEUE_AND_EXPIRE = "queue_and_expire"
    KEEP_RETRYABLE = "keep_retryable"
    OFFER_HUMAN = "offer_human"
    REFUSE_AND_RETRY_LATER = "refuse_and_retry_later"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class Degradation:
    """What happens when a provider is down, and what must NOT happen."""

    provider: Provider
    fallback: Fallback
    #: True when the lifecycle can continue without this provider at all.
    lifecycle_continues: bool
    #: The claim that must never be made while this provider is unavailable.
    forbidden_claim: str
    customer_message: str = ""


DEGRADATIONS: dict[Provider, Degradation] = {
    Provider.OPENAI: Degradation(
        provider=Provider.OPENAI,
        fallback=Fallback.DETERMINISTIC_ONLY,
        lifecycle_continues=True,
        forbidden_claim="an objection or requirement the model never actually extracted",
        customer_message="Could you tell me the day and time that suits you?",
    ),
    Provider.GOOGLE_CALENDAR: Degradation(
        provider=Provider.GOOGLE_CALENDAR,
        fallback=Fallback.KEEP_RETRYABLE,
        lifecycle_continues=False,
        forbidden_claim="that the demo is confirmed, or any join link",
        customer_message="I am just finalising the class link — I will confirm shortly.",
    ),
    Provider.META_WHATSAPP: Degradation(
        provider=Provider.META_WHATSAPP,
        fallback=Fallback.QUEUE_AND_EXPIRE,
        lifecycle_continues=True,
        forbidden_claim="that a message was delivered",
    ),
    Provider.CASHFREE: Degradation(
        provider=Provider.CASHFREE,
        fallback=Fallback.REFUSE_AND_RETRY_LATER,
        lifecycle_continues=False,
        forbidden_claim="that a payment succeeded, or any payment state at all",
        customer_message="Payments are briefly unavailable. I will send your link shortly.",
    ),
    Provider.NXTUTORS_GATEWAY: Degradation(
        provider=Provider.NXTUTORS_GATEWAY,
        fallback=Fallback.REFUSE_AND_RETRY_LATER,
        lifecycle_continues=False,
        forbidden_claim="a tutor contact, a plan price, or an activated subscription",
    ),
    Provider.TUTOR_INTELLIGENCE: Degradation(
        provider=Provider.TUTOR_INTELLIGENCE,
        fallback=Fallback.OFFER_HUMAN,
        lifecycle_continues=False,
        forbidden_claim="any tutor name, fee, availability or profile link",
        customer_message="Let me get a colleague to find the right tutor for you.",
    ),
    Provider.ONBOARDING: Degradation(
        provider=Provider.ONBOARDING,
        fallback=Fallback.KEEP_RETRYABLE,
        lifecycle_continues=True,
        forbidden_claim="that onboarding has accepted the customer",
    ),
    Provider.AURORA_DATA_API: Degradation(
        provider=Provider.AURORA_DATA_API,
        fallback=Fallback.FAIL_CLOSED,
        lifecycle_continues=False,
        forbidden_claim="any state change at all — an unpersisted decision did not happen",
    ),
}


class CircuitRegistry:
    """One breaker per provider, built once per container.

    Deliberately in-process. A distributed breaker needs shared state on the
    request path, which costs a database round trip per call to protect against
    a failure mode a local breaker already handles at this scale.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 60.0,
        half_open_probes: int = 1,
    ) -> None:
        self._breakers = {
            provider: CircuitBreaker(
                name=provider.value,
                failure_threshold=failure_threshold,
                reset_seconds=reset_seconds,
                half_open_probes=half_open_probes,
            )
            for provider in Provider
        }
        #: Operator kill switches. An open switch behaves exactly like an open
        #: circuit, so every caller already handles it.
        self._disabled: set[Provider] = set()

    def breaker(self, provider: Provider) -> CircuitBreaker:
        return self._breakers[provider]

    def disable(self, provider: Provider) -> None:
        """Kill switch. Used when a provider is known-bad and we want to stop
        paying for the discovery on every request."""
        self._disabled.add(provider)

    def enable(self, provider: Provider) -> None:
        self._disabled.discard(provider)

    def available(self, provider: Provider) -> bool:
        return provider not in self._disabled and self._breakers[provider].state is not State.OPEN

    def degradation(self, provider: Provider) -> Degradation:
        return DEGRADATIONS[provider]

    def snapshot(self) -> dict[str, str]:
        """Circuit state per provider. Emitted as a metric and by the doctor."""
        return {
            provider.value: ("disabled" if provider in self._disabled else breaker.state.value)
            for provider, breaker in self._breakers.items()
        }

    def open_providers(self) -> tuple[Provider, ...]:
        return tuple(p for p in Provider if not self.available(p))

    def lifecycle_blocked_by(self) -> tuple[Provider, ...]:
        """Open providers the lifecycle genuinely cannot proceed without.

        The distinction matters for alerting: OpenAI being down is a degraded
        experience, Cashfree being down stops revenue, and paging differently
        for the two is the difference between an alert people read and one they
        mute.
        """
        return tuple(p for p in self.open_providers() if not DEGRADATIONS[p].lifecycle_continues)
