"""Asking about one named tutor, rather than asking to be matched.

Two different questions arrive on the same WhatsApp thread:

    "class 10 cbse maths tutor in gurugram"   -> match me
    "tell me about Sneha Joshi"               -> describe this person
    "is Rajesh mahera available?"             -> is this person on the books

The service only ever answered the first. `Intent.SPECIFIC_TUTOR` existed in the
enum and in `OWNED_INTENTS`, but no pattern ever produced it, so a parent naming
a tutor got "Which subject do you need help with?" — which reads as not
listening.

**How a name is found, and why it is safe.** There is no name lexicon, so this
works by subtraction: strip every word the domain already understands (subjects,
boards, classes, cities, modes, tuition words, stopwords) and see what is left.
Whatever remains is a *candidate* name, never a confirmed one.

That leaves a false-positive risk — "reykjavik" is not in the city table either.
The defence is that a candidate name is worth nothing until the database
confirms it: `TurnService` looks it up, and a lookup that matches no tutor falls
through to normal matching. So a wrong guess costs one indexed query and changes
nothing the parent sees.

Nothing here fabricates. `summarise()` emits only fields the projection actually
holds; a tutor with no rating gets no rating line, not a hedged one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain import academics, localities, modes, subjects
from tutor_match_meta.domain.text import tokens

#: Words that mean "I am asking about a person", not "match me".
_ABOUT = re.compile(
    r"\b(?:tell\s+me\s+about|about|who\s+is|summar(?:y|ise|ize)|summarise|summarize|"
    r"profile\s+of|details?\s+of|info(?:rmation)?\s+(?:about|on)|"
    r"kaun\s+hai|ke\s+baare|batao)\b",
    re.IGNORECASE,
)

#: Words that name a person without asking for a description.
_LOOKUP = re.compile(
    r"\b(?:is|has|does)\b.{0,40}?\b(?:available|free|teach|teaching)\b"
    r"|\b(?:i\s+want|book|assign|give\s+me|chahiye)\b",
    re.IGNORECASE,
)

#: Tuition vocabulary. Present in both kinds of message, so it is stripped from
#: the name rather than used to decide.
_ROLE_WORDS = frozenset(
    {
        "tutor",
        "tutors",
        "teacher",
        "teachers",
        "sir",
        "madam",
        "maam",
        "ma'am",
        "tuition",
        "tution",
        "coaching",
        "faculty",
        "mentor",
        "guru",
        "ji",
    }
)

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "about",
        "me",
        "my",
        "mine",
        "i",
        "you",
        "your",
        "he",
        "she",
        "they",
        "it",
        "this",
        "that",
        "these",
        "those",
        "want",
        "need",
        "looking",
        "look",
        "search",
        "find",
        "get",
        "give",
        "show",
        "tell",
        "know",
        "please",
        "pls",
        "plz",
        "can",
        "could",
        "would",
        "will",
        "shall",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "any",
        "some",
        "available",
        "free",
        "book",
        "booking",
        "assign",
        "details",
        "detail",
        "info",
        "information",
        "profile",
        "summary",
        "summarise",
        "summarize",
        "who",
        "what",
        "which",
        "where",
        "when",
        "how",
        "why",
        "kaun",
        "hai",
        "ka",
        "ki",
        "ke",
        "baare",
        "batao",
        "chahiye",
        "mujhe",
        "aur",
        "se",
        "as",
        # Structural words that survive the domain check because they carry no
        # value on their own: `class_number("class")` is None without a digit,
        # so "class" was being read as a surname on every requirement message.
        "class",
        "std",
        "standard",
        "grade",
        "section",
        "near",
        "nearby",
        "around",
        "sector",
        "block",
        "phase",
        "area",
        "locality",
        "city",
        "budget",
        "fee",
        "fees",
        "rs",
        "rupees",
        "inr",
        "per",
        "hour",
        "month",
        "morning",
        "evening",
        "afternoon",
        "weekend",
        "weekdays",
        "time",
    }
)

#: A name is at most this many words. Longer means the subtraction kept prose,
#: not a name, and a lookup on it would be noise. Three covers the Indian
#: given-name/middle/surname range; at four, the SQL-injection strings in
#: `tests/security/test_sql_injection.py` started being read as people.
MAX_NAME_WORDS = 3
#: Below this, a "name" is too generic to search on — one two-letter token would
#: match hundreds of tutors.
MIN_NAME_CHARS = 3


@dataclass(frozen=True, slots=True)
class TutorLookup:
    """A candidate name, unconfirmed until the database says otherwise."""

    #: Whitespace-joined leftover tokens, lowercased.
    name: str
    #: True when the parent asked *about* the tutor rather than merely naming
    #: them. Decides whether the reply is a profile or a short availability line.
    wants_summary: bool

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(self.name.split())


def _is_known_vocabulary(token: str) -> bool:
    """True when the domain already understands this word.

    Anything that answers here belongs to the requirement, not to a name.
    """
    if token in _STOPWORDS or token in _ROLE_WORDS:
        return True
    # `extract`/`extract_board`, never `normalize`/`normalize_board`. The
    # normalisers are permissive by design — given an unrecognised word they
    # title-case it and hand it back rather than returning None, because a real
    # but unlisted subject should survive. Used as a membership test they call
    # every word a subject, which made this function answer True for everything
    # and left no name behind.
    if subjects.extract(token):
        return True
    if academics.extract_board(token):
        return True
    if academics.class_number(token) is not None:
        return True
    if modes.parse_mode_tokens(token):
        return True
    # `extract_city`, not `normalize_city`: the latter title-cases whatever it
    # is given and never returns None, so using it here marked *every* word as a
    # known city and no name ever survived the subtraction.
    if localities.extract_city(token):
        return True
    return token.isdigit()


def _has_requirement_signal(text: str) -> bool:
    """True when the message names a subject, class, board or city."""
    if subjects.extract(text):
        return True
    if academics.extract_class(text) or academics.extract_board(text):
        return True
    return bool(localities.extract_city(text) or localities.extract_pincode(text))


def detect(text: str) -> TutorLookup | None:
    """Extract a candidate tutor name, or None when this is not that kind of
    message.

    Returns None rather than guessing whenever the leftovers do not look like a
    person: no tokens, too many tokens, or tokens too short to search on.
    """
    if not text or not text.strip():
        return None

    wants_summary = bool(_ABOUT.search(text))
    named = bool(_LOOKUP.search(text))
    mentions_role = any(token in _ROLE_WORDS for token in tokens(text))

    # Some signal is required. A bare "class 10 maths" must never be read as a
    # person, and without this guard every unmatched word would become a name.
    if not (wants_summary or named or mentions_role):
        return None

    # A message carrying real requirement content is a matching request, not a
    # question about a person — "class 12 IB astrophysics tutor in reykjavik"
    # leaves "astrophysics reykjavik" behind, which is a subject and a place,
    # not a name. An explicit "tell me about X" still wins, because a parent
    # may name a tutor and a class in one breath.
    if not wants_summary and _has_requirement_signal(text):
        return None

    leftovers = [token for token in tokens(text) if not _is_known_vocabulary(token)]
    if not leftovers or len(leftovers) > MAX_NAME_WORDS:
        return None
    if sum(len(token) for token in leftovers) < MIN_NAME_CHARS:
        return None

    # A single leftover word is only a name when the parent explicitly asked
    # about someone. Otherwise it is far more likely to be a place we do not
    # have in the city table — "a tutor in patna" leaves "patna" — and
    # querying for a tutor named Patna on every such message is pure waste.
    if len(leftovers) == 1 and not wants_summary:
        return None

    return TutorLookup(name=" ".join(leftovers), wants_summary=wants_summary)


# --------------------------------------------------------------------- summary
def summarise(tutor: TutorCandidate, *, profile_url: str | None = None) -> str:
    """A short, fully grounded description of one tutor.

    Every line is a fact the projection holds. A field that is absent produces
    no line at all — never "rating not available", which reads as a fact about
    the tutor rather than about our data.
    """
    lines: list[str] = [f"*{tutor.name}*"]

    where = tutor.locality or tutor.city
    place = f"based in {where}" if where else None
    fee = tutor.fee.label if tutor.fee and tutor.fee.minimum is not None else None
    mode = ", ".join(_mode_label(m) for m in tutor.capabilities.modes) or None
    lines.append(" · ".join(part for part in (mode, place, fee) if part) or "")

    teaches: list[str] = []
    if tutor.capabilities.subjects:
        teaches.append("Teaches " + ", ".join(tutor.capabilities.subjects[:4]))
    if tutor.capabilities.classes:
        teaches.append("for " + ", ".join(tutor.capabilities.classes[:4]))
    if tutor.capabilities.boards:
        teaches.append("(" + ", ".join(tutor.capabilities.boards[:3]) + ")")
    if teaches:
        lines.append(" ".join(teaches))

    credentials: list[str] = []
    if tutor.experience_years:
        years = tutor.experience_years
        credentials.append(f"{years} year{'s' if years != 1 else ''} of experience")
    if tutor.education:
        credentials.append(tutor.education[:80])
    if credentials:
        lines.append(" · ".join(credentials))

    # A rating is only quoted when there are enough reviews for it to mean
    # something; the count alone is not evidence of quality.
    reviews = tutor.reviews
    if reviews.count and reviews.rating_avg is not None:
        # One decimal, the same as the shortlist renders. Showing 4.63 here
        # and 4.6 there reads as two different numbers for one tutor.
        lines.append(f"Rated {reviews.rating_avg:.1f} across {reviews.count} reviews")
    elif reviews.count:
        lines.append(f"{reviews.count} review{'s' if reviews.count != 1 else ''} on file")

    if tutor.profile_summary:
        lines.append(_clip(tutor.profile_summary, 180))

    if profile_url:
        lines.append(profile_url)

    return "\n".join(line for line in lines if line)


def _mode_label(mode: object) -> str:
    value = getattr(mode, "value", str(mode))
    return {"home": "Home tuition", "online": "Online", "hybrid": "Home or online"}.get(
        value, str(value).title()
    )


def _clip(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rsplit(" ", 1)[0] + "…"
