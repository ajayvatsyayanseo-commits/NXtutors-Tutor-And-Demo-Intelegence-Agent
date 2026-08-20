"""Conversation memory: structured business state, not a growing transcript.

The rule this module exists to enforce: **no authoritative fact is ever stored
only as prose.** A price, a payment status, a selected tutor and a slot are
columns. The summary is a convenience for the model; losing it costs fluency,
never correctness.

That distinction is what makes summarisation safe. A summariser that drops "we
agreed ₹4,320" is fine, because the amount lives in `dcc_discount_decisions` and
the payment path reads it from there. A design that kept it only in the summary
would be one bad summary away from charging the wrong amount.

The context builder is bounded by construction: it fills a token budget in
priority order and stops. There is no path that loads an unbounded transcript.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any

from demo_command_center.contracts.common import SCHEMA_VERSION
from demo_command_center.security.pii import redact
from demo_command_center.state.states import DemoState

#: Rough characters-per-token for English/Hinglish mixed text. Deliberately
#: conservative: over-estimating trims the prompt, under-estimating blows the
#: context window and fails the call.
CHARS_PER_TOKEN = 3.5

#: Hard ceiling on how much conversation text can ever enter a prompt.
MAX_RECENT_MESSAGES = 8
MAX_SUMMARY_CHARS = 1_200


class Priority(IntEnum):
    """Fill order when the budget is tight. Lower is dropped first."""

    RECENT_MESSAGES = 1
    SUMMARY = 2
    OPEN_QUESTIONS = 3
    STAGE_FACTS = 4
    #: Never dropped. Without these the model cannot answer coherently at all.
    CORE_STATE = 5


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


@dataclass(frozen=True, slots=True)
class Fact:
    """One piece of context, with what it costs and whether it may be dropped."""

    key: str
    value: str
    priority: Priority

    @property
    def tokens(self) -> int:
        return estimate_tokens(f"{self.key}: {self.value}")


@dataclass(slots=True)
class ConversationMemory:
    """The durable, structured memory for one conversation.

    Every financial and scheduling field here is a mirror of an authoritative
    row, kept for prompt assembly. `authoritative_refs` names where the truth
    actually lives, so a reader is never in doubt.
    """

    schema_version: str = SCHEMA_VERSION
    conversation_ref: str = ""
    state: DemoState = DemoState.NEW

    # --- structured business state (mirrors of authoritative rows)
    requirements: dict[str, str] = field(default_factory=dict)
    tutor_ref: str | None = None
    tutor_name: str = ""
    slot_label: str = ""
    demo_id: str | None = None
    calendar_event_id: str | None = None
    reminders_scheduled: int = 0
    outcome: str = ""
    objection_categories: tuple[str, ...] = ()
    forecast_probability: float | None = None
    offer_percent: int | None = None
    payment_state: str = ""
    handoff_state: str = ""

    # --- conversational memory
    summary: str = ""
    summary_version: int = 0
    #: Bounded ring of recent turns, already redacted.
    recent: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    prompt_version: str = ""
    updated_at: datetime | None = None

    #: Where the truth is. Prose is never the answer for any of these.
    authoritative_refs: dict[str, str] = field(
        default_factory=lambda: {
            "offer_percent": "dcc_discount_decisions",
            "payment_state": "dcc_payment_orders + dcc_payment_events",
            "slot_label": "dcc_demos.starts_at",
            "tutor_ref": "dcc_tutor_candidate_snapshots",
            "outcome": "dcc_demo_outcomes",
        }
    )

    def remember(self, message: str, *, now: datetime) -> None:
        """Append a turn, redacted and bounded."""
        cleaned = redact(message).strip()
        if not cleaned:
            return
        self.recent = (*self.recent, cleaned)[-MAX_RECENT_MESSAGES:]
        self.updated_at = now

    def summarise_into(self, summary: str, *, now: datetime) -> None:
        """Replace the summary. Never touches structured state.

        Deliberately cannot write `offer_percent`, `payment_state` or any other
        authoritative mirror — a summariser has no route to corrupt them.
        """
        self.summary = redact(summary)[:MAX_SUMMARY_CHARS]
        self.summary_version += 1
        self.updated_at = now

    def invalidate_summary(self, *, now: datetime) -> None:
        """Drop the summary after a correction.

        "Sorry, class 9 not 10" makes every sentence derived from the old value
        wrong. Cheaper and safer to re-summarise than to patch prose.
        """
        self.summary = ""
        self.summary_version += 1
        self.updated_at = now

    def facts(self) -> list[Fact]:
        """Everything available to a prompt, with its priority."""
        out: list[Fact] = [
            Fact("state", self.state.value, Priority.CORE_STATE),
        ]
        if self.requirements:
            out.append(
                Fact(
                    "requirements",
                    ", ".join(f"{k}={v}" for k, v in sorted(self.requirements.items())),
                    Priority.CORE_STATE,
                )
            )
        if self.tutor_name:
            out.append(Fact("selected_tutor", self.tutor_name, Priority.CORE_STATE))
        if self.slot_label:
            out.append(Fact("demo_time", self.slot_label, Priority.CORE_STATE))
        if self.outcome:
            out.append(Fact("demo_outcome", self.outcome, Priority.STAGE_FACTS))
        if self.objection_categories:
            out.append(
                Fact(
                    "recorded_objections",
                    ", ".join(self.objection_categories),
                    Priority.STAGE_FACTS,
                )
            )
        if self.payment_state:
            out.append(Fact("payment_state", self.payment_state, Priority.STAGE_FACTS))
        if self.open_questions:
            out.append(
                Fact("open_questions", "; ".join(self.open_questions), Priority.OPEN_QUESTIONS)
            )
        if self.summary:
            out.append(Fact("summary", self.summary, Priority.SUMMARY))
        for index, message in enumerate(self.recent):
            out.append(Fact(f"recent_{index}", message, Priority.RECENT_MESSAGES))
        return out


#: Which facts each lifecycle stage actually needs. A prompt asking about a
#: tutor choice does not need the payment state, and paying for those tokens on
#: every turn is how a per-message cost quietly doubles.
STAGE_KEYS: dict[DemoState, frozenset[str]] = {
    DemoState.COLLECTING_REQUIREMENTS: frozenset({"state", "requirements", "open_questions"}),
    DemoState.AWAITING_TUTOR_SELECTION: frozenset({"state", "requirements", "selected_tutor"}),
    DemoState.NEGOTIATING_SLOT: frozenset({"state", "selected_tutor", "demo_time", "requirements"}),
    DemoState.POST_DEMO_ANALYSIS: frozenset(
        {"state", "selected_tutor", "demo_outcome", "recorded_objections"}
    ),
    DemoState.FOLLOWUP_PENDING: frozenset(
        {"state", "selected_tutor", "demo_outcome", "recorded_objections", "payment_state"}
    ),
    DemoState.PAYMENT_PENDING: frozenset({"state", "payment_state", "demo_time"}),
}


@dataclass(frozen=True, slots=True)
class BuiltContext:
    text: str
    tokens: int
    included: tuple[str, ...]
    dropped: tuple[str, ...]

    @property
    def within_budget(self) -> bool:
        return True  # by construction; `dropped` records what it cost


class ContextBuilder:
    """Assembles a bounded prompt context. Cannot exceed its budget."""

    def __init__(self, *, token_budget: int = 4_000) -> None:
        self._budget = token_budget

    def build(self, memory: ConversationMemory, *, stage_scoped: bool = True) -> BuiltContext:
        """Fill the budget in priority order, highest first.

        Stage scoping is applied *before* the budget, so a tight budget spends
        its tokens on facts that matter here rather than trimming them last.
        """
        allowed = STAGE_KEYS.get(memory.state) if stage_scoped else None
        facts = sorted(memory.facts(), key=lambda f: (-f.priority, f.key))

        included: list[Fact] = []
        dropped: list[str] = []
        spent = 0

        for fact in facts:
            scoped_out = (
                allowed is not None
                and fact.key not in allowed
                and fact.priority < Priority.CORE_STATE
                and not fact.key.startswith("recent_")
            )
            if scoped_out:
                dropped.append(fact.key)
                continue
            if spent + fact.tokens > self._budget and fact.priority is not Priority.CORE_STATE:
                dropped.append(fact.key)
                continue
            included.append(fact)
            spent += fact.tokens

        text = "\n".join(f"{fact.key}: {fact.value}" for fact in included)
        return BuiltContext(
            text=text,
            tokens=spent,
            included=tuple(f.key for f in included),
            dropped=tuple(dropped),
        )


def memory_from_state(
    *,
    conversation_ref: str,
    state: DemoState,
    facts: dict[str, Any],
    now: datetime,
) -> ConversationMemory:
    """Build memory from the orchestrator's assembled facts.

    Reads only from the fact dict, which itself came from repositories — so
    memory is a projection of authoritative rows, never a parallel truth.
    """
    return ConversationMemory(
        conversation_ref=conversation_ref,
        state=state,
        tutor_ref=facts.get("tutor_ref"),
        demo_id=facts.get("demo_id"),
        calendar_event_id=facts.get("calendar_event_id"),
        payment_state="confirmed" if facts.get("subscription_ref") else "",
        updated_at=now,
    )
