"""Boards, classes/grades, exams and academic level.

Canonical class labels match Laravel's `ClassNormalizer` (`"Class 10"`, `"LKG"`)
so the projection and the website agree. Board and exam vocabularies are added
here because the matcher needs them and the website has no equivalent.
"""

from __future__ import annotations

import re
from enum import StrEnum

from tutor_match_meta.domain.text import normalize_key, tokens

# ------------------------------------------------------------------- boards
CBSE = "CBSE"
ICSE = "ICSE"
ISC = "ISC"
IB = "IB"
IGCSE = "IGCSE"
CAMBRIDGE = "Cambridge"
STATE_BOARD = "State Board"
NIOS = "NIOS"

_BOARD_ALIASES: dict[str, str] = {
    "cbse": CBSE,
    # A data-entry typo in `teacher_courses.board` (14 rows). Left
    # unmapped it becomes its own board and those tutors never match a
    # CBSE requirement.
    "cbsc": CBSE,
    "centralboard": CBSE,
    "ncert": CBSE,
    "icse": ICSE,
    "cisce": ICSE,
    "isc": ISC,
    "ib": IB,
    "ibdp": IB,
    "internationalbaccalaureate": IB,
    "myp": IB,
    "pyp": IB,
    "igcse": IGCSE,
    "cambridge": CAMBRIDGE,
    "caie": CAMBRIDGE,
    "alevel": CAMBRIDGE,
    "aslevel": CAMBRIDGE,
    "stateboard": STATE_BOARD,
    "state": STATE_BOARD,
    "hbse": STATE_BOARD,
    "upboard": STATE_BOARD,
    "mahboard": STATE_BOARD,
    "ssc": STATE_BOARD,
    "nios": NIOS,
    "openschool": NIOS,
}

#: Boards whose syllabus differs enough that a mismatch is a real problem in
#: exam years, but is survivable in early grades (see `board_is_mandatory`).
_STRICT_BOARDS: frozenset[str] = frozenset({IB, IGCSE, CAMBRIDGE, ISC})

# -------------------------------------------------------------------- exams
JEE = "JEE"
NEET = "NEET"
BOARD_EXAM = "Board Exam"
OLYMPIAD = "Olympiad"
NTSE = "NTSE"
CUET = "CUET"
SAT = "SAT"

_EXAM_ALIASES: dict[str, str] = {
    "jee": JEE,
    "jeemain": JEE,
    "jeeadvanced": JEE,
    "iitjee": JEE,
    "iit": JEE,
    "neet": NEET,
    "neetug": NEET,
    "medical": NEET,
    "aiims": NEET,
    "boards": BOARD_EXAM,
    "boardexam": BOARD_EXAM,
    "boardexams": BOARD_EXAM,
    "olympiad": OLYMPIAD,
    "imo": OLYMPIAD,
    "nso": OLYMPIAD,
    "ntse": NTSE,
    "cuet": CUET,
    "sat": SAT,
}

#: Competitive exams are subject-gated: a NEET aspirant needs Bio/Chem/Physics,
#: a JEE aspirant needs Maths/Physics/Chemistry.
EXAM_SUBJECTS: dict[str, frozenset[str]] = {
    JEE: frozenset({"Mathematics", "Physics", "Chemistry"}),
    NEET: frozenset({"Biology", "Physics", "Chemistry"}),
}

# ------------------------------------------------------------------ classes
_PRESCHOOL: dict[str, str] = {
    "nursery": "Nursery",
    "prenursery": "Nursery",
    "playgroup": "Nursery",
    "lkg": "LKG",
    "ukg": "UKG",
    "kg": "KG",
    "kindergarten": "KG",
}
_ROMAN: dict[str, int] = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
    "xi": 11,
    "xii": 12,
}
_CLASS_NUMBER = re.compile(r"\b(?:class|std|standard|grade|clas)?\s*(\d{1,2})(?:st|nd|rd|th)?\b")
_CLASS_WORD = r"(?:class|std|standard|grade|clas)"
#: Longest-first so `vii` cannot be shortened to `vi`.
_ROMAN_ALT = "|".join(sorted(_ROMAN, key=len, reverse=True))
#: Separators the website actually puts between the word and the numeral.
#: `category.cat_title` stores "Class - XI", "Class- XII", "Class-II" — 1,297 of
#: 1,344 tutor-course rows use the hyphenated form. Matching only whitespace
#: here meant `normalize_class("Class - XI")` fell through to the title-case
#: fallback and returned the string "Class - Xi", which matches no requirement
#: at all: class filtering was silently broken for ~96% of the tutor base.
_CLASS_SEP = r"[\s\-‐-―:.]*"
#: A Roman numeral counts as a grade only when a class word sits next to it.
#: A bare token cannot: `i` is the commonest English word in this inbox, so
#: "hi i need a tutor" was parsing as Class 1 and hard-filtering the pool.
_CLASS_ROMAN = re.compile(
    rf"\b{_CLASS_WORD}{_CLASS_SEP}({_ROMAN_ALT})\b"
    rf"|\b({_ROMAN_ALT})\s*(?:th)?{_CLASS_SEP}{_CLASS_WORD}\b"
)


