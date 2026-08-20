"""Objection analysis — the structured output capability 031 must produce.

This is the one place a model's output shape is the contract, so the model is
constrained hard:

* **Every objection carries an evidence reference.** `quote` must be a verbatim
  span from the transcript; `verify_quotes` checks that it actually appears, so
  a fabricated "the parent said it is too expensive" fails validation rather
  than reaching a follow-up message.
* **Explicit and inferred are separate types of claim.** An `INFERRED` objection
  may inform strategy and may be summarised for a human. It may never be quoted
  back to the parent as something they said — `quotable_to_customer` is what the
  message builder filters on.
* **Categories are a closed enum.** The discount policy keys off them; a
  free-string category would silently match no band and award nothing, or worse,
  be coerced into one that pays out.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from demo_command_center.contracts.common import SCHEMA_VERSION, Confidence, Evidence

#: A quote longer than this is a paraphrase of the whole conversation, not a
#: citation, and defeats the point of requiring one.
MAX_QUOTE_CHARS = 300


class ObjectionCategory(StrEnum):
    """The closed set the discount policy and follow-up strategy key on."""

    PRICE = "price_concern"
    TUTOR_FIT = "tutor_fit_concern"
    TIMING = "timing_concern"
    TRUST = "trust_concern"
    LEARNING_NEED = "learning_need_concern"
    DECISION_MAKER = "decision_maker_dependency"
    COMPETITOR = "competitor_mention"
    LOGISTICS = "logistics_concern"
    NONE = "no_objection"


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    MIXED = "mixed"


class PurchaseIntent(StrEnum):
    READY = "ready"
    INTERESTED = "interested"
    HESITANT = "hesitant"
    UNLIKELY = "unlikely"
    UNKNOWN = "unknown"


class NextStep(StrEnum):
    """What the orchestrator should do next. A closed set, because this value
    is dispatched on — a free string would mean an unhandled branch."""

    SEND_FOLLOWUP = "send_followup"
    #: Answer the objection on its merits before anything commercial. Distinct
    #: from ANSWER_QUESTION: a question wants information, an objection wants
    #: to be taken seriously.
    ADDRESS_OBJECTIONS = "address_objections"
    OFFER_DISCOUNT = "offer_discount"
    OFFER_ALTERNATIVE_TUTOR = "offer_alternative_tutor"
    OFFER_RESCHEDULE = "offer_reschedule"
    ANSWER_QUESTION = "answer_question"
    WAIT_FOR_DECISION_MAKER = "wait_for_decision_maker"
    HUMAN_HANDOFF = "human_handoff"
    CLOSE_AS_LOST = "close_as_lost"
    NONE = "none"


class ObjectionItem(BaseModel):
    """One objection, with the evidence that establishes it."""

    model_config = ConfigDict(frozen=True)

    category: ObjectionCategory
    evidence: Evidence
    #: Verbatim span from the transcript. Required for EXPLICIT; forbidden to be
    #: invented for INFERRED, where the reasoning goes in `rationale` instead.
    quote: str = Field(default="", max_length=MAX_QUOTE_CHARS)
    #: Which message the quote came from. Lets an operator find it in seconds.
    message_ref: str = Field(default="", max_length=64)
    rationale: str = Field(default="", max_length=400)
    confidence: Confidence = Confidence.MEDIUM
    root_cause: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _evidence_matches_claim(self) -> Self:
        if self.evidence is Evidence.EXPLICIT and not self.quote.strip():
            raise ValueError(f"{self.category.value}: an explicit objection requires a quote")
        if self.evidence is Evidence.INFERRED and not self.rationale.strip():
            raise ValueError(f"{self.category.value}: an inferred objection requires a rationale")
        return self

    @property
    def quotable_to_customer(self) -> bool:
        """Only an explicit, high-or-medium confidence objection may be echoed."""
        return (
            self.evidence is Evidence.EXPLICIT
            and self.confidence is not Confidence.LOW
            and bool(self.quote.strip())
        )


class ObjectionAnalysisV1(BaseModel):
    """The full analysis of one post-demo conversation."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    demo_id: str = Field(max_length=64)
    conversation_ref: str = Field(max_length=128)
    objections: tuple[ObjectionItem, ...] = ()
    sentiment: Sentiment = Sentiment.NEUTRAL
    intent: PurchaseIntent = PurchaseIntent.UNKNOWN
    recommended_next_step: NextStep = NextStep.NONE
    summary: str = Field(default="", max_length=600)
    #: Model + prompt version that produced this. Never a bare model name in
    #: code — it comes from settings and is stamped here for audit.
    model_ref: str = Field(default="", max_length=64)
    prompt_version: str = Field(default="", max_length=32)
    analysed_at: datetime

    @model_validator(mode="after")
    def _one_category_once(self) -> Self:
        seen = [o.category for o in self.objections if o.category is not ObjectionCategory.NONE]
        if len(set(seen)) != len(seen):
            raise ValueError(f"duplicate objection categories: {sorted({c.value for c in seen})}")
        return self

    def categories(self, *, explicit_only: bool = False) -> frozenset[ObjectionCategory]:
        """What the discount engine reads. `explicit_only` is what it *should*
        read for a price band — paying out on an inferred price concern is
        discounting against a guess."""
        return frozenset(
            o.category
            for o in self.objections
            if o.category is not ObjectionCategory.NONE
            and (not explicit_only or o.evidence is Evidence.EXPLICIT)
        )

    def has(self, category: ObjectionCategory, *, explicit_only: bool = False) -> bool:
        return category in self.categories(explicit_only=explicit_only)

    def customer_quotable(self) -> tuple[ObjectionItem, ...]:
        return tuple(o for o in self.objections if o.quotable_to_customer)


def verify_quotes(analysis: ObjectionAnalysisV1, transcript: str) -> tuple[str, ...]:
    """Every quote that does not actually appear in the transcript.

    The anti-fabrication check, run on the model's output before the analysis is
    stored. Comparison is whitespace-normalised and case-insensitive: a model
    that reflows a quote across a line break has not fabricated it, and failing
    that case would make the check unusable in practice.
    """
    haystack = " ".join(transcript.split()).casefold()
    bad: list[str] = []
    for item in analysis.objections:
        quote = " ".join(item.quote.split()).casefold()
        if quote and quote not in haystack:
            bad.append(f"{item.category.value}:{item.quote[:60]}")
    return tuple(bad)
