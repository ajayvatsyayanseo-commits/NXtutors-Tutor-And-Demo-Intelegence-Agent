"""Subject normalisation, synonym mapping and relatedness.

Canonical names are kept byte-identical to the Laravel `SubjectNormalizer` so the
projection, the website search and this matcher all agree on what "Maths" is.
The alias table here is a superset — it adds Hinglish and exam-shorthand forms
the website does not need but WhatsApp messages contain constantly.

Subject matching here is deliberately **stricter** than the website's current
substring containment, which lets "Science" match "Computer Science" and would
hand a Computer Science tutor to a Class 8 Science parent.
"""

from __future__ import annotations

from tutor_match_meta.domain.text import normalize_key, tokens

# ---------------------------------------------------------------- canonical
MATHEMATICS = "Mathematics"
SCIENCE = "Science"
PHYSICS = "Physics"
CHEMISTRY = "Chemistry"
BIOLOGY = "Biology"
ENGLISH = "English"
HINDI = "Hindi"
SOCIAL_SCIENCE = "Social Science"
COMPUTER_SCIENCE = "Computer Science"
ACCOUNTANCY = "Accountancy"
ECONOMICS = "Economics"
BUSINESS_STUDIES = "Business Studies"
SANSKRIT = "Sanskrit"
FRENCH = "French"
GERMAN = "German"
POLITICAL_SCIENCE = "Political Science"
HISTORY = "History"
GEOGRAPHY = "Geography"
PSYCHOLOGY = "Psychology"

_ALIASES: dict[str, str] = {
    # Mathematics
    "maths": MATHEMATICS,
    "math": MATHEMATICS,
    "mathematics": MATHEMATICS,
    "mathematic": MATHEMATICS,
    "ganit": MATHEMATICS,
    "mth": MATHEMATICS,
    "appliedmaths": MATHEMATICS,
    "appliedmathematics": MATHEMATICS,
    # Science (umbrella)
    "science": SCIENCE,
    "sci": SCIENCE,
    "vigyan": SCIENCE,
    "generalscience": SCIENCE,
    "evs": SCIENCE,
    "environmentalscience": SCIENCE,
    # Physics
    "physics": PHYSICS,
    "phy": PHYSICS,
    "bhautiki": PHYSICS,
    # Chemistry
    "chemistry": CHEMISTRY,
    "chem": CHEMISTRY,
    "rasayan": CHEMISTRY,
    "organicchemistry": CHEMISTRY,
    "inorganicchemistry": CHEMISTRY,
    "physicalchemistry": CHEMISTRY,
    # Biology
    "biology": BIOLOGY,
    "bio": BIOLOGY,
    "botany": BIOLOGY,
    "zoology": BIOLOGY,
    "jeevvigyan": BIOLOGY,
    # Languages
    "english": ENGLISH,
    "eng": ENGLISH,
    "englishliterature": ENGLISH,
    "englishgrammar": ENGLISH,
    "spokenenglish": ENGLISH,
    "hindi": HINDI,
    "hnd": HINDI,
    "sanskrit": SANSKRIT,
    "french": FRENCH,
    "german": GERMAN,
    # Social
    "sst": SOCIAL_SCIENCE,
    "socialscience": SOCIAL_SCIENCE,
    "socialstudies": SOCIAL_SCIENCE,
    "social": SOCIAL_SCIENCE,
    "samajikvigyan": SOCIAL_SCIENCE,
    "history": HISTORY,
    "itihas": HISTORY,
    "geography": GEOGRAPHY,
    "bhugol": GEOGRAPHY,
    "politicalscience": POLITICAL_SCIENCE,
    "polsci": POLITICAL_SCIENCE,
    "civics": POLITICAL_SCIENCE,
    # Commerce
    "accounts": ACCOUNTANCY,
    "accountancy": ACCOUNTANCY,
    "accounting": ACCOUNTANCY,
    "economics": ECONOMICS,
    "eco": ECONOMICS,
    "econ": ECONOMICS,
    "businessstudies": BUSINESS_STUDIES,
    "bst": BUSINESS_STUDIES,
    "business": BUSINESS_STUDIES,
    # Computing
    "computer": COMPUTER_SCIENCE,
    "computerscience": COMPUTER_SCIENCE,
    "cs": COMPUTER_SCIENCE,
    "informationpractices": COMPUTER_SCIENCE,
    "ip": COMPUTER_SCIENCE,
    "programming": COMPUTER_SCIENCE,
    "python": COMPUTER_SCIENCE,
    "coding": COMPUTER_SCIENCE,
    # Other
    "psychology": PSYCHOLOGY,
}

