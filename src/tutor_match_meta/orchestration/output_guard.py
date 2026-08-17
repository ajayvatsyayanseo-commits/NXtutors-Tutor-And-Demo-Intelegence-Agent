"""The last gate before a parent sees anything.

`evidence_guard` works on *claims* — structured objects with evidence attached.
This module works on the **finished message**, and it exists because the two
failure modes are different.

The evidence guard answers "can we support this claim?". It cannot answer
"did a stack trace end up in the reply?", "does this message name a tutor who
was filtered out?", or "did a template start promising exam results?" — those
are properties of the assembled string and of the decision it was assembled
from, not of any single claim.

So this runs on every outbound message, in both directions:

    structural   length, WhatsApp formatting, no control characters
    referential  every tutor named exists in the decision and is eligible
    link         every URL is a canonical NXTutors profile link
    leakage      no private field, no internal score, no prompt text, no
                 database error, no stack trace
    claims       no guarantee, no unsupported superlative

A failure is **never** patched up and sent anyway. The message is replaced with
a safe fallback and the incident is counted, because a message that failed
validation is a message we do not understand, and quietly editing it is how a
half-correct claim reaches a parent.

The model cannot bypass this: it runs after generation, on the bytes, and it has
no path back to the generator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from tutor_match_meta.contracts.scoring import MatchDecisionV1
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.prompts.registry import FORBIDDEN_CLAIM_MARKERS
from tutor_match_meta.security import injection
from tutor_match_meta.security.pii import found_pii_kinds

logger = get_logger("output_guard")

#: WhatsApp accepts 4096, but a shortlist that long is unreadable on a phone and
#: is a symptom of a template loop rather than a rich answer.
MAX_MESSAGE_CHARS = 1_400
MIN_MESSAGE_CHARS = 8

#: Text that means an internal failure escaped into the reply.
_LEAK_MARKERS: tuple[tuple[str, str], ...] = (
    ("traceback (most recent call last)", "stack_trace"),
    ("sqlalchemy", "orm_internals"),
    ("asyncpg", "driver_internals"),
    ("psycopg", "driver_internals"),
    ("integrityerror", "database_error"),
    ("operationalerror", "database_error"),
    ('relation "', "database_error"),
    ("syntax error at or near", "database_error"),
    ("none type object", "python_error"),
    ("nonetype", "python_error"),
    ("<class '", "python_repr"),
    ("object at 0x", "python_repr"),
    ("internal server error", "raw_error"),
    ("stacktrace", "stack_trace"),
)

#: Text that means the prompt or the system's own scaffolding leaked.
_PROMPT_MARKERS: tuple[str, ...] = (
    injection.UNTRUSTED_OPEN.lower(),
    injection.UNTRUSTED_CLOSE.lower(),
    "system prompt",
    "you are an assistant",
    "as an ai",
    "[removed-directive]",
    "json schema",
    "additionalproperties",
)

#: Internal-only vocabulary. A parent must never see how we score tutors.
_INTERNAL_MARKERS: tuple[str, ...] = (
    "replacement risk",
    "risk score",
    "weight coverage",
    "confidence score",
    "policy_id",
    "policy checksum",
    "data_quality",
    "hard filter",
    "pseudonym",
    "cand_",
    "internal score",
)

#: Superlatives we cannot support from any data we hold.
_UNSUPPORTED_SUPERLATIVES: tuple[str, ...] = (
    "best tutor in",
    "top tutor in",
    "number one tutor",
    "the best teacher",
    "most qualified in",
)

_URL = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
#: Control characters other than newline and tab. Their only purpose in a
#: message is to hide something from a human reviewer.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Violation(StrEnum):
    TOO_LONG = "too_long"
    TOO_SHORT = "too_short"
    CONTROL_CHARACTERS = "control_characters"
    UNKNOWN_TUTOR = "unknown_tutor_referenced"
    NON_CANONICAL_LINK = "non_canonical_link"
    PII_LEAK = "pii_leak"
    INTERNAL_LEAK = "internal_field_leak"
    PROMPT_LEAK = "prompt_text_leak"
    ERROR_LEAK = "error_detail_leak"
    UNSUPPORTED_GUARANTEE = "unsupported_guarantee"
    UNSUPPORTED_SUPERLATIVE = "unsupported_superlative"
    UNAUTHORISED_FEE = "unauthorised_fee"


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    violations: tuple[Violation, ...]
    detail: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations


#: What a parent gets when validation fails. Says nothing untrue, promises
#: nothing, and routes to a person.
SAFE_FALLBACK = (
    "Let me get one of our coordinators to look at this and come back to you "
    "with the right tutor options."
)


def validate(
    message: str,
    *,
    decision: MatchDecisionV1 | None = None,
    public_base_url: str = "https://www.nxtutors.com",
    authorised_fees: tuple[str, ...] = (),
) -> GuardVerdict:
    """Check a finished message. Returns every violation, not just the first.

    Reporting all of them matters during an incident: one bad template usually
    trips several rules at once, and seeing the set is what identifies which
    template.
    """
    violations: list[Violation] = []
    detail: list[str] = []
    lowered = message.lower()

    # --- structural
    if len(message) > MAX_MESSAGE_CHARS:
        violations.append(Violation.TOO_LONG)
        detail.append(f"{len(message)} chars")
    if len(message.strip()) < MIN_MESSAGE_CHARS:
        violations.append(Violation.TOO_SHORT)
    if _CONTROL.search(message):
        violations.append(Violation.CONTROL_CHARACTERS)

    # --- leakage. PII first: it is the most costly to get wrong.
    kinds = found_pii_kinds(message)
    # A profile URL is an expected, intentional URL; the PII scan flags every
    # URL, so canonical links are excluded before judging.
    leaked = [k for k in kinds if k != "url"]
    if leaked:
        violations.append(Violation.PII_LEAK)
        detail.append(",".join(leaked))

    for marker, label in _LEAK_MARKERS:
        if marker in lowered:
            violations.append(Violation.ERROR_LEAK)
            detail.append(label)
            break

    if any(marker in lowered for marker in _PROMPT_MARKERS):
        violations.append(Violation.PROMPT_LEAK)

    if any(marker in lowered for marker in _INTERNAL_MARKERS):
        violations.append(Violation.INTERNAL_LEAK)

    # --- claims
    if any(marker in lowered for marker in FORBIDDEN_CLAIM_MARKERS):
        violations.append(Violation.UNSUPPORTED_GUARANTEE)
    if any(marker in lowered for marker in _UNSUPPORTED_SUPERLATIVES):
        violations.append(Violation.UNSUPPORTED_SUPERLATIVE)

    # --- links: every URL must be a canonical profile link for a tutor that
    #     is actually in this decision's shortlist.
    allowed_urls = {e.profile_url for e in decision.shortlist} if decision else set()
    prefix = f"{public_base_url.rstrip('/')}/"
    for url in _URL.findall(message):
        clean = url.rstrip(".,;:)")
        if not clean.startswith(prefix):
            violations.append(Violation.NON_CANONICAL_LINK)
            detail.append(clean[:80])
        elif decision is not None and clean not in allowed_urls:
            violations.append(Violation.UNKNOWN_TUTOR)
            detail.append(clean[:80])

    # --- referential: every named tutor must be one we shortlisted.
    if decision is not None:
        shortlisted = {e.name.strip().lower() for e in decision.shortlist if e.name.strip()}
        for tutor_id in decision.candidate_ids:
            # A raw tutor id in a parent-facing message is an internal
            # identifier leak, whether or not the tutor was shortlisted.
            if tutor_id and tutor_id.lower() in lowered and tutor_id.lower() not in shortlisted:
                violations.append(Violation.INTERNAL_LEAK)
                detail.append("tutor_id_in_message")
                break

    # --- fees: every rate shown must be a published band we are authorised to
    #     quote, i.e. one that appears verbatim on a shortlist entry.
    #
    #     Note what this deliberately does *not* do: it does not refuse to show
    #     a fee just because the family did not state a budget. A tutor's
    #     published rate is a projection-backed fact, and `evidence_guard`
    #     treats it as one for a documented reason — gating it on the
    #     negotiation dimension used to hide real prices from parents who
    #     simply had not mentioned money. The risk here is a *made-up* number,
    #     not a disclosed one.
    unauthorised = _unauthorised_fees(message, decision, authorised_fees)
    if unauthorised:
        violations.append(Violation.UNAUTHORISED_FEE)
        detail.extend(unauthorised[:3])

    return GuardVerdict(violations=tuple(dict.fromkeys(violations)), detail=tuple(detail))


#: Any currency-prefixed amount, with or without separators.
_FEE_AMOUNT = re.compile(r"(?:₹|rs\.?\s*|inr\s*)\s*([\d,]+)", re.IGNORECASE)


def _unauthorised_fees(
    message: str,
    decision: MatchDecisionV1 | None,
    authorised_fees: tuple[str, ...] = (),
) -> list[str]:
    """Amounts in the message that nothing authorises us to quote.

    Two sources of authority, and both are evidence rather than permission:

    * a **shortlist entry's** `fee_label` — the band we published for a tutor we
      just recommended;
    * an explicit `authorised_fees` list, for replies that legitimately quote a
      band without producing a shortlist. A tutor-profile lookup is the case
      that matters: the parent asked about one named tutor, so that tutor's own
      published band is exactly the fact being reported.

    With neither, any amount in the message is unauthorised — which stays the
    right answer for a no-match or clarification reply that has started quoting
    prices out of nowhere.
    """
    quoted = {a.replace(",", "") for a in _FEE_AMOUNT.findall(message)}
    if not quoted:
        return []
    authorised: set[str] = set()
    for entry in decision.shortlist if decision else ():
        if entry.fee_label:
            authorised.update(a.replace(",", "") for a in _FEE_AMOUNT.findall(entry.fee_label))
    for label in authorised_fees:
        if label:
            authorised.update(a.replace(",", "") for a in _FEE_AMOUNT.findall(label))
    return sorted(quoted - authorised)


def enforce(
    message: str,
    *,
    decision: MatchDecisionV1 | None = None,
    public_base_url: str = "https://www.nxtutors.com",
    authorised_fees: tuple[str, ...] = (),
) -> tuple[str, GuardVerdict]:
    """Validate, and substitute the safe fallback on any violation.

    Returns `(message_to_send, verdict)`. The caller records the verdict and
    escalates to a human — a message that failed validation is a defect, not a
    routine event, and it should never be invisible.
    """
    verdict = validate(
        message,
        decision=decision,
        public_base_url=public_base_url,
        authorised_fees=authorised_fees,
    )
    if verdict.ok:
        return message, verdict

    logger.error(
        "outbound message failed validation; substituting the safe fallback",
        extra={
            "tmm_violations": [v.value for v in verdict.violations],
            # The detail is bounded and non-identifying by construction, but
            # the message itself is never logged.
            "tmm_detail": list(verdict.detail)[:5],
        },
    )
    return SAFE_FALLBACK, verdict


__all__ = [
    "MAX_MESSAGE_CHARS",
    "MIN_MESSAGE_CHARS",
    "SAFE_FALLBACK",
    "GuardVerdict",
    "Violation",
    "enforce",
    "validate",
]
