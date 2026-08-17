"""Deterministic intent routing and the accept/decline decision.

TutorMatch is a *guest* in a conversation Lead Intake owns. So the first
question on every handoff is not "what should I say" but **"is this even
mine?"** — and the answer has to be deterministic, because two agents both
deciding "yes" is the double-reply bug.

The routing rule that matters is the overlap case:

    "I need a maths tutor and I don't have an account"

Both matching and onboarding intents are present. Randomly picking one loses
information and makes the parent repeat themselves. The policy is:

1. the **primary goal** wins — they want a tutor, the account is a means;
2. collect the matching requirement anyway, so nothing is lost;
3. only when an action genuinely *requires* an account do we hand off;
4. the MatchSession is preserved in a continuation token;
5. on resume, we carry on rather than restarting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from tutor_match_meta.contracts.envelope import AgentId
from tutor_match_meta.domain.text import normalize_key, tokens


class Intent(StrEnum):
    """What the parent is trying to do on this turn."""

    FIND_TUTOR = "find_tutor"
    REPLACE_TUTOR = "replace_tutor"
    COMPARE_TUTORS = "compare_tutors"
    SPECIFIC_TUTOR = "specific_tutor"
    SELECT_FROM_SHORTLIST = "select_from_shortlist"
    REQUEST_DEMO = "request_demo"
    ONBOARDING = "onboarding"
    HUMAN_REQUESTED = "human_requested"
    #: In our lane but not a routing signal (an answer to our own question).
    CONTINUATION = "continuation"
    OTHER = "other"


#: Intents TutorMatch owns. Everything else is declined so Lead Intake keeps it.
OWNED_INTENTS: frozenset[Intent] = frozenset(
    {
        Intent.FIND_TUTOR,
        Intent.REPLACE_TUTOR,
        Intent.COMPARE_TUTORS,
        Intent.SPECIFIC_TUTOR,
        Intent.SELECT_FROM_SHORTLIST,
        Intent.REQUEST_DEMO,
        Intent.CONTINUATION,
    }
)

_FIND_TUTOR = re.compile(
    r"\b(?:find|need|want|looking\s+for|searching|suggest|recommend|arrange|"
    r"get\s+me|show\s+me|chahiye|dhundh)\b[^.\n]{0,40}?"
    r"\b(?:tutor|teacher|tuition|tution|coaching|faculty|sir|madam|ma'?am)\b"
    r"|\b(?:tutor|teacher|tuition|tution)\b[^.\n]{0,25}?\b(?:near|for|in)\b",
    re.IGNORECASE,
)
_REPLACE = re.compile(
    r"\b(?:replace|change|another|different|new)\b[^.\n]{0,25}?\b(?:tutor|teacher)\b"
    r"|\b(?:tutor|teacher)\b[^.\n]{0,25}?\b(?:not\s+working|quit|left|stopped|unhappy)\b",
    re.IGNORECASE,
)
_COMPARE = re.compile(r"\bcompare\b|\bwhich\s+(?:one|tutor|teacher)\s+is\s+better\b", re.I)
_DEMO = re.compile(r"\b(?:demo|trial|sample)\s*(?:class|session|lesson)?\b", re.I)
_HUMAN = re.compile(
    r"\b(?:human|agent|person|counsellor|counselor|staff|someone\s+real|"
    r"talk\s+to\s+(?:a\s+)?(?:human|person|someone)|call\s+me|speak\s+to)\b",
    re.IGNORECASE,
)
#: Mirrors `onboarding_router.SIGNUP_INTENT_RE` in the Lead Intake repo, so both
#: sides agree on what "signup" looks like. Kept in sync by a contract test.
_ONBOARDING = re.compile(
    r"\bsign\s*up\b|\bsignup\b|\bregister\b|\bregistration\b|"
    r"\bcreate\s+(?:an?\s+)?account\b|\bopen\s+(?:an?\s+)?account\b|\bonboard(?:ing)?\b|"
    r"\bjoin\s+as\s+(?:an?\s+)?(?:student|tutor|teacher)\b|"
    r"\bbecome\s+(?:an?\s+)?(?:tutor|teacher)\b|"
    r"\b(?:no|don'?t\s+have|dont\s+have|without)\s+(?:an?\s+)?account\b",
    re.IGNORECASE,
)
#: Subject words that mark a message as a tutoring request.
#:
#: "accounts" is plural-only on purpose. Accountancy is a real NXTutors subject
#: that students call "accounts", but the singular "account" almost always means
#: a *website* account — so matching it made "create an account" look like a
#: tutoring request and stole the message from the onboarding agent.
_SUBJECT_HINT = re.compile(
    r"\b(?:maths?|mathematics|science|physics|chemistry|biology|english|hindi|"
    r"sst|social|computer|accounts|accountancy|economics|jee|neet)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Whether this turn is ours, and what to do about it."""

    intent: Intent
    owned: bool
    #: Set when we must borrow another agent before we can finish.
    handoff_to: AgentId | None = None
    #: Onboarding words were present but matching is the primary goal.
    onboarding_secondary: bool = False
    reason: str = ""

    @property
    def needs_handoff(self) -> bool:
        return self.handoff_to is not None


