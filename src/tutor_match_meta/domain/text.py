"""Text normalisation shared by every domain normaliser.

`slugify` is a faithful port of Laravel's `Str::slug()` for the ASCII cases the
NXTutors data actually contains. It has to match, because the canonical tutor
profile URL is built from it — a divergence here produces a 404 for a real tutor.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]*>")

#: Devanagari and common Indic ranges. Present so Hinglish input is folded for
#: keyword lookup rather than silently dropped.
_TRANSLITERATIONS = {
    "क्लास": "class",
    "कक्षा": "class",
    "गणित": "maths",
    "विज्ञान": "science",
    "अंग्रेजी": "english",
    "हिंदी": "hindi",
    "भौतिक": "physics",
    "रसायन": "chemistry",
    "जीव": "biology",
    "ट्यूटर": "tutor",
    "शिक्षक": "teacher",
    "ऑनलाइन": "online",
    "घर": "home",
}


def strip_html(value: str) -> str:
    return _HTML_TAG.sub(" ", value)


def collapse_whitespace(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def clean(value: str | None) -> str:
    """Website free-text → safe single-line text. Mirrors the Laravel mapper."""
    if not value:
        return ""
    return collapse_whitespace(strip_html(value))


def ascii_fold(value: str) -> str:
    """Drop accents. 'Gurugrām' -> 'Gurugram'."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def slugify(value: str, fallback: str = "") -> str:
    """Laravel `Str::slug()` equivalent for ASCII-foldable input.

    Returns `fallback` when the input slugifies to nothing, matching the
    website's `Str::slug($city) ?: 'india'` idiom at the call sites.
    """
    slug = _NON_ALNUM.sub("-", ascii_fold(value).lower()).strip("-")
    return slug or fallback


def normalize_key(value: str) -> str:
    """Aggressive fold for dictionary lookup: lowercase, alnum only, no spaces.

    'Maths ', 'maths', 'MATHS', 'Math-s' all collapse to 'maths'.
    """
    return re.sub(r"[^a-z0-9]", "", ascii_fold(value).lower())


def transliterate_hinglish(text: str) -> str:
    """Replace known Devanagari terms with their romanised equivalents.

    Deliberately a small explicit table rather than a transliteration library:
    the vocabulary that matters here is a few dozen tutoring terms, and a
    dependency that guesses would introduce noise the matcher cannot audit.
    """
    out = text
    for source, target in _TRANSLITERATIONS.items():
        out = out.replace(source, target)
    return out


def tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, Hinglish-folded."""
    folded = ascii_fold(transliterate_hinglish(text)).lower()
    return [t for t in re.split(r"[^a-z0-9+#]+", folded) if t]
