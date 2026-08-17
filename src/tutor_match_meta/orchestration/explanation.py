"""Stage G — turning an approved evidence bundle into human text.

Two rules define this module.

**Facts are computed, never generated.** Every sentence is assembled from claims
that already passed `EvidenceGuard`. The LLM, when used at all, is given the
finished facts and asked only to phrase them — it never sees a database dump and
is never asked to decide who the best tutor is.

**It should read like a coordinator, not a chatbot.** Short. No "I am an AI", no
"I can assist you", no numbered questionnaires, no manufactured enthusiasm. If
there is nothing evidence-backed to say about a tutor, the entry is short — that
is the honest outcome, not a bug to paper over.
"""

from __future__ import annotations

from dataclasses import dataclass

from tutor_match_meta.contracts.common import Dimension, TuitionMode
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.contracts.scoring import ScoredCandidate, ShortlistEntry
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain import modes as mode_domain
from tutor_match_meta.orchestration.evidence_guard import Claim, ClaimKind, EvidenceGuard
from tutor_match_meta.scoring.policy import ScoringPolicy

#: WhatsApp renders long messages badly and parents do not read them.
MAX_MESSAGE_CHARS = 900


@dataclass(frozen=True, slots=True)
class ExplainedShortlist:
    entries: tuple[ShortlistEntry, ...]
    message: str
    refusals: tuple[str, ...] = ()


