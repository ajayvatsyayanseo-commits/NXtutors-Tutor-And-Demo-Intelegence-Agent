"""`ModelContext` — the one place data is allowed to leave for a model.

Every LLM call in this service builds one of these first. It is a **positive**
construction: fields are copied in one at a time by name, so a new column on
`TutorCandidate` cannot reach a provider merely by existing. A denylist would
have the opposite failure mode, and the failure mode is the whole point —
adding `tutor.aadhaar_number` to a projection should not silently start
shipping it to OpenAI.

What a ranking or explanation call may see about a tutor:

    pseudonym         cand_1, cand_2 — never the tutor id, never the name
    subjects/boards   what they teach
    class range       who they teach
    experience        years, banded
    rating summary    average and sample size
    locality label    "Sector 57" — never coordinates, never an address
    fee band          only when the family already stated a budget

What it may never see, enforced by `assert_no_forbidden_fields` and asserted
against real payloads in `tests/security/test_model_payload.py`:

    tutor_id, name, phone, email, address, coordinates, document numbers,
    account identifiers, internal risk scores, admin flags, other families'
    data, or anything the family did not supply.

The parent side is equally narrow: the requirement's *tutoring* content only,
with PII redacted and the pincode removed — pincode plus class plus locality
narrows to a small enough group to be worth withholding even though no single
field is identifying.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.contracts.scoring import ScoredCandidate
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.security.injection import sanitise
from tutor_match_meta.security.pii import redact

#: Substrings that must never appear as a key in a model payload. Matched on
#: the key, not the value: the type system already tells us what these fields
#: are, so we do not need to guess from their contents (§3).
FORBIDDEN_KEY_MARKERS: tuple[str, ...] = (
    "phone",
    "mobile",
    "email",
    "address",
    "latitude",
    "longitude",
    "geo",
    "coordinate",
    "aadhaar",
    "pan",
    "passport",
    "document",
    "account",
    "password",
    "otp",
    "token",
    "secret",
    "api_key",
    "tutor_id",
    "user_id",
    "public_ref",
    "risk",
    "internal",
    "admin",
    "commission",
    "margin",
    "payout",
)

#: Whole-field names that are forbidden even though the marker scan would miss
#: them. `name` is the important one: a tutor's real name in a ranking payload
#: re-identifies the pseudonym and defeats the point of having one.
FORBIDDEN_KEYS: frozenset[str] = frozenset({"name", "avatar_url", "pincode", "dob", "birth_date"})


class ForbiddenFieldError(ValueError):
    """A model payload contained a field it must never carry."""


def assert_no_forbidden_fields(payload: Any, *, where: str = "model_payload") -> None:
    """Walk a payload and refuse anything on the forbidden list.

    Runs on every real call, not only in tests. The cost is a dictionary walk
    over a few dozen keys; the alternative is discovering the leak in a
    provider's retention logs.
    """
    for path, key in _walk_keys(payload):
        lowered = key.lower()
        if lowered in FORBIDDEN_KEYS or any(m in lowered for m in FORBIDDEN_KEY_MARKERS):
            raise ForbiddenFieldError(f"{where}: field {path!r} must not be sent to a model")


def _walk_keys(node: Any, prefix: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.append((path, str(key)))
            found.extend(_walk_keys(value, path))
    elif isinstance(node, list | tuple):
        for index, value in enumerate(node):
            found.extend(_walk_keys(value, f"{prefix}[{index}]"))
    return found


# ------------------------------------------------------------------ tutors


@dataclass(frozen=True, slots=True)
class CandidateView:
    """What a model may know about one tutor. Pseudonymous by construction."""

    pseudonym: str
    subjects: tuple[str, ...]
    boards: tuple[str, ...]
    class_range: str
    experience_band: str
    rating_summary: str
    locality_label: str
    #: Present only when the family stated a budget, so the model can phrase
    #: "within your budget" — never the tutor's actual rate card.
    fee_fit: str | None = None
    #: Guard-approved evidence strings. Nothing else may be cited.
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, (), "")}


def _experience_band(years: int | None) -> str:
    if years is None:
        return "unknown"
    if years < 2:
        return "under 2 years"
    if years < 5:
        return "2-5 years"
    if years < 10:
        return "5-10 years"
    return "10+ years"


def _rating_summary(tutor: TutorCandidate) -> str:
    reviews = tutor.reviews
    if not reviews.count or reviews.rating_avg is None:
        return "no reviews yet"
    return f"{reviews.rating_avg:.1f} from {reviews.count} reviews"


def _class_range(tutor: TutorCandidate) -> str:
    classes = [str(c) for c in tutor.capabilities.classes if c]
    return ", ".join(classes[:6]) if classes else "unspecified"


def candidate_view(
    tutor: TutorCandidate,
    *,
    pseudonym: str,
    evidence: tuple[str, ...] = (),
    fee_fit: str | None = None,
) -> CandidateView:
    """Project one tutor down to the model-visible surface.

    Note what is *not* read: `tutor_id`, `public_ref`, `name`, `geo`,
    `avatar_url`, `pincode`. The projection is by explicit copy, so those
    cannot arrive by accident.
    """
    return CandidateView(
        pseudonym=pseudonym,
        subjects=tuple(str(s) for s in tutor.capabilities.subjects[:8]),
        boards=tuple(str(b) for b in tutor.capabilities.boards[:6]),
        class_range=_class_range(tutor),
        experience_band=_experience_band(tutor.experience_years),
        rating_summary=_rating_summary(tutor),
        # Locality label only. Coordinates stay inside the proximity evaluator,
        # which returns a distance band, never a point (§21).
        locality_label=str(tutor.locality or tutor.city or "not stated"),
        fee_fit=fee_fit,
        evidence=tuple(evidence[:6]),
    )


# ------------------------------------------------------------- requirements


@dataclass(frozen=True, slots=True)
class RequirementView:
    """What a model may know about the family's request."""

    subject: str | None = None
    board: str | None = None
    student_class: str | None = None
    mode: str | None = None
    city: str | None = None
    #: Locality label as the parent typed it, sanitised. No pincode.
    area: str | None = None
    urgency: str | None = None
    schedule_hint: str | None = None
    has_budget: bool = False
    weak_topics: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, (), False)}