class AcademicLevel(StrEnum):
    """Coarse bands that drive pedagogy expectations, not exact grade equality."""

    EARLY_YEARS = "early_years"  # Nursery..Class 2
    PRIMARY = "primary"  # Class 3..5
    MIDDLE = "middle"  # Class 6..8
    SECONDARY = "secondary"  # Class 9..10
    SENIOR_SECONDARY = "senior_secondary"  # Class 11..12
    UNKNOWN = "unknown"


def normalize_board(board: str | None) -> str | None:
    if not board or not board.strip():
        return None
    return _BOARD_ALIASES.get(normalize_key(board)) or board.strip().upper()


def extract_board(text: str) -> str | None:
    for token in tokens(text):
        canonical = _BOARD_ALIASES.get(token)
        if canonical:
            return canonical
    return None


def board_is_mandatory(board: str | None, class_number: int | None) -> bool:
    """Whether a board mismatch should hard-filter rather than merely score down.

    International boards diverge from CBSE/ICSE at every level, so they are
    always mandatory. Indian boards only diverge enough to matter from Class 9,
    when the syllabus and the exam pattern become board-specific — filtering a
    Class 4 parent's pool on CBSE-vs-state would delete good tutors for no reason.
    """
    canonical = normalize_board(board)
    if canonical is None:
        return False
    if canonical in _STRICT_BOARDS:
        return True
    return class_number is not None and class_number >= 9


def normalize_class(value: str | None) -> str | None:
    """`'10th'`, `'class X'`, `'std 10'` -> `'Class 10'`; `'lkg'` -> `'LKG'`."""
    if not value or not value.strip():
        return None
    key = normalize_key(value)
    if key in _PRESCHOOL:
        return _PRESCHOOL[key]
    # A whole field that *is* a numeral ("X" from a website record) is a grade;
    # a numeral loose in a sentence is not — see `_CLASS_ROMAN`.
    if key.replace(" ", "") in _ROMAN:
        return f"Class {_ROMAN[key.replace(' ', '')]}"

    # A range stays a range. "Class 11-12" and "6-12" are the website's way of
    # saying a tutor covers several grades, and collapsing them to their first
    # value (which is what returning `Class 11` did) silently narrowed every
    # such tutor to one grade — a tutor who teaches 6 to 12 stopped matching
    # Class 7 through 12. `teaches_class` already understands range labels; it
    # just never got one.
    low, high = _class_range(value)
    if low is not None and high is not None and low != high:
        return f"Class {low}-{high}"

    number = class_number(value)
    if number is not None:
        return f"Class {number}"
    return " ".join(word.capitalize() for word in value.split())


def class_number(value: str | None) -> int | None:
    """Numeric grade, or None. Digits win over Roman numerals when both appear."""
    if not value or not value.strip():
        return None
    text = value.strip().lower()
    match = _CLASS_NUMBER.search(text)
    if match:
        number = int(match.group(1))
        if 1 <= number <= 12:
            return number
    roman = _CLASS_ROMAN.search(text)
    if roman:
        return _ROMAN[roman.group(1) or roman.group(2)]
    return None


def extract_class(text: str) -> str | None:
    lowered = " ".join(tokens(text))
    for key, label in _PRESCHOOL.items():
        if key in lowered.replace(" ", ""):
            return label
    number = class_number(lowered)
    return f"Class {number}" if number is not None else None


def normalize_exam(exam: str | None) -> str | None:
    if not exam or not exam.strip():
        return None
    return _EXAM_ALIASES.get(normalize_key(exam)) or exam.strip().upper()


def extract_exam(text: str) -> str | None:
    for token in tokens(text):
        canonical = _EXAM_ALIASES.get(token)
        if canonical:
            return canonical
    return None


def level_for(class_label: str | None) -> AcademicLevel:
    if not class_label:
        return AcademicLevel.UNKNOWN
    key = normalize_key(class_label)
    if key in _PRESCHOOL or key in {"nursery", "lkg", "ukg", "kg"}:
        return AcademicLevel.EARLY_YEARS
    number = class_number(class_label)
    if number is None:
        return AcademicLevel.UNKNOWN
    if number <= 2:
        return AcademicLevel.EARLY_YEARS
    if number <= 5:
        return AcademicLevel.PRIMARY
    if number <= 8:
        return AcademicLevel.MIDDLE
    if number <= 10:
        return AcademicLevel.SECONDARY
    return AcademicLevel.SENIOR_SECONDARY


def teaches_class(required: str | None, taught: list[str] | tuple[str, ...]) -> bool:
    """Whether a tutor's declared class list covers the required class.

    Handles the two shapes the website actually stores: discrete labels
    (`"Class 10"`) and ranges (`"Class 6-10"`, `"6 to 10"`).
    """
    want = class_number(required)
    want_label = normalize_class(required)
    if want_label is None:
        return True  # No requirement stated: nothing to violate.
    for entry in taught:
        if normalize_class(entry) == want_label:
            return True
        if want is not None:
            low, high = _class_range(entry)
            if low is not None and high is not None and low <= want <= high:
                return True
    return False


def _class_range(entry: str) -> tuple[int | None, int | None]:
    numbers = [int(n) for n in re.findall(r"\d{1,2}", entry) if 1 <= int(n) <= 12]
    if len(numbers) >= 2:
        return min(numbers), max(numbers)
    return (None, None)
