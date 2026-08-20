"""LLM cost control. Four independent ceilings, plus a list of forbidden uses.

Tokens alone do not bound cost. The expensive failure mode is not one long
prompt — it is a redelivery loop making three thousand cheap calls, which no
token budget notices. So there are four ceilings and a call has to clear all of
them:

1. calls per turn;
2. calls per conversation;
3. reasoning (expensive-model) calls per conversation;
4. tokens per conversation.

`FORBIDDEN_USES` is the other half, and the more important one. The cheapest
model call is the one that never happens, and every entry there is a place
where a deterministic answer already exists. Asking a model to compute a
discount is not just expensive — it is wrong.

Model ids are **never** literals here. They come from settings, are routed by
purpose, and are recorded on every usage row so a cost review can attribute
spend to a capability rather than to "OpenAI".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Purpose(StrEnum):
    """Why a model is being called. Routes to a model tier and a budget."""

    INTENT_CLASSIFICATION = "intent_classification"
    REQUIREMENT_EXTRACTION = "requirement_extraction"
    TIME_INTERPRETATION = "time_interpretation"
    OBJECTION_EXTRACTION = "objection_extraction"
    MESSAGE_WORDING = "message_wording"
    SUMMARISATION = "summarisation"


class Tier(StrEnum):
    """Cost tier. `REASONING` is the one worth being stingy with."""

    CLASSIFIER = "classifier"
    EXTRACTION = "extraction"
    REASONING = "reasoning"


#: Purpose → tier. Only objection extraction earns the expensive model: it is
#: the one task where a shallow answer produces a wrong business decision.
TIERS: dict[Purpose, Tier] = {
    Purpose.INTENT_CLASSIFICATION: Tier.CLASSIFIER,
    Purpose.REQUIREMENT_EXTRACTION: Tier.EXTRACTION,
    Purpose.TIME_INTERPRETATION: Tier.EXTRACTION,
    Purpose.MESSAGE_WORDING: Tier.EXTRACTION,
    Purpose.SUMMARISATION: Tier.EXTRACTION,
    Purpose.OBJECTION_EXTRACTION: Tier.REASONING,
}

#: Things a model must never be asked to do, and what does them instead.
#: Enforced by `assert_not_forbidden` and asserted by a security test.
FORBIDDEN_USES: dict[str, str] = {
    "arithmetic": "Money and percentages are Decimal arithmetic in domain/pricing.py",
    "state_transition": "state/transitions.py — a table, not a judgement",
    "availability": "the website gateway is authoritative",
    "payment_validation": "signature verification plus exact amount reconciliation",
    "regional_aggregation": "SQL rollups in capabilities/monitoring",
    "discount_amount": "the deterministic band engine in capabilities/discounts",
    "authorization": "actor sets on the transition table",
    "tutor_eligibility": "Tutor Intelligence hard filters",
    "no_show_determination": "calendar/conference evidence only",
    "duplicate_event": "a redelivery is dropped before any model is reached",
}


class ForbiddenModelUse(Exception):
    def __init__(self, use: str, instead: str) -> None:
        super().__init__(f"an LLM must not be used for {use}: {instead}")
        self.use = use


def assert_not_forbidden(use: str) -> None:
    instead = FORBIDDEN_USES.get(use)
    if instead is not None:
        raise ForbiddenModelUse(use, instead)


class BudgetExceeded(Exception):
    """A ceiling was hit. The caller degrades deterministically; it never waits."""

    def __init__(self, ceiling: str, limit: int, observed: int) -> None:
        super().__init__(f"llm budget exceeded: {ceiling} (limit {limit}, observed {observed})")
        self.ceiling = ceiling
        self.limit = limit
        self.observed = observed


@dataclass(frozen=True, slots=True)
class Budget:
    """The four ceilings. All from settings; none is a literal in business code."""

    calls_per_turn: int = 2
    calls_per_conversation: int = 30
    reasoning_calls_per_conversation: int = 3
    tokens_per_conversation: int = 60_000
    max_input_tokens: int = 4_000
    max_output_tokens: int = 900
    timeout_seconds: float = 20.0
    max_retries: int = 2


@dataclass(slots=True)
class Usage:
    """Running spend for one conversation. Reset per conversation, not per turn."""

    conversation_ref: str
    calls_this_turn: int = 0
    calls_total: int = 0
    reasoning_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    records: list[dict[str, object]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def start_turn(self) -> None:
        self.calls_this_turn = 0


class BudgetGuard:
    """Checks every ceiling before a call and records what it cost after."""

    def __init__(self, budget: Budget) -> None:
        self._budget = budget

    def check(self, usage: Usage, purpose: Purpose) -> None:
        """Raise `BudgetExceeded` if this call would breach a ceiling."""
        b = self._budget
        if usage.calls_this_turn >= b.calls_per_turn:
            raise BudgetExceeded("calls_per_turn", b.calls_per_turn, usage.calls_this_turn)
        if usage.calls_total >= b.calls_per_conversation:
            raise BudgetExceeded(
                "calls_per_conversation", b.calls_per_conversation, usage.calls_total
            )
        if usage.total_tokens >= b.tokens_per_conversation:
            raise BudgetExceeded(
                "tokens_per_conversation", b.tokens_per_conversation, usage.total_tokens
            )
        if TIERS[purpose] is Tier.REASONING:
            if usage.reasoning_calls >= b.reasoning_calls_per_conversation:
                raise BudgetExceeded(
                    "reasoning_calls_per_conversation",
                    b.reasoning_calls_per_conversation,
                    usage.reasoning_calls,
                )

    def record(
        self,
        usage: Usage,
        *,
        purpose: Purpose,
        model_ref: str,
        prompt_version: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        succeeded: bool,
        fell_back: bool = False,
    ) -> dict[str, object]:
        """Account for one call. Returns the row written to `dcc_model_usage`."""
        usage.calls_this_turn += 1
        usage.calls_total += 1
        usage.input_tokens += input_tokens
        usage.output_tokens += output_tokens
        if TIERS[purpose] is Tier.REASONING:
            usage.reasoning_calls += 1

        record: dict[str, object] = {
            "conversation_ref": usage.conversation_ref,
            "purpose": purpose.value,
            "tier": TIERS[purpose].value,
            "model_ref": model_ref,
            "prompt_version": prompt_version,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": round(latency_ms, 2),
            "succeeded": succeeded,
            "fell_back": fell_back,
        }
        usage.records.append(record)
        return record

    def remaining(self, usage: Usage) -> dict[str, int]:
        b = self._budget
        return {
            "calls_this_turn": max(0, b.calls_per_turn - usage.calls_this_turn),
            "calls_total": max(0, b.calls_per_conversation - usage.calls_total),
            "reasoning_calls": max(0, b.reasoning_calls_per_conversation - usage.reasoning_calls),
            "tokens": max(0, b.tokens_per_conversation - usage.total_tokens),
        }


@dataclass(slots=True)
class DailyCostCircuit:
    """An environment-wide spend ceiling.

    The last line of defence against a runaway loop that survives every
    per-conversation budget by spreading itself across many conversations. When
    it trips, every capability degrades to its deterministic path — the service
    keeps working, it just stops thinking.
    """

    #: Micro-units of currency, so the ceiling is an integer and never a float.
    daily_ceiling_micros: int
    spent_micros: int = 0
    window_start: datetime | None = None
    tripped: bool = False

    def spend(self, micros: int, *, now: datetime) -> None:
        if self.window_start is None or (now - self.window_start).days >= 1:
            self.window_start = now
            self.spent_micros = 0
            self.tripped = False
        self.spent_micros += micros
        if self.spent_micros >= self.daily_ceiling_micros:
            self.tripped = True

    @property
    def utilisation(self) -> float:
        if self.daily_ceiling_micros <= 0:
            return 0.0
        return round(self.spent_micros / self.daily_ceiling_micros, 4)

    def assert_open(self) -> None:
        if self.tripped:
            raise BudgetExceeded("daily_cost_circuit", self.daily_ceiling_micros, self.spent_micros)


def estimate_cost_micros(
    *, input_tokens: int, output_tokens: int, input_per_million: int, output_per_million: int
) -> int:
    """Cost in micro-units, from configured per-million rates.

    Rates are **parameters**, never constants: published prices change, and a
    hardcoded one silently makes every cost report wrong from the day it does.
    Integer arithmetic throughout — a float cost that is summed across a month
    drifts.
    """
    return (input_tokens * input_per_million + output_tokens * output_per_million) // 1_000_000
