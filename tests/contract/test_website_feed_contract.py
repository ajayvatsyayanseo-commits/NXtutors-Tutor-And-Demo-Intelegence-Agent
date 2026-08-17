"""The contract between this agent and the NXTutors website.

Two codebases, two languages, two deploy pipelines, one wire format. Nothing
here talks to the website — it pins the agreement so a change on either side
fails a test instead of failing a sync at 3am.

The website side lives at:

    Nxtutors Website/public/routes/api.php
    Nxtutors Website/public/app/Http/Middleware/VerifyAgentSignature.php
    Nxtutors Website/public/app/Http/Controllers/Api/AgentTutorFeedController.php

Three things must agree, and each is asserted below:

1. **the public ref** — `base64url("{user_id}-nxt")`, no padding. It is what
   every profile link is built from, so a mismatch means every link 404s;
2. **the canonical string** — `METHOD\\nPATH?QUERY\\nTIMESTAMP\\nsha256(body)`.
   The PHP middleware rebuilds it from `getRequestUri()`, which is why the query
   string is part of the signed path;
3. **the JSON shape** — what the controller emits must validate against
   `FeedTutor`, including the fields that are legitimately absent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from tutor_match_meta.domain.identity import decode_public_ref, encode_public_ref
from tutor_match_meta.integrations.website.tutor_feed import (
    FEED_FIELDS,
    FEED_PATH,
    FeedPage,
    FeedTutor,
    _to_candidate,
)
from tutor_match_meta.security.signing import (
    SIGNATURE_PREFIX,
    SignedRequest,
    canonical_string,
    sign,
)

pytestmark = pytest.mark.contract

WEBSITE = Path("E:/Nx Tutor Lead Intake Agent/Ready In Production Agents/Nxtutors Website/public")
CONTROLLER = WEBSITE / "app/Http/Controllers/Api/AgentTutorFeedController.php"
MIDDLEWARE = WEBSITE / "app/Http/Middleware/VerifyAgentSignature.php"
MAPPER = WEBSITE / "app/NxtAi/Support/PublicTutorFieldMapper.php"

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

website_present = pytest.mark.skipif(
    not CONTROLLER.exists(),
    reason=(
        "the website checkout is not on this machine. These assertions pin a "
        "cross-repository contract and are NOT evidence of health when skipped."
    ),
)


class TestPublicRef:
    """`publicToken()` in PHP and `encode_public_ref()` in Python."""

    @pytest.mark.parametrize("user_id", ["1016", "2938", "NXT10001", "10"])
    def test_the_two_implementations_agree(self, user_id: str) -> None:
        php = (
            base64.b64encode(f"{user_id}-nxt".encode())
            .decode()
            .replace("+", "-")
            .replace("/", "_")
            .rstrip("=")
        )
        assert encode_public_ref(user_id) == php

    @pytest.mark.parametrize("user_id", ["1016", "2938", "NXT10001"])
    def test_a_ref_round_trips(self, user_id: str) -> None:
        assert decode_public_ref(encode_public_ref(user_id)) == user_id

    def test_the_ref_carries_the_nxt_suffix(self) -> None:
        """The website's tutor route decodes this and expects the suffix; a ref
        minted without it resolves to no tutor."""
        decoded = base64.urlsafe_b64decode(encode_public_ref("1016") + "==").decode()
        assert decoded == "1016-nxt"


class TestSignature:
    """A PHP re-implementation of the canonical string, asserted equal."""

    SECRET = "shared-feed-secret"

    def php_equivalent(self, method: str, path: str, timestamp: int, body: bytes) -> str:
        # Mirrors VerifyAgentSignature::handle() line for line.
        canonical = "\n".join(
            [method.upper(), path, str(timestamp), hashlib.sha256(body).hexdigest()]
        )
        digest = hmac.new(self.SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        return f"v1={digest}"

    def test_a_get_with_a_query_string_verifies(self) -> None:
        path = f"{FEED_PATH}?limit=100&offset=0"
        stamp = 1_700_000_000
        produced = sign(self.SECRET, SignedRequest("GET", path, stamp, b""))
        assert produced == self.php_equivalent("GET", path, stamp, b"")

    def test_the_query_string_is_part_of_what_is_signed(self) -> None:
        """Otherwise a signature captured for page 1 replays against any page."""
        stamp = 1_700_000_000
        first = sign(self.SECRET, SignedRequest("GET", f"{FEED_PATH}?offset=0", stamp, b""))
        second = sign(self.SECRET, SignedRequest("GET", f"{FEED_PATH}?offset=500", stamp, b""))
        assert first != second

    def test_the_method_is_part_of_what_is_signed(self) -> None:
        stamp = 1_700_000_000
        assert sign(self.SECRET, SignedRequest("GET", FEED_PATH, stamp, b"")) != sign(
            self.SECRET, SignedRequest("POST", FEED_PATH, stamp, b"")
        )

    def test_the_prefix_is_the_one_the_middleware_checks(self) -> None:
        assert SIGNATURE_PREFIX == "v1="

    def test_the_canonical_string_has_exactly_four_lines(self) -> None:
        assert canonical_string("GET", FEED_PATH, 1, b"").count("\n") == 3


class TestTheShapeTheControllerEmits:
    """A record built to the controller's field list must validate here."""

    #: Exactly the keys AgentTutorFeedController::toFeedRecord() returns.
    RECORD: ClassVar[dict[str, object]] = {
        "user_id": "1016",
        "name": "Rajesh mahera",
        "gender": "Male",
        "avatar": "https://www.nxtutors.com/storage/user/a.jpg",
        "city": "Gurugram",
        "locality": None,
        "district": "Gurugram",
        "state": "Haryana",
        "pincode": "122001",
        "experience": "9 years",
        "education": "M.Sc Mathematics",
        "profile_summary": "Teaches CBSE senior secondary.",
        "budget": "600 / hour",
        "subjects": [],
        "boards": ["CBSE"],
        "classes": ["Class - XI"],
        "modes": ["Home"],
        "reviews": {"count": 0},
        "availability": None,
        "updated_at": None,
    }

    def test_the_record_validates(self) -> None:
        assert FeedTutor.model_validate(self.RECORD).user_id == "1016"

    def test_every_emitted_key_is_on_the_allowlist(self) -> None:
        assert set(self.RECORD) <= FEED_FIELDS

    def test_a_page_of_them_validates(self) -> None:
        page = FeedPage.model_validate({"tutors": [self.RECORD], "has_more": True})
        assert page.has_more and len(page.tutors) == 1

    def test_the_websites_real_class_label_maps_to_a_grade(self) -> None:
        """`Class - XI` is what `category.cat_title` actually stores, for 1,297
        of 1,344 tutor-course rows."""
        candidate = _to_candidate(FeedTutor.model_validate(self.RECORD), NOW)
        assert candidate.capabilities.classes == ("Class 11",)

    def test_a_record_with_no_reviews_is_not_rated(self) -> None:
        candidate = _to_candidate(FeedTutor.model_validate(self.RECORD), NOW)
        assert candidate.reviews.count == 0
        assert candidate.reviews.rating_avg is None

    def test_the_ref_matches_what_the_website_would_mint(self) -> None:
        candidate = _to_candidate(FeedTutor.model_validate(self.RECORD), NOW)
        assert candidate.public_ref == encode_public_ref("1016")


