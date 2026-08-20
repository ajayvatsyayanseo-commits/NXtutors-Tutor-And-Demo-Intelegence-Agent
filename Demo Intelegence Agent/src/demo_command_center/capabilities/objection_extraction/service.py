"""Capability 031 — Objection Extraction.

The one capability where an LLM produces the primary output. Everything around
it exists to make that safe:

* The model is given a **redacted** transcript (`redact` with pincodes on) and
  the text is fenced as untrusted data.
* It must answer in a **strict JSON schema**. There is no free-text path.
* Its output is re-validated as `ObjectionAnalysisV1`, which refuses an explicit
  objection with no quote and an inferred one with no rationale.
* Every quote is checked **against the actual transcript**. A fabricated
  citation is dropped, and if enough are dropped the whole analysis is refused
  rather than partially trusted.

A refused analysis is not a failure of the turn: `empty_analysis()` returns a
well-formed "no objections established", which is the honest answer when we
could not establish any.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from demo_command_center.capabilities.objection_extraction.schema import (
    OBJECTION_SCHEMA,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from demo_command_center.contracts.common import Confidence, Evidence
from demo_command_center.contracts.ports import LlmPort, ProviderError
from demo_command_center.domain.objections import (
    NextStep,
    ObjectionAnalysisV1,
    ObjectionCategory,
    ObjectionItem,
    PurchaseIntent,
    Sentiment,
    verify_quotes,
)
from demo_command_center.observability.logging import get_logger

logger = get_logger("capability.objections")

#: Above this share of fabricated quotes, the whole analysis is discarded. A
#: model that invents half its citations is not having an off day on one field.
MAX_FABRICATION_RATIO = 0.34

PROMPT_VERSION = "objections.v1"


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    analysis: ObjectionAnalysisV1
    #: Quotes that did not appear in the transcript. Always recorded, even when
    #: the analysis survives — this is the metric that catches drift.
    fabricated: tuple[str, ...] = ()
    refused: bool = False
    refusal_reason: str = ""


class ObjectionExtractionCapability:
    def __init__(self, llm: LlmPort, *, model_ref: str) -> None:
        self._llm = llm
        self._model_ref = model_ref

    async def extract(
        self,
        *,
        demo_id: str,
        conversation_ref: str,
        transcript: str,
        now: datetime,
        max_output_tokens: int = 900,
    ) -> ExtractionResult:
        if not transcript.strip():
            return ExtractionResult(
                analysis=self.empty_analysis(demo_id, conversation_ref, now=now),
                refused=True,
                refusal_reason="empty_transcript",
            )

        try:
            raw = await self._llm.structured(
                purpose="objection_extraction",
                system=SYSTEM_PROMPT,
                user=build_user_prompt(transcript),
                schema=OBJECTION_SCHEMA,
                max_output_tokens=max_output_tokens,
            )
        except ProviderError as exc:
            logger.warning("objection extraction unavailable", extra={"dcc_provider": exc.provider})
            return ExtractionResult(
                analysis=self.empty_analysis(demo_id, conversation_ref, now=now),
                refused=True,
                refusal_reason=f"llm_unavailable:{exc.code or 'error'}",
            )

        try:
            analysis = self._to_analysis(
                raw, demo_id=demo_id, conversation_ref=conversation_ref, now=now
            )
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "objection output failed validation", extra={"dcc_error": str(exc)[:200]}
            )
            return ExtractionResult(
                analysis=self.empty_analysis(demo_id, conversation_ref, now=now),
                refused=True,
                refusal_reason="schema_validation_failed",
            )

        fabricated = verify_quotes(analysis, transcript)
        if not fabricated:
            return ExtractionResult(analysis=analysis)

        quoted = [o for o in analysis.objections if o.quote.strip()]
        ratio = len(fabricated) / max(len(quoted), 1)
        if ratio > MAX_FABRICATION_RATIO:
            logger.error(
                "objection analysis discarded: too many unverifiable quotes",
                extra={"dcc_fabricated": str(len(fabricated)), "dcc_quoted": str(len(quoted))},
            )
            return ExtractionResult(
                analysis=self.empty_analysis(demo_id, conversation_ref, now=now),
                fabricated=fabricated,
                refused=True,
                refusal_reason="quotes_not_in_transcript",
            )

        # A minority of bad quotes: drop those items, keep the rest. Dropping is
        # safe because an objection we cannot cite is one we must not act on.
        bad = {item.split(":", 1)[0] for item in fabricated}
        kept = tuple(o for o in analysis.objections if o.category.value not in bad)
        return ExtractionResult(
            analysis=analysis.model_copy(update={"objections": kept}), fabricated=fabricated
        )

    def empty_analysis(
        self, demo_id: str, conversation_ref: str, *, now: datetime
    ) -> ObjectionAnalysisV1:
        """The honest "we established nothing" result."""
        return ObjectionAnalysisV1(
            demo_id=demo_id,
            conversation_ref=conversation_ref,
            objections=(),
            sentiment=Sentiment.NEUTRAL,
            intent=PurchaseIntent.UNKNOWN,
            recommended_next_step=NextStep.SEND_FOLLOWUP,
            summary="",
            model_ref=self._model_ref,
            prompt_version=PROMPT_VERSION,
            analysed_at=now,
        )

    # ------------------------------------------------------------- internals
    def _to_analysis(
        self, raw: dict[str, Any], *, demo_id: str, conversation_ref: str, now: datetime
    ) -> ObjectionAnalysisV1:
        """Coerce model output into the domain type. Unknown enum values are
        dropped rather than mapped to a neighbour — a category we do not
        recognise must not become one that pays out a discount."""
        items: list[ObjectionItem] = []
        seen: set[ObjectionCategory] = set()

        for entry in raw.get("objections", []) or []:
            category = _enum(ObjectionCategory, entry.get("category"))
            if category is None or category is ObjectionCategory.NONE or category in seen:
                continue
            seen.add(category)
            items.append(
                ObjectionItem(
                    category=category,
                    evidence=_enum(Evidence, entry.get("evidence")) or Evidence.INFERRED,
                    quote=str(entry.get("quote") or "")[:300],
                    message_ref=str(entry.get("message_ref") or "")[:64],
                    rationale=str(entry.get("rationale") or "")[:400],
                    confidence=_enum(Confidence, entry.get("confidence")) or Confidence.LOW,
                    root_cause=str(entry.get("root_cause") or "")[:200],
                )
            )

        return ObjectionAnalysisV1(
            demo_id=demo_id,
            conversation_ref=conversation_ref,
            objections=tuple(items),
            sentiment=_enum(Sentiment, raw.get("sentiment")) or Sentiment.NEUTRAL,
            intent=_enum(PurchaseIntent, raw.get("intent")) or PurchaseIntent.UNKNOWN,
            recommended_next_step=_enum(NextStep, raw.get("recommended_next_step"))
            or NextStep.SEND_FOLLOWUP,
            summary=str(raw.get("summary") or "")[:600],
            model_ref=self._model_ref,
            prompt_version=PROMPT_VERSION,
            analysed_at=now,
        )


def _enum(enum_type: Any, value: Any) -> Any:
    """Strict enum coercion. Returns None for anything unrecognised."""
    if not isinstance(value, str):
        return None
    try:
        return enum_type(value)
    except ValueError:
        return None