def classify(text: str, *, in_active_match: bool = False) -> Intent:
    """Classify one message. Deterministic; no model involved.

    `in_active_match` is decisive rather than cosmetic: once we have asked a
    question, "Class 8" is an answer to us, not an ambiguous fragment that some
    other agent should claim.
    """
    if not text or not text.strip():
        return Intent.CONTINUATION if in_active_match else Intent.OTHER

    # An explicit human request always wins, from any state. Never make someone
    # who asked for a person argue with a bot first.
    if _HUMAN.search(text):
        return Intent.HUMAN_REQUESTED

    if _REPLACE.search(text):
        return Intent.REPLACE_TUTOR
    if _COMPARE.search(text):
        return Intent.COMPARE_TUTORS
    if _DEMO.search(text) and not _FIND_TUTOR.search(text):
        return Intent.REQUEST_DEMO
    if _FIND_TUTOR.search(text):
        return Intent.FIND_TUTOR

    onboarding = bool(_ONBOARDING.search(text))
    # A subject named alongside signup words is still a tutoring request:
    # "I want to register, need maths tuition for class 8".
    if onboarding and not _SUBJECT_HINT.search(text):
        return Intent.ONBOARDING
    if onboarding:
        return Intent.FIND_TUTOR

    if in_active_match:
        return Intent.CONTINUATION

    # A bare subject + class with no verb ("class 10 cbse maths") is how most
    # parents actually open. Treat it as a tutoring request.
    if _SUBJECT_HINT.search(text) and len(tokens(text)) <= 12:
        return Intent.FIND_TUTOR
    return Intent.OTHER


def route(
    text: str,
    *,
    in_active_match: bool = False,
    account_required: bool = False,
    has_account: bool = True,
) -> RoutingDecision:
    """The accept/decline/handoff decision for one turn.

    `account_required` is a business policy input, not something we infer. We
    only borrow onboarding when an action truly cannot proceed without an
    account — asking a parent to register before we will even show them tutors
    is how you lose the parent.
    """
    intent = classify(text, in_active_match=in_active_match)

    if intent is Intent.HUMAN_REQUESTED:
        return RoutingDecision(
            intent=intent,
            owned=False,
            handoff_to=AgentId.HUMAN,
            reason="parent_asked_for_human",
        )

    if intent is Intent.ONBOARDING:
        # Pure signup with no tutoring content. Not ours: Lead Intake already
        # routes these to the onboarding agent, and duplicating that here would
        # give the parent two onboarding flows.
        return RoutingDecision(
            intent=intent,
            owned=False,
            handoff_to=AgentId.ONBOARDING,
            reason="pure_onboarding_intent",
        )

    if intent not in OWNED_INTENTS:
        return RoutingDecision(intent=intent, owned=False, reason="not_a_matching_intent")

    secondary = bool(_ONBOARDING.search(text))
    if account_required and not has_account:
        # Matching is still the goal; we borrow onboarding and resume after.
        return RoutingDecision(
            intent=intent,
            owned=True,
            handoff_to=AgentId.ONBOARDING,
            onboarding_secondary=secondary,
            reason="account_required_for_next_action",
        )

    return RoutingDecision(
        intent=intent, owned=True, onboarding_secondary=secondary, reason="matching_intent"
    )


#: Lead Intake's own intent vocabulary (`app/lead_manager_v2/intent_router.py`).
#: When it labels a message for us, we honour its label rather than
#: re-classifying — two classifiers disagreeing about the same message is
#: exactly the ambiguity this phase exists to remove.
LEAD_INTAKE_INTENT_MAP: dict[str, Intent] = {
    "student_lead": Intent.FIND_TUTOR,
    "demo_command_center": Intent.FIND_TUTOR,
    "specific_tutor_request": Intent.SPECIFIC_TUTOR,
    "availability": Intent.CONTINUATION,
    "pricing": Intent.CONTINUATION,
    "callback": Intent.HUMAN_REQUESTED,
    "complaint": Intent.HUMAN_REQUESTED,
    "support": Intent.HUMAN_REQUESTED,
    "tutor_application": Intent.ONBOARDING,
    "personal_memory_bank": Intent.OTHER,
    "ai_tutor": Intent.OTHER,
    "ai_tutor_chat": Intent.OTHER,
}


def intent_from_upstream(label: str | None) -> Intent | None:
    """Translate a Lead Intake intent label, or None if we do not know it."""
    if not label:
        return None
    return LEAD_INTAKE_INTENT_MAP.get(normalize_key(label).replace(" ", "_")) or (
        LEAD_INTAKE_INTENT_MAP.get(label.strip().lower())
    )
