"""Tuition mode normalisation.

The website encodes mode across three free-text columns that disagree with each
other: `register.class_type`, `teacher_courses.class_type` and
`teacher_courses.mode`. The Laravel mapper unions all three; so does this.

Unknown is never treated as "no". A tutor whose mode columns are blank is
mode-agnostic, not mode-incapable — filtering them out would delete a large
slice of the tutor base for a data-entry gap.
"""

from __future__ import annotations

from tutor_match_meta.contracts.common import TuitionMode
from tutor_match_meta.domain.text import normalize_key, tokens

_ONLINE_TERMS = ("online", "remote", "virtual", "zoom", "video", "skype", "digital")
_HOME_TERMS = ("home", "offline", "inperson", "person", "academic", "doorstep", "athome", "ghar")
_BOTH_TERMS = ("both", "hybrid", "any", "either", "flexible")


def parse_mode_tokens(raw: str | None) -> tuple[TuitionMode, ...]:
    """Modes implied by one free-text column value.

    Mirrors `PublicTutorFieldMapper::modeTokens()`: "both" short-circuits to
    online+home rather than falling through the substring checks.
    """
    if not raw:
        return ()
    key = normalize_key(raw)
    if any(term in key for term in _BOTH_TERMS):
        return (TuitionMode.ONLINE, TuitionMode.HOME)
    modes: list[TuitionMode] = []
    if any(term in key for term in _ONLINE_TERMS):
        modes.append(TuitionMode.ONLINE)
    if any(term in key for term in _HOME_TERMS):
        modes.append(TuitionMode.HOME)
    return tuple(modes)


def union_modes(*raw_values: str | None) -> tuple[TuitionMode, ...]:
    """Union across every mode-bearing column, order-stable and deduplicated."""
    seen: dict[TuitionMode, None] = {}
    for value in raw_values:
        for mode in parse_mode_tokens(value):
            seen.setdefault(mode, None)
    return tuple(seen)


def normalize_mode(value: str | None) -> TuitionMode | None:
    """A single requested mode. `'both'` becomes HYBRID on the request side."""
    if not value:
        return None
    key = normalize_key(value)
    if any(term in key for term in _BOTH_TERMS):
        return TuitionMode.HYBRID
    modes = parse_mode_tokens(value)
    if len(modes) == 1:
        return modes[0]
    if len(modes) > 1:
        return TuitionMode.HYBRID
    return None


def extract_mode(text: str) -> TuitionMode | None:
    """Mode requested in a free-text message.

    Home wins a tie: "home tuition or online is also fine" states a preference
    with a fallback, and the preference is what should drive the filter.
    """
    joined = "".join(tokens(text))
    wants_home = any(term in joined for term in _HOME_TERMS)
    wants_online = any(term in joined for term in _ONLINE_TERMS)
    if any(term in joined for term in _BOTH_TERMS) or (wants_home and wants_online):
        return TuitionMode.HYBRID if not wants_home else TuitionMode.HOME
    if wants_home:
        return TuitionMode.HOME
    if wants_online:
        return TuitionMode.ONLINE
    return None


def mode_label(mode: TuitionMode) -> str:
    return {
        TuitionMode.HOME: "Home tuition",
        TuitionMode.ONLINE: "Online",
        TuitionMode.HYBRID: "Home or online",
    }[mode]
