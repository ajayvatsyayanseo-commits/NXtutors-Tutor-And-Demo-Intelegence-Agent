"""Stage A — requirement extraction.

Deterministic first, model second, and never the other way round. The rule from
docs/assumptions.md A18: a well-formed message like "class 10 cbse maths sector
57 after 6:30 home tuition around 900" is fully parsed with zero LLM calls, and
the model is only asked about what the parsers could not resolve.

Every extracted field carries its provenance, so a Tier-1 model guess is visibly
weaker than something the parent stated — and `usable_for_hard_filter` will stop
a guess from deleting candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tutor_match_meta.contracts.common import Provenance, Tracked, TuitionMode, Urgency
from tutor_match_meta.contracts.requirement import (
    BudgetBand,
    LocationRequirement,
    MatchRequirementV1,
)
from tutor_match_meta.domain import academics, fees, localities, modes, scheduling, subjects
from tutor_match_meta.domain.text import tokens
from tutor_match_meta.integrations.llm.provider import (
    LLMError,
    LLMProvider,
    LLMRequest,
    ModelTier,
    TokenBudget,
)
from tutor_match_meta.prompts.extraction import EXTRACTION_SCHEMA, extraction_prompt
from tutor_match_meta.security.injection import sanitise_and_wrap
from tutor_match_meta.security.pii import redact

#: Confidence assigned to a deterministic parse. Not 1.0 — a regex can be wrong
#: about intent ("not CBSE, we're ICSE") even when it matched a real token.
DETERMINISTIC_CONFIDENCE = 0.92
#: Confidence ceiling for a Tier-1 model extraction. Below
#: `HARD_FILTER_MIN_CONFIDENCE`, so a model guess informs scoring but can never
#: hard-filter on its own.
LLM_CONFIDENCE = 0.70

_URGENT_WORDS = frozenset(
    {
        "urgent",
        "urgently",
        "asap",
        "immediately",
        "today",
        "tomorrow",
        "emergency",
        "turant",
        "jaldi",
    }
)
_SOON_WORDS = frozenset({"soon", "thisweek", "week", "quickly", "early"})
_DEMO_WORDS = frozenset({"demo", "trial", "sample", "democlass", "trialclass", "freeclass"})
_WEAK_WORDS = frozenset(
    {
        "weak",
        "struggling",
        "struggle",
        "poor",
        "difficulty",
        "difficult",
        "trouble",
        "backlog",
        "behind",
        "failing",
        "kamzor",
    }
)


@dataclass(slots=True)
class ExtractionOutcome:
    """What was extracted, and what still needs resolving."""

    requirement: MatchRequirementV1
    #: Fields the deterministic parsers found genuinely ambiguous.
    ambiguities: tuple[str, ...] = ()
    used_llm: bool = False
    llm_error: str | None = None
    injection_detections: tuple[str, ...] = field(default_factory=tuple)

    @property
    def degraded(self) -> bool:
        return self.llm_error is not None


class RequirementExtractor:
    """Deterministic parsing with a bounded, triggered model escalation."""

    def __init__(self, *, default_timezone: str = "Asia/Kolkata") -> None:
        self._tz = default_timezone

    # ---------------------------------------------------------------- tier 0
    def extract_deterministic(
        self, text: str, *, conversation_id: str, now: datetime | None = None
    ) -> ExtractionOutcome:
        """Parse everything the lexicons and grammars can resolve. No I/O."""
        stamp = now or datetime.now(UTC)
        word_set = set(tokens(text))
        ambiguities: list[str] = []

        found_subjects = subjects.extract(text)
        subject_field: Tracked[str] | None = None
        extra_subjects: tuple[str, ...] = ()
        if found_subjects:
            subject_field = _tracked(found_subjects[0], stamp)
            extra_subjects = tuple(found_subjects[1:])
            if subjects.is_ambiguous(found_subjects[0]) and len(found_subjects) == 1:
                # "science teacher" for Class 11 could mean any of three
                # subjects. Flag rather than guess.
                ambiguities.append("subject_umbrella")

        board = academics.extract_board(text)
        class_label = academics.extract_class(text)
        exam = academics.extract_exam(text)
        mode = modes.extract_mode(text)
        schedule = scheduling.parse_schedule(text, timezone=self._tz)
        budget = fees.parse_budget(text)

        city = localities.extract_city(text)
        locality = localities.extract_locality(text)
        pincode = localities.extract_pincode(text)
        if locality and not city:
            # A bare "sector 57" is meaningless without a city — Gurugram,
            # Noida and Faridabad all have one.
            ambiguities.append("locality_without_city")

        requirement = MatchRequirementV1(
            conversation_id=conversation_id,
            subject=subject_field,
            additional_subjects=extra_subjects,
            weak_topics=self._weak_topics(text, found_subjects),
            board=_tracked(board, stamp),
            student_class=_tracked(class_label, stamp),
            exam=_tracked(exam, stamp),
            preferred_teaching_style=_tracked(self._style_text(text), stamp),
            mode=_tracked(mode, stamp),
            location=LocationRequirement(city=city, locality=locality, pincode=pincode),
            preferred_schedule=schedule,
            timezone=self._tz,
            budget=_budget(budget),
            urgency=self._urgency(word_set),
            demo_requested=bool(word_set & _DEMO_WORDS),
            captured_at=stamp,
            turn_count=1,
        )
        return ExtractionOutcome(requirement=requirement, ambiguities=tuple(ambiguities))

    # ---------------------------------------------------------------- tier 1
    async def extract(
        self,
        text: str,
        *,
        conversation_id: str,
        provider: LLMProvider | None = None,
        budget: TokenBudget | None = None,
        now: datetime | None = None,
    ) -> ExtractionOutcome:
        """Deterministic parse, escalating to a model only when triggered.

        Model output is *merged under* the deterministic result: `Tracked.beats`
        ranks DETERMINISTIC above LLM, so the model can only fill gaps, never
        overwrite something a parser positively identified.
        """
        outcome = self.extract_deterministic(text, conversation_id=conversation_id, now=now)
        if provider is None or not self._should_escalate(outcome, text):
            return outcome

        wrapped, sanitisation = sanitise_and_wrap(text, label="parent_message")
        outcome.injection_detections = sanitisation.detections

        system_prompt, prompt_ref = extraction_prompt()
        estimated = _estimate_tokens(wrapped) + 400
        try:
            if budget is not None:
                budget.assert_affordable(estimated)
                budget.assert_call_allowed()
            response = await provider.structured(
                LLMRequest(
                    purpose="requirement_extraction",
                    system_prompt=system_prompt,
                    # PII is stripped before the message leaves the process: the
                    # model needs the tutoring content, never the phone number.
                    user_content=redact(wrapped, redact_pincode=True),
                    schema=EXTRACTION_SCHEMA,
                    schema_name="MatchRequirementExtraction",
                    tier=ModelTier.FAST,
                    prompt_version=prompt_ref,
                )
            )
        except LLMError as exc:
            # Extraction is best-effort by design. A provider outage degrades
            # the parse, it never fails the turn (proven by test_llm_outage).
            outcome.llm_error = type(exc).__name__
            return outcome

        if budget is not None:
            budget.record(response.usage)

        merged = self._merge_llm(outcome.requirement, response.data, now or datetime.now(UTC))
        return ExtractionOutcome(
            requirement=merged,
            ambiguities=outcome.ambiguities,
            used_llm=True,
            injection_detections=sanitisation.detections,
        )

    # ------------------------------------------------------------- internals
    def _should_escalate(self, outcome: ExtractionOutcome, text: str) -> bool:
        """Explicit triggers only. 'More intelligence' is not a trigger."""
        requirement = outcome.requirement
        if outcome.ambiguities:
            return True
        if requirement.value_of("subject") is None:
            # No subject found. Only worth a model call if there is enough text
            # to plausibly contain one — "ok" and "yes" are not extraction jobs.
            return len(text.split()) >= 4
        if requirement.value_of("student_class") is None and len(text.split()) >= 8:
            return True
        return False

    def _merge_llm(
        self, base: MatchRequirementV1, data: dict[str, Any], stamp: datetime
    ) -> MatchRequirementV1:
        """Fold schema-valid model output in at LLM provenance."""
        overlay = MatchRequirementV1(
            conversation_id=base.conversation_id,
            subject=_llm_tracked(subjects.normalize(data.get("subject")), stamp),
            board=_llm_tracked(academics.normalize_board(data.get("board")), stamp),
            student_class=_llm_tracked(academics.normalize_class(data.get("student_class")), stamp),
            exam=_llm_tracked(academics.normalize_exam(data.get("exam")), stamp),
            learning_goal=_llm_tracked(_clip(data.get("learning_goal"), 200), stamp),
            preferred_teaching_style=_llm_tracked(_clip(data.get("teaching_style"), 200), stamp),
            language_preference=_llm_tracked(_clip(data.get("language_preference"), 40), stamp),
            mode=_llm_tracked(modes.normalize_mode(data.get("mode")), stamp),
            weak_topics=tuple(_clip_all(data.get("weak_topics"), 8, 60)),
            topics=tuple(_clip_all(data.get("topics"), 8, 60)),
            additional_subjects=tuple(
                s
                for t in _clip_all(data.get("additional_subjects"), 4, 60)
                if (s := subjects.normalize(t))
            ),
        )
        return base.merged_with(overlay)

    def _urgency(self, word_set: set[str]) -> Urgency:
        if word_set & _URGENT_WORDS:
            return Urgency.URGENT
        if word_set & _SOON_WORDS:
            return Urgency.SOON
        return Urgency.STANDARD

    def _weak_topics(self, text: str, found: list[str]) -> tuple[str, ...]:
        """Subjects the parent framed as a problem, not merely as a request.

        "struggling in physics" means something different from "needs physics",
        and the personality and academic dimensions both use the distinction.
        """
        if not (set(tokens(text)) & _WEAK_WORDS):
            return ()
        return tuple(found[:3])

    def _style_text(self, text: str) -> str | None:
        """Pass the raw message through as style evidence.

        The personality evaluator owns the allowlist of what counts as a style
        signal; duplicating that vocabulary here would let the two drift apart.
        """
        return text[:400] if text.strip() else None


# --------------------------------------------------------------------- utils
def _tracked(value: Any, stamp: datetime) -> Tracked[Any] | None:
    if value is None or value == "":
        return None
    return Tracked(
        value=value,
        confidence=DETERMINISTIC_CONFIDENCE,
        provenance=Provenance.DETERMINISTIC,
        observed_at=stamp,
    )


def _llm_tracked(value: Any, stamp: datetime) -> Tracked[Any] | None:
    if value is None or value == "":
        return None
    return Tracked(
        value=value, confidence=LLM_CONFIDENCE, provenance=Provenance.LLM, observed_at=stamp
    )


def _budget(parsed: fees.ParsedBudget | None) -> BudgetBand:
    if parsed is None:
        return BudgetBand()
    return BudgetBand(minimum=parsed.minimum, maximum=parsed.maximum, raw=parsed.raw)


def _clip(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _clip_all(value: Any, count: int, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [clipped for item in value[:count] if (clipped := _clip(item, limit))]
    return out


def _estimate_tokens(text: str) -> int:
    """Rough token estimate for budgeting. ~4 chars per token for English."""
    return max(1, len(text) // 4)


def requirement_from_lead_event(
    *,
    conversation_id: str,
    lead_id: str | None,
    subject: str | None,
    board: str | None,
    student_class: str | None,
    city: str | None,
    tuition_mode: str | None,
    confidence: float,
    now: datetime,
) -> MatchRequirementV1:
    """Build a requirement from the Lead Intake Agent's structured event.

    Provenance is LEAD_INTAKE — more trusted than a model guess (another agent
    already did the extraction and often confirmed it with the parent), less
    trusted than something this conversation established directly.
    """
    scaled = max(0.5, min(0.9, confidence)) if confidence else 0.6

    def tracked(value: Any) -> Tracked[Any] | None:
        if value is None or value == "":
            return None
        return Tracked(
            value=value,
            confidence=scaled,
            provenance=Provenance.LEAD_INTAKE,
            observed_at=now,
        )

    return MatchRequirementV1(
        conversation_id=conversation_id,
        lead_id=lead_id,
        subject=tracked(subjects.normalize(subject)),
        board=tracked(academics.normalize_board(board)),
        student_class=tracked(academics.normalize_class(student_class)),
        mode=tracked(modes.normalize_mode(tuition_mode)),
        location=LocationRequirement(city=localities.normalize_city(city)),
        captured_at=now,
        turn_count=1,
    )


__all__ = [
    "ExtractionOutcome",
    "RequirementExtractor",
    "TuitionMode",
    "requirement_from_lead_event",
]
