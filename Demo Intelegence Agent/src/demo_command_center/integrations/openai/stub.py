"""The deterministic LLM stub — the default when no OpenAI key is configured.

Not a mock. It produces schema-valid output using cheap heuristics, so a local
run of the full lifecycle exercises every downstream validator, guard and
persistence path exactly as it would in production. A stub that returned `{}`
would leave the objection validator, the quote verifier and the discount trigger
mapping entirely untested by `make demo`.

Its answers are intentionally conservative: `Evidence.INFERRED` with `LOW`
confidence, so nothing it produces is quotable to a customer and nothing it
produces alone qualifies for a discount band.
"""

from __future__ import annotations

import re
from typing import Any

from demo_command_center.contracts.common import Confidence, Evidence
from demo_command_center.domain.objections import (
    NextStep,
    ObjectionCategory,
    PurchaseIntent,
    Sentiment,
)

#: Keyword → category. Deliberately small: this is a fallback, and a big keyword
#: table would start looking like a classifier we trust.
_SIGNALS: tuple[tuple[ObjectionCategory, tuple[str, ...]], ...] = (
    (
        ObjectionCategory.PRICE,
        ("expensive", "costly", "budget", "afford", "mehenga", "price", "fees"),
    ),
    (
        ObjectionCategory.TUTOR_FIT,
        ("another teacher", "different tutor", "not comfortable", "accent"),
    ),
    (ObjectionCategory.TIMING, ("busy", "no time", "later", "next month", "baad mein")),
    (ObjectionCategory.TRUST, ("sure", "genuine", "reviews", "trust", "scam")),
    (
        ObjectionCategory.DECISION_MAKER,
        ("husband", "wife", "father", "mother", "papa", "ask at home"),
    ),
    (ObjectionCategory.COMPETITOR, ("byju", "vedantu", "unacademy", "other institute")),
)

_POSITIVE = ("good", "great", "helpful", "nice", "achha", "accha", "loved", "excellent")
_NEGATIVE = ("bad", "poor", "boring", "waste", "not good", "bekar")


class StubLlm:
    """Heuristic, deterministic, offline."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def structured(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(purpose)
        if purpose == "objection_extraction":
            return self._objections(user)
        if purpose == "intent_classification":
            return self._intent(user)
        return {}

    # ------------------------------------------------------------- purposes
    def _objections(self, text: str) -> dict[str, Any]:
        lowered = text.lower()
        objections: list[dict[str, Any]] = []

        for category, keywords in _SIGNALS:
            hit = next((word for word in keywords if word in lowered), None)
            if hit is None:
                continue
            objections.append(
                {
                    "category": category.value,
                    # INFERRED, never EXPLICIT: the stub has no reliable way to
                    # produce a verbatim span, and an unverifiable quote would
                    # be discarded by the transcript check anyway.
                    "evidence": Evidence.INFERRED.value,
                    "rationale": f"Keyword signal {hit!r} present in the conversation.",
                    "confidence": Confidence.LOW.value,
                    "root_cause": "",
                    "quote": "",
                    "message_ref": "",
                }
            )

        return {
            "objections": objections,
            "sentiment": self._sentiment(lowered).value,
            "intent": (
                PurchaseIntent.HESITANT.value if objections else PurchaseIntent.UNKNOWN.value
            ),
            "recommended_next_step": (
                NextStep.ADDRESS_OBJECTIONS.value if objections else NextStep.SEND_FOLLOWUP.value
            ),
            "summary": (
                f"Heuristic analysis found {len(objections)} possible objection(s). "
                "Produced by the offline stub provider; treat as low confidence."
            ),
        }

    @staticmethod
    def _sentiment(lowered: str) -> Sentiment:
        positive = any(word in lowered for word in _POSITIVE)
        negative = any(word in lowered for word in _NEGATIVE)
        if positive and negative:
            return Sentiment.MIXED
        if positive:
            return Sentiment.POSITIVE
        if negative:
            return Sentiment.NEGATIVE
        return Sentiment.NEUTRAL

    @staticmethod
    def _intent(text: str) -> dict[str, Any]:
        lowered = text.lower()
        if re.search(r"\b(yes|haan|ok|okay|confirm|book)\b", lowered):
            return {"intent": "confirm", "confidence": Confidence.MEDIUM.value}
        if re.search(r"\b(no|nahi|cancel|stop)\b", lowered):
            return {"intent": "decline", "confidence": Confidence.MEDIUM.value}
        if re.search(r"\b(reschedule|change time|postpone)\b", lowered):
            return {"intent": "reschedule", "confidence": Confidence.MEDIUM.value}
        return {"intent": "unknown", "confidence": Confidence.LOW.value}