def requirement_view(requirement: MatchRequirementV1) -> RequirementView:
    """Project the requirement to its tutoring content.

    `lead_id`, `conversation_id`, any phone hash and the pincode are all
    dropped. The model is being asked to phrase a sentence about tutoring; none
    of those help it do that.
    """

    def value(name: str) -> str | None:
        raw = requirement.value_of(name)
        return _clean(str(raw)) if raw else None

    schedule = requirement.preferred_schedule
    hint = ", ".join(schedule.labels(limit=2)) if schedule else ""
    return RequirementView(
        subject=value("subject"),
        board=value("board"),
        student_class=value("student_class"),
        mode=value("mode"),
        city=_clean(requirement.location.city) if requirement.location.city else None,
        area=_clean(requirement.location.locality) if requirement.location.locality else None,
        urgency=str(requirement.urgency.value) if requirement.urgency else None,
        schedule_hint=_clean(hint),
        has_budget=requirement.budget.maximum is not None,
        weak_topics=tuple(c for t in requirement.weak_topics[:4] if (c := _clean(t))),
    )


_WHITESPACE = re.compile(r"\s+")


def _clean(text: str | None) -> str | None:
    """Redact, sanitise, collapse. Applied to every free-text field."""
    if not text:
        return None
    stripped = sanitise(redact(text, redact_pincode=True)).text
    collapsed = _WHITESPACE.sub(" ", stripped).strip()
    return collapsed[:120] or None


# ----------------------------------------------------------------- context


@dataclass(frozen=True, slots=True)
class ModelContext:
    """The complete payload for one model call. Nothing else is sent."""

    purpose: str
    requirement: RequirementView
    candidates: tuple[CandidateView, ...] = ()
    #: Sanitised RAG passages. Already wrapped as untrusted data by the caller.
    knowledge: tuple[str, ...] = ()
    #: Bounded conversation context. Never the full history (§14).
    recent_turns: tuple[str, ...] = ()
    notes: dict[str, str] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        """Serialise, then verify. The verification is not optional."""
        payload: dict[str, Any] = {
            "purpose": self.purpose,
            "request": self.requirement.as_dict(),
        }
        if self.candidates:
            payload["candidates"] = [c.as_dict() for c in self.candidates]
        if self.knowledge:
            payload["reference"] = list(self.knowledge)
        if self.recent_turns:
            payload["recent"] = list(self.recent_turns)
        if self.notes:
            payload["notes"] = dict(self.notes)
        assert_no_forbidden_fields(payload, where=f"model_context[{self.purpose}]")
        return payload

    def as_json(self) -> str:
        import json

        return json.dumps(self.as_payload(), ensure_ascii=False, separators=(",", ":"))


def explanation_context(
    *,
    requirement: MatchRequirementV1,
    tutors: dict[str, TutorCandidate],
    shortlist: tuple[ScoredCandidate, ...],
    approved_evidence: dict[str, tuple[str, ...]],
    knowledge: tuple[str, ...] = (),
) -> ModelContext:
    """Build the explanation payload from guard-approved evidence only.

    `approved_evidence` comes from the evidence guard, which has already
    discarded any dimension whose data was missing, stale, or below the
    policy's minimum sample size. Passing anything else here would let the
    model cite a number nobody can support.
    """
    views = tuple(
        candidate_view(
            tutors[c.tutor_id],
            pseudonym=c.pseudonym,
            evidence=approved_evidence.get(c.tutor_id, ()),
            fee_fit="within stated budget" if requirement.budget.maximum is not None else None,
        )
        for c in shortlist
        if c.tutor_id in tutors
    )
    return ModelContext(
        purpose="shortlist_explanation",
        requirement=requirement_view(requirement),
        candidates=views,
        knowledge=knowledge,
    )


__all__ = [
    "FORBIDDEN_KEYS",
    "FORBIDDEN_KEY_MARKERS",
    "CandidateView",
    "ForbiddenFieldError",
    "ModelContext",
    "RequirementView",
    "assert_no_forbidden_fields",
    "candidate_view",
    "explanation_context",
    "requirement_view",
]
