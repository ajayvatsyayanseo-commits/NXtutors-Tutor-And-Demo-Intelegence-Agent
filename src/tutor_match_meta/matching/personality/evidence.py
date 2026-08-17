"""The closed evidence allowlist for teaching-style compatibility.

This module exists to make one boundary impossible to cross by accident:
**only pedagogical and communication characteristics are ever modelled.**

Permitted (docs/assumptions.md A9):
  * `teacher_review.patience` / `.communication` — verified rating sub-scores;
  * explicit self-described *teaching style* phrases in the tutor's own profile;
  * what the parent explicitly asked for.

Forbidden, and not representable by these types: temperament, personality type,
mental-health traits, and any protected attribute (gender, religion, caste,
region, age, disability). There is no code path that produces such a trait,
which is a stronger guarantee than a rule saying not to.
"""

from __future__ import annotations

from enum import StrEnum

from tutor_match_meta.domain.text import normalize_key


class StyleTrait(StrEnum):
    """The complete set of modelled teaching-style traits. Closed by design."""

    PATIENT = "patient"
    STRUCTURED = "structured"
    EXAM_FOCUSED = "exam_focused"
    CONCEPTUAL = "conceptual"
    INTERACTIVE = "interactive"
    FAST_PACED = "fast_paced"
    BEGINNER_FRIENDLY = "beginner_friendly"
    BILINGUAL = "bilingual"


#: Phrases a tutor may write about their own teaching. Matched against the
#: profile narrative only — never against a review's free text, which is a third
#: party's impression rather than a declared approach.
_SELF_DESCRIBED: dict[StyleTrait, tuple[str, ...]] = {
    StyleTrait.PATIENT: ("patient", "patiently", "calm", "supportive", "encouraging"),
    StyleTrait.STRUCTURED: (
        "structured",
        "systematic",
        "organised",
        "organized",
        "planned",
        "stepbystep",
        "methodical",
        "disciplined",
    ),
    StyleTrait.EXAM_FOCUSED: (
        "examoriented",
        "examfocused",
        "boardexam",
        "previousyear",
        "mocktest",
        "testseries",
        "questionbank",
        "scoreimprovement",
    ),
    StyleTrait.CONCEPTUAL: (
        "conceptual",
        "conceptclarity",
        "fundamentals",
        "basics",
        "understanding",
        "fromscratch",
        "rootlevel",
    ),
    StyleTrait.INTERACTIVE: (
        "interactive",
        "discussion",
        "doubtsolving",
        "doubtclearing",
        "twoway",
        "activitybased",
        "practical",
    ),
    StyleTrait.FAST_PACED: ("fastpaced", "intensive", "crash", "rapid", "accelerated"),
    StyleTrait.BEGINNER_FRIENDLY: (
        "beginner",
        "weakstudents",
        "slowlearners",
        "foundation",
        "remedial",
        "buildconfidence",
    ),
    StyleTrait.BILINGUAL: ("bilingual", "hindiandenglish", "hindimedium", "hinglish"),
}

#: What a parent asks for, in their words.
_REQUESTED: dict[StyleTrait, tuple[str, ...]] = {
    StyleTrait.PATIENT: (
        "patient",
        "patience",
        "calm",
        "gentle",
        "kind",
        "understanding",
        "friendly",
        "notstrict",
        "softspoken",
    ),
    StyleTrait.STRUCTURED: (
        "structured",
        "organised",
        "organized",
        "disciplined",
        "strict",
        "regular",
    ),
    StyleTrait.EXAM_FOCUSED: (
        "exam",
        "boards",
        "score",
        "marks",
        "result",
        "percentage",
        "rank",
        "competitive",
    ),
    StyleTrait.CONCEPTUAL: (
        "concept",
        "concepts",
        "basics",
        "fundamentals",
        "understand",
        "clarity",
        "weak",
        "fromscratch",
    ),
    StyleTrait.INTERACTIVE: ("interactive", "engaging", "doubts", "discussion", "fun"),
    # "urgent" is deliberately absent: it means *start soon*, not *teach fast*.
    # Reading it as a pace preference made a patient tutor look like a style
    # conflict for a parent who simply needed someone this week. Urgency has its
    # own field on the requirement and its own policy.
    StyleTrait.FAST_PACED: ("fastpaced", "quickpace", "crash", "intensive", "accelerated"),
    StyleTrait.BEGINNER_FRIENDLY: (
        "struggling",
        "weak",
        "beginner",
        "slow",
        "behind",
        "backlog",
        "confidence",
    ),
    StyleTrait.BILINGUAL: ("hindi", "hinglish", "bilingual", "hindimedium"),
}

#: Pairs that pull in opposite directions. A parent asking for patience and
#: beginner support is poorly served by a self-described crash-course tutor.
CONFLICTING: tuple[tuple[StyleTrait, StyleTrait], ...] = (
    (StyleTrait.PATIENT, StyleTrait.FAST_PACED),
    (StyleTrait.BEGINNER_FRIENDLY, StyleTrait.FAST_PACED),
    (StyleTrait.CONCEPTUAL, StyleTrait.EXAM_FOCUSED),
)

#: Review sub-scores that corroborate a trait. Only these two columns qualify —
#: `expertise` and `reliability` belong to other dimensions.
REVIEW_BACKED: dict[StyleTrait, str] = {
    StyleTrait.PATIENT: "patience_avg",
    StyleTrait.INTERACTIVE: "communication_avg",
}


def traits_from_profile(profile_text: str | None) -> frozenset[StyleTrait]:
    """Traits a tutor explicitly claims about their own teaching approach."""
    if not profile_text:
        return frozenset()
    key = normalize_key(profile_text)
    return frozenset(
        trait
        for trait, phrases in _SELF_DESCRIBED.items()
        if any(phrase in key for phrase in phrases)
    )


def traits_from_request(request_text: str | None) -> frozenset[StyleTrait]:
    """Traits a parent explicitly asked for."""
    if not request_text:
        return frozenset()
    key = normalize_key(request_text)
    return frozenset(
        trait for trait, phrases in _REQUESTED.items() if any(phrase in key for phrase in phrases)
    )


def conflicts_between(
    wanted: frozenset[StyleTrait], offered: frozenset[StyleTrait]
) -> list[tuple[StyleTrait, StyleTrait]]:
    """Requested/offered pairs that are known to work against each other."""
    return [
        (want, have)
        for want, have in CONFLICTING
        if (want in wanted and have in offered) or (have in wanted and want in offered)
    ]