@website_present
class TestTheWebsiteSourceStillAgrees:
    """Read the PHP. If someone edits it, these fail rather than the sync."""

    def test_the_middleware_signs_the_same_four_parts(self) -> None:
        php = MIDDLEWARE.read_text(encoding="utf-8")
        for part in ("getMethod()", "getRequestUri()", "getContent()", "sha256"):
            assert part in php, f"middleware no longer uses {part}"
        assert "hash_equals" in php, "signature comparison must stay constant-time"
        assert "'v1='" in php or '"v1="' in php

    def test_the_route_is_get_only(self) -> None:
        routes = (WEBSITE / "routes/api.php").read_text(encoding="utf-8")
        assert "Route::get" in routes
        for verb in ("Route::post", "Route::put", "Route::patch", "Route::delete"):
            assert verb not in routes, f"the agent feed must never expose {verb}"

    def test_the_controller_emits_the_keys_this_test_pins(self) -> None:
        php = CONTROLLER.read_text(encoding="utf-8")
        for key in TestTheShapeTheControllerEmits.RECORD:
            assert f"'{key}' =>" in php, f"controller no longer emits {key!r}"

    def test_no_private_column_is_emitted(self) -> None:
        """The mapper's own PRIVATE_COLUMNS list, enforced against the feed."""
        mapper = MAPPER.read_text(encoding="utf-8")
        private = re.search(r"PRIVATE_COLUMNS\s*=\s*\[(.*?)\];", mapper, re.S)
        assert private, "PublicTutorFieldMapper::PRIVATE_COLUMNS not found"
        columns = set(re.findall(r"'([a-z_]+)'", private.group(1)))
        assert columns, "no private columns parsed"

        php = CONTROLLER.read_text(encoding="utf-8")
        leaked = sorted(c for c in columns if f"'{c}' =>" in php)
        assert leaked == [], f"the feed emits private columns: {leaked}"
        assert not columns & FEED_FIELDS, "a private column is on the agent allowlist"

    def test_the_controller_reads_both_course_schemas(self) -> None:
        """1,269 tutors appear only in the id-schema and 24 only in the
        string-schema. Eager-loading one drops most of the tutor base."""
        php = CONTROLLER.read_text(encoding="utf-8")
        assert "'courses" in php, "id-schema (teacher_course_managment) not eager-loaded"
        assert "'coursess'" in php, "string-schema (teacher_courses) not eager-loaded"

    def test_the_controller_filters_to_active_tutors(self) -> None:
        php = CONTROLLER.read_text(encoding="utf-8")
        assert "'join_as', 'teacher'" in php
        assert "'status', 't'" in php