class ExplanationBuilder:
    """Builds shortlist entries and the outbound message, deterministically."""

    def __init__(self, policy: ScoringPolicy, *, public_base_url: str) -> None:
        self._policy = policy
        self._guard = EvidenceGuard(policy)
        self._base_url = public_base_url

    def build_entry(
        self,
        *,
        rank: int,
        tutor: TutorCandidate,
        candidate: ScoredCandidate,
        profile_url: str,
        requirement: MatchRequirementV1,
        availability_label: str | None = None,
    ) -> tuple[ShortlistEntry, tuple[str, ...]]:
        """One shortlist entry, with every claim guard-checked."""
        proposed = self._propose_claims(
            tutor=tutor,
            candidate=candidate,
            requirement=requirement,
            availability_label=availability_label,
        )
        result = self._guard.filter(proposed, candidate)
        approved = {claim.kind: claim for claim in result.approved}

        reasons = self._reason_sentences(approved, candidate)
        entry = ShortlistEntry(
            rank=rank,
            tutor_id=tutor.tutor_id,
            name=tutor.name,
            profile_url=profile_url,
            reasons=reasons[: self._policy.explanation.max_reasons_per_tutor],
            mode_label=approved[ClaimKind.MODE].text if ClaimKind.MODE in approved else None,
            locality_label=(
                approved[ClaimKind.LOCALITY].text if ClaimKind.LOCALITY in approved else None
            ),
            availability_label=(
                approved[ClaimKind.AVAILABILITY].text
                if ClaimKind.AVAILABILITY in approved
                else None
            ),
            fee_label=approved[ClaimKind.FEE].text if ClaimKind.FEE in approved else None,
            internal_notes=self._internal_notes(candidate),
        )
        return entry, tuple(result.refusal_reasons)

    # ------------------------------------------------------------- proposals
    def _propose_claims(
        self,
        *,
        tutor: TutorCandidate,
        candidate: ScoredCandidate,
        requirement: MatchRequirementV1,
        availability_label: str | None,
    ) -> list[Claim]:
        """Everything we *might* say. The guard decides what survives."""
        claims: list[Claim] = [Claim(ClaimKind.NAME, tutor.name)]

        academic = candidate.scores.get(Dimension.ACADEMIC)
        wanted_class = requirement.value_of("student_class")
        wanted_board = requirement.value_of("board")
        if academic and wanted_class and "class_exact" in academic.reason_codes:
            label = f"{wanted_board} {wanted_class}" if wanted_board else str(wanted_class)
            claims.append(Claim(ClaimKind.CLASS, f"already teaches {label}", academic.evidence))

        expertise = candidate.scores.get(Dimension.SUBJECT_EXPERTISE)
        subject = requirement.value_of("subject")
        if expertise and subject and "subjects_all_covered" in expertise.reason_codes:
            claims.append(Claim(ClaimKind.SUBJECT, f"teaches {subject}", expertise.evidence))

        if availability_label:
            availability = candidate.scores.get(Dimension.AVAILABILITY)
            claims.append(
                Claim(
                    ClaimKind.AVAILABILITY,
                    availability_label,
                    availability.evidence if availability else (),
                )
            )

        proximity = candidate.scores.get(Dimension.PROXIMITY)
        if proximity and tutor.locality:
            if "same_locality" in proximity.reason_codes:
                claims.append(
                    Claim(ClaimKind.LOCALITY, f"based in {tutor.locality}", proximity.evidence)
                )
            elif "same_city" in proximity.reason_codes and tutor.city:
                claims.append(
                    Claim(ClaimKind.LOCALITY, f"based in {tutor.city}", proximity.evidence)
                )

        performance = candidate.scores.get(Dimension.PERFORMANCE)
        if performance and tutor.reviews.count and tutor.reviews.rating_avg:
            claims.append(
                Claim(
                    ClaimKind.RATING,
                    f"rated {tutor.reviews.rating_avg:.1f} across {tutor.reviews.count} reviews",
                    performance.evidence,
                )
            )
        if performance and tutor.experience_years:
            claims.append(
                Claim(
                    ClaimKind.EXPERIENCE,
                    f"{tutor.experience_years} years of teaching experience",
                    performance.evidence,
                )
            )

        negotiation = candidate.scores.get(Dimension.NEGOTIATION)
        if negotiation and tutor.fee.label:
            # No unit is appended — the column has none (assumptions A3).
            claims.append(Claim(ClaimKind.FEE, tutor.fee.label, negotiation.evidence))

        mode = requirement.value_of("mode")
        if isinstance(mode, TuitionMode) and tutor.supports_mode(mode):
            claims.append(Claim(ClaimKind.MODE, mode_domain.mode_label(mode)))

        personality = candidate.scores.get(Dimension.PERSONALITY)
        if personality and "style_overlap" in " ".join(personality.reason_codes):
            claims.append(
                Claim(
                    ClaimKind.TEACHING_STYLE,
                    "matches the teaching style you asked for",
                    personality.evidence,
                )
            )

        return claims

    def _reason_sentences(
        self, approved: dict[ClaimKind, Claim], candidate: ScoredCandidate
    ) -> tuple[str, ...]:
        """Approved claims as short phrases, in the policy's preference order.

        Availability, locality and fee are excluded: they have dedicated fields
        on the entry and are rendered there. Including them here would spend the
        limited reason slots repeating the line above, which is how the
        strongest differentiator — a real review history — got squeezed out.
        """
        order = (
            ClaimKind.CLASS,
            ClaimKind.SUBJECT,
            ClaimKind.RATING,
            ClaimKind.EXPERIENCE,
            ClaimKind.TEACHING_STYLE,
        )
        reasons = [approved[kind].text for kind in order if kind in approved]
        if not reasons:
            # Nothing citable. Say so rather than inventing a reason — this is
            # the honest output for a thin profile.
            reasons = ["available for this requirement; details to be confirmed"]
        _ = candidate
        return tuple(reasons)

    def _internal_notes(self, candidate: ScoredCandidate) -> tuple[str, ...]:
        """Coordinator-only notes. Never rendered into a parent message."""
        notes: list[str] = []
        risk = candidate.scores.get(Dimension.REPLACEMENT_RISK)
        if risk and risk.flags:
            notes.append("risk_flags:" + ",".join(risk.flags[:3]))
        if "low_weight_coverage" in candidate.flags:
            notes.append("thin_evidence_base")
        negotiation = candidate.scores.get(Dimension.NEGOTIATION)
        if negotiation and "requires_fee_approval" in negotiation.flags:
            notes.append("fee_exception_needs_approval")
        if self._policy.explanation.expose_numeric_scores:
            notes.append(f"final_score:{candidate.final_score:.3f}")
        return tuple(notes)

    # --------------------------------------------------------------- message
    def compose_message(
        self, entries: tuple[ShortlistEntry, ...], requirement: MatchRequirementV1
    ) -> str:
        """The WhatsApp text. Short, factual, one clear next step."""
        if not entries:
            return self.compose_no_match(requirement)

        subject = requirement.value_of("subject")
        class_label = requirement.value_of("student_class")
        descriptor = " ".join(str(p) for p in (class_label, subject) if p)

        if len(entries) == 1:
            header = (
                f"One tutor stands out for {descriptor}:" if descriptor else "One tutor stands out:"
            )
        else:
            header = (
                f"Here are {len(entries)} tutors for {descriptor}:"
                if descriptor
                else f"Here are {len(entries)} tutors who fit:"
            )

        blocks = [header, ""]
        for entry in entries:
            blocks.append(self._render_entry(entry))
            blocks.append("")

        blocks.append(
            "Reply with a name and I'll set up a demo class."
            if len(entries) > 1
            else "Reply here and I'll set up a demo class."
        )
        message = "\n".join(blocks).strip()
        return _truncate(message, MAX_MESSAGE_CHARS)

    def _render_entry(self, entry: ShortlistEntry) -> str:
        lines = [f"*{entry.name}*"]
        detail = " · ".join(
            part for part in (entry.mode_label, entry.locality_label, entry.fee_label) if part
        )
        if detail:
            lines.append(detail)
        if entry.reasons:
            lines.append(_sentence_case("; ".join(entry.reasons[:3])))
        if entry.availability_label:
            lines.append(f"Can do: {entry.availability_label}")
        lines.append(entry.profile_url)
        return "\n".join(lines)

    def compose_no_match(
        self, requirement: MatchRequirementV1, *, blocking_rule: str | None = None
    ) -> str:
        """No candidates cleared the bar.

        The suggestion is derived from the constraint that *actually* emptied the
        pool, not from a generic list. Telling an IB parent to "try online" when
        the real problem is that no IB tutor teaches Class 8 wastes their turn
        and ours.
        """
        subject = requirement.value_of("subject")
        where = requirement.location.locality or requirement.location.city
        what = f"{subject} " if subject else ""
        near = f" near {where}" if where else ""

        return (
            f"I couldn't find a {what}tutor who fits everything{near} right now. "
            f"{self._loosening_suggestion(requirement, blocking_rule)}"
        )

    def _loosening_suggestion(
        self, requirement: MatchRequirementV1, blocking_rule: str | None
    ) -> str:
        """The single most useful thing this parent could relax."""
        rule = (blocking_rule or "").lower()

        if "board_supported" in rule:
            board = requirement.value_of("board")
            label = f"{board} " if board else ""
            return (
                f"None of our {label}tutors cover that class yet. "
                "Would a tutor from a different board be acceptable?"
            )
        if "subject_supported" in rule:
            return "Shall I look at a related subject, or widen the area?"
        if "class_supported" in rule:
            return "Shall I include tutors who teach nearby classes?"
        if "fee_negotiable" in rule:
            return "The tutors who fit are above that budget. Shall I ask about package rates?"
        if "within_travel_radius" in rule or "city_reachable" in rule:
            return "Would online work, or shall I widen the area?"
        if "gender" in rule:
            return "Shall I include tutors of any gender?"
        if "below_quality_threshold" in rule:
            return "The closest options were weak fits. Shall I relax one of the requirements?"

        if requirement.value_of("mode") is TuitionMode.ONLINE:
            return "Shall I check a slightly different time or budget?"
        if requirement.budget:
            return "Shall I widen the area, or look slightly above that budget?"
        return "Would online work, or shall I widen the area?"

    def compose_handoff(self, reason: str) -> str:
        """Human handoff. Never explains the internal reason to the parent."""
        _ = reason
        return (
            "Let me get one of our coordinators to look at this personally — "
            "they'll message you shortly."
        )


def _sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _truncate(text: str, limit: int) -> str:
    """Trim to a whole line rather than mid-word."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return cut.rstrip()