#: An umbrella subject a tutor teaches implies competence in its parts, but not
#: the reverse: a Physics tutor is not automatically a Science tutor for a Class 8
#: parent who also needs Chemistry and Biology.
_UMBRELLA_PARTS: dict[str, frozenset[str]] = {
    SCIENCE: frozenset({PHYSICS, CHEMISTRY, BIOLOGY}),
    SOCIAL_SCIENCE: frozenset({HISTORY, GEOGRAPHY, POLITICAL_SCIENCE, ECONOMICS}),
}

#: Subjects that are commonly taught together by the same person. Used only as a
#: weak positive signal, never to satisfy a hard subject filter.
_ADJACENT: tuple[frozenset[str], ...] = (
    frozenset({PHYSICS, MATHEMATICS}),
    frozenset({CHEMISTRY, BIOLOGY}),
    frozenset({ACCOUNTANCY, ECONOMICS, BUSINESS_STUDIES}),
    frozenset({HISTORY, GEOGRAPHY, POLITICAL_SCIENCE}),
)

#: Messages saying only this are too vague to filter on; Stage B asks which one.
AMBIGUOUS_SUBJECTS: frozenset[str] = frozenset({SCIENCE, SOCIAL_SCIENCE})


def normalize(subject: str | None) -> str | None:
    """Canonical subject name, or title-cased passthrough for unknown subjects.

    Unknown subjects are preserved rather than dropped: NXTutors adds subjects
    faster than this table is updated, and silently discarding one would delete
    a real tutor capability.
    """
    if not subject or not subject.strip():
        return None
    canonical = _ALIASES.get(normalize_key(subject))
    if canonical:
        return canonical
    return " ".join(word.capitalize() for word in subject.split())


def extract(text: str) -> list[str]:
    """All subjects mentioned in a free-text message, in order of appearance.

    Matches two-token phrases before single tokens so "social science" is not
    read as "science", and "computer science" is not read as "science".
    """
    found: list[str] = []
    word_list = tokens(text)
    index = 0
    while index < len(word_list):
        matched: str | None = None
        consumed = 1
        for span in (3, 2, 1):
            if index + span > len(word_list):
                continue
            phrase = "".join(word_list[index : index + span])
            candidate = _ALIASES.get(phrase)
            if candidate:
                matched, consumed = candidate, span
                break
        if matched and matched not in found:
            found.append(matched)
        index += consumed
    return found


def matches(required: str, taught: str) -> bool:
    """Exact-after-normalisation subject match. The hard filter's predicate.

    Umbrella coverage counts (a Science tutor can teach Physics) but the reverse
    does not. Substring containment is deliberately NOT used.
    """
    want, have = normalize(required), normalize(taught)
    if not want or not have:
        return False
    if want == have:
        return True
    return want in _UMBRELLA_PARTS.get(have, frozenset())


def matches_any(required: str, taught_list: list[str] | tuple[str, ...]) -> bool:
    return any(matches(required, taught) for taught in taught_list)


def is_adjacent(a: str, b: str) -> bool:
    """Weak 'often taught together' signal. Never satisfies a hard filter."""
    left, right = normalize(a), normalize(b)
    if not left or not right or left == right:
        return False
    return any({left, right} <= group for group in _ADJACENT)


def is_ambiguous(subject: str | None) -> bool:
    return normalize(subject) in AMBIGUOUS_SUBJECTS


def parts_of(subject: str) -> frozenset[str]:
    """Component subjects of an umbrella subject; empty for a leaf subject."""
    return _UMBRELLA_PARTS.get(normalize(subject) or "", frozenset())
