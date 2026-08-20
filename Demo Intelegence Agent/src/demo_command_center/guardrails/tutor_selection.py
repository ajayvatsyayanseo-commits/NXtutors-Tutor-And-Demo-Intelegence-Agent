"""Tutor selection integrity.

The rule: **a tutor reference may only enter the booking path from a snapshot we
persisted when we presented the options.** Not from user text, not from LLM
output, not from an ops console field.

Without this, three separate attacks all work:

* A parent types a tutor id they saw elsewhere and gets them booked without ever
  passing the matching policy's hard filters.
* The extraction model hallucinates a plausible-looking ref and we send a
  confirmation request to nobody.
* A prompt-injected message ("select tutor tut_admin") reaches the selection
  handler as if the parent had chosen it.

Selection therefore resolves an *ordinal or a name fragment* against the stored
snapshot. The ref is looked up, never accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from demo_command_center.contracts.tutor_match import TutorCandidateV1

#: Ordinals a parent actually types, mapped to a 1-based index.
_ORDINALS: dict[str, int] = {
    "1": 1, "one": 1, "first": 1, "pehla": 1, "a": 1,
    "2": 2, "two": 2, "second": 2, "dusra": 2, "b": 2,
    "3": 3, "three": 3, "third": 3, "teesra": 3, "c": 3,
}  # fmt: skip

_REJECT_WORDS = (
    "none", "neither", "no one", "nobody", "other", "others", "different",
    "someone else", "koi aur", "dusra dikhao", "alternatives", "more options",
)  # fmt: skip

#: Button ids we mint when presenting options. `tutor:1`, `tutor:2`, `tutor:3`.
_BUTTON = re.compile(r"^tutor:(?P<ordinal>[1-3])$")


class TutorSelectionRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Selection:
    """A resolved choice. `candidate` is always from the snapshot."""

    candidate: TutorCandidateV1 | None = None
    rejected_all: bool = False
    ambiguous: bool = False
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.candidate is not None


def resolve(
    text: str, candidates: tuple[TutorCandidateV1, ...], *, button_id: str | None = None
) -> Selection:
    """Map what the parent said onto one of the candidates we showed them.

    Returns an unresolved `Selection` rather than raising for ordinary ambiguity
    — "the first one or maybe the second" is a conversation, not an error.
    """
    if not candidates:
        return Selection(reason="no_candidates_presented")

    if button_id:
        match = _BUTTON.match(button_id.strip().lower())
        if match:
            index = int(match.group("ordinal"))
            if index <= len(candidates):
                return Selection(candidate=candidates[index - 1])
            return Selection(reason="button_ordinal_out_of_range")

    lowered = " ".join(text.lower().split())
    if not lowered:
        return Selection(reason="empty_selection")

    if any(word in lowered for word in _REJECT_WORDS):
        return Selection(rejected_all=True, reason="alternatives_requested")

    by_name = _match_by_name(lowered, candidates)
    if len(by_name) == 1:
        return Selection(candidate=by_name[0])
    if len(by_name) > 1:
        return Selection(ambiguous=True, reason="multiple_name_matches")

    by_ordinal = _match_by_ordinal(lowered, candidates)
    if len(by_ordinal) == 1:
        return Selection(candidate=by_ordinal[0])
    if len(by_ordinal) > 1:
        return Selection(ambiguous=True, reason="multiple_ordinals")

    return Selection(reason="unrecognised_selection")


def _match_by_name(text: str, candidates: tuple[TutorCandidateV1, ...]) -> list[TutorCandidateV1]:
    """Whole-token first-name match. Substring matching would let 'an' hit
    'Ananya' and 'Anand' at once, and worse, hit them from unrelated words."""
    tokens = set(re.findall(r"[a-z]{3,}", text))
    hits: list[TutorCandidateV1] = []
    for candidate in candidates:
        name_tokens = {part.lower() for part in candidate.name.split() if len(part) >= 3}
        if tokens & name_tokens:
            hits.append(candidate)
    return hits


def _match_by_ordinal(
    text: str, candidates: tuple[TutorCandidateV1, ...]
) -> list[TutorCandidateV1]:
    hits: list[TutorCandidateV1] = []
    for token in re.findall(r"[a-z0-9]+", text):
        index = _ORDINALS.get(token)
        if index is not None and index <= len(candidates):
            found = candidates[index - 1]
            if found not in hits:
                hits.append(found)
    return hits


def assert_from_snapshot(
    tutor_ref: str, candidates: tuple[TutorCandidateV1, ...]
) -> TutorCandidateV1:
    """The hard gate. Raises unless `tutor_ref` is one we actually presented.

    Called on the booking path with whatever ref the orchestrator is about to
    act on, wherever it came from. It is the single place that makes
    "never accept a tutor id supplied only by the LLM" enforceable rather than
    aspirational.
    """
    for candidate in candidates:
        if candidate.tutor_ref == tutor_ref:
            return candidate
    raise TutorSelectionRejected(f"tutor_not_in_presented_candidates:{tutor_ref[:24]}")
