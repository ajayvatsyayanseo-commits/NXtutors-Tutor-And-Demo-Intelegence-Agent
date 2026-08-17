"""Tutor identity and canonical profile links.

The encoding here is not a design choice — it is reverse-engineered from the live
site and must stay byte-identical:

`resources/views/tutor/partials/cards.blade.php`
    rtrim(strtr(base64_encode($user_id . '-nxt'), '+/', '-_'), '=')

`HomeController::showsingletutornew()`
    re-pads, base64-decodes, and 404s unless the result ends in '-nxt', then
    looks up `register` where join_as='teacher' AND status='t'.

So a link is only valid for an ACTIVE tutor. `TutorProfileLinkResolver` refuses
to emit one otherwise — a broken link in a WhatsApp shortlist is worse than
omitting the tutor.
"""

from __future__ import annotations

import base64
import binascii

from tutor_match_meta.contracts.common import RECOMMENDABLE_FRESHNESS
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain.text import slugify

_SUFFIX = "-nxt"


class UnresolvableProfileLink(Exception):
    """Raised rather than guessing a URL. Never caught into a fabricated slug."""


def encode_public_ref(user_id: str) -> str:
    """`register.user_id` -> the site's URL-safe base64 token."""
    if not user_id:
        raise ValueError("user_id is required to build a public ref")
    raw = base64.b64encode(f"{user_id}{_SUFFIX}".encode())
    return raw.decode("ascii").replace("+", "-").replace("/", "_").rstrip("=")


def decode_public_ref(public_ref: str) -> str | None:
    """Reverse of `encode_public_ref`. Returns None for anything malformed.

    Mirrors the controller's validation exactly, including the `-nxt` suffix
    check, so a ref this function accepts is a ref the website will accept.
    """
    ref = public_ref.strip()
    if not ref or not all(c.isalnum() or c in "-_" for c in ref):
        return None
    padded = ref.replace("-", "+").replace("_", "/")
    padded += "=" * ((4 - len(padded) % 4) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not decoded.endswith(_SUFFIX):
        return None
    user_id = decoded[: -len(_SUFFIX)]
    return user_id or None


class TutorProfileLinkResolver:
    """Builds canonical profile URLs, and refuses when it cannot be sure."""

    def __init__(self, public_base_url: str) -> None:
        self._base = public_base_url.rstrip("/")

    def resolve(self, tutor: TutorCandidate) -> str:
        """Canonical absolute profile URL for an active, fresh-enough tutor.

        Raises `UnresolvableProfileLink` rather than returning a guess when the
        projection is too stale to assert the tutor is still active — a stale
        row can describe an account that was deactivated yesterday, and that URL
        is a hard 404.
        """
        if tutor.freshness not in RECOMMENDABLE_FRESHNESS:
            raise UnresolvableProfileLink(
                f"tutor {tutor.tutor_id} projection is {tutor.freshness}; "
                "refusing to publish a link that may 404"
            )
        expected = encode_public_ref(tutor.tutor_id)
        if tutor.public_ref != expected:
            raise UnresolvableProfileLink(
                f"tutor {tutor.tutor_id} public_ref does not match its user_id"
            )
        city_slug = slugify(tutor.city or "", fallback="india")
        name_slug = slugify(tutor.name or "", fallback="tutor")
        return f"{self._base}/tutor/{city_slug}/{tutor.public_ref}/{name_slug}"

    def try_resolve(self, tutor: TutorCandidate) -> str | None:
        try:
            return self.resolve(tutor)
        except (UnresolvableProfileLink, ValueError):
            return None
