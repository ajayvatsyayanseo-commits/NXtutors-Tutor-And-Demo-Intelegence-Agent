"""The signed HTTPS tutor feed that replaced the MySQL adapter.

The feed is the only way tutor facts enter this service, so these tests care
about three things above all: that the privacy allowlist actually holds, that
malformed upstream data cannot reach the projection, and that a transport
failure fails the page rather than half-writing it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from tutor_match_meta.contracts.common import TuitionMode
from tutor_match_meta.integrations.website.tutor_feed import (
    FEED_FIELDS,
    FEED_PATH,
    MAX_RESPONSE_BYTES,
    FeedTutor,
    FeedUnavailable,
    WebsiteTutorFeed,
    build_feed,
)
from tutor_match_meta.security.signing import SignedRequest, canonical_string
from tutor_match_meta.security.urls import UrlPolicy

HOST = "api.nxtutors.test"
BASE = f"https://{HOST}"
KEY = "feed-signing-key"
#: `resolve_dns=False` because the test host is not real. The SSRF defence
#: itself is covered in tests/security; here we only need the allowlist.
POLICY = UrlPolicy(allowed_hosts=frozenset({HOST}), resolve_dns=False)
NOW = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

RECORD = {
    "user_id": "NXT10001",
    "name": "Anita Sharma",
    "gender": "female",
    "city": "Gurugram",
    "locality": "Sector 57",
    "pincode": "122003",
    "experience": "8 years",
    "education": "M.Sc Mathematics",
    "profile_summary": "Teaches CBSE maths.",
    "budget": "800-1000",
    "subjects": ["maths"],
    "boards": ["cbse"],
    "classes": ["10"],
    "modes": ["home"],
    "reviews": {"count": 17, "rating_avg": 4.6, "latest_review_at": "2026-02-01T10:00:00Z"},
    "updated_at": "2026-02-20T12:00:00Z",
}


def feed_with(handler) -> WebsiteTutorFeed:
    return WebsiteTutorFeed(
        base_url=BASE,
        signing_key=KEY,
        url_policy=POLICY,
        page_size=2,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def ok(payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


class TestMapping:
    async def test_a_record_becomes_a_normalised_candidate(self) -> None:
        feed = feed_with(ok({"tutors": [RECORD], "has_more": False}))
        page = await feed.fetch_page(offset=0, now=NOW)

        assert page.fetched == 1
        assert not page.has_more
        tutor = page.tutors[0]
        assert tutor.tutor_id == "NXT10001"
        assert tutor.name == "Anita Sharma"
        # Normalisation is ours: the feed says "maths"/"cbse"/"10".
        assert tutor.capabilities.subjects == ("Mathematics",)
        assert tutor.capabilities.boards == ("CBSE",)
        assert tutor.capabilities.classes == ("Class 10",)
        assert TuitionMode.HOME in tutor.capabilities.modes
        assert tutor.fee.minimum == 800
        assert tutor.reviews.count == 17
        assert tutor.reviews.rating_avg == 4.6
        assert tutor.reviews.latest_review_at is not None
        assert tutor.public_ref and tutor.public_ref != tutor.tutor_id

    async def test_a_six_digit_pincode_is_required(self) -> None:
        feed = feed_with(ok({"tutors": [{**RECORD, "pincode": "12"}], "has_more": False}))
        page = await feed.fetch_page(offset=0, now=NOW)
        assert page.tutors[0].pincode is None

    async def test_source_updated_at_falls_back_to_the_sync_stamp(self) -> None:
        record = {k: v for k, v in RECORD.items() if k != "updated_at"}
        feed = feed_with(ok({"tutors": [record], "has_more": False}))
        page = await feed.fetch_page(offset=0, now=NOW)
        assert page.tutors[0].source_updated_at == NOW


class TestPrivacyAllowlist:
    def test_the_allowlist_and_the_model_agree(self) -> None:
        """If they drift, the security test asserting FEED_FIELDS is disjoint
        from the forbidden set stops proving anything about the real model."""
        assert set(FeedTutor.model_fields) <= FEED_FIELDS

    def test_a_private_field_published_by_the_website_is_dropped(self) -> None:
        record = FeedTutor.model_validate(
            {**RECORD, "phone": "+919876543210", "email": "a@b.c", "address": "House 4"}
        )
        dumped = record.model_dump()
        assert "phone" not in dumped
        assert "email" not in dumped
        assert "address" not in dumped

    async def test_a_private_field_never_reaches_the_candidate(self) -> None:
        feed = feed_with(ok({"tutors": [{**RECORD, "phone": "+919876543210"}], "has_more": False}))
        page = await feed.fetch_page(offset=0, now=NOW)
        assert "9876543210" not in page.tutors[0].model_dump_json()


class TestMalformedUpstream:
    async def test_a_record_missing_its_id_fails_the_page(self) -> None:
        """Half a page is worse than no page: the checkpoint would advance past
        the records that failed and they would never be retried."""
        bad = {k: v for k, v in RECORD.items() if k != "user_id"}
        feed = feed_with(ok({"tutors": [RECORD, bad], "has_more": False}))
        with pytest.raises(FeedUnavailable, match="malformed"):
            await feed.fetch_page(offset=0, now=NOW)

    async def test_an_out_of_range_rating_fails_the_page(self) -> None:
        record = {**RECORD, "reviews": {"count": 1, "rating_avg": 9.9}}
        feed = feed_with(ok({"tutors": [record], "has_more": False}))
        with pytest.raises(FeedUnavailable):
            await feed.fetch_page(offset=0, now=NOW)

    async def test_a_non_json_body_is_refused(self) -> None:
        feed = feed_with(lambda request: httpx.Response(200, text="<html>oops</html>"))
        with pytest.raises(FeedUnavailable, match="non-JSON"):
            await feed.fetch_page(offset=0, now=NOW)

    async def test_an_oversized_body_is_refused_before_parsing(self) -> None:
        giant = httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
        feed = feed_with(lambda request: giant)
        with pytest.raises(FeedUnavailable, match="size ceiling"):
            await feed.fetch_page(offset=0, now=NOW)

    async def test_has_more_is_ignored_when_the_page_is_empty(self) -> None:
        """A server that always says `has_more` with no rows would spin the
        sync loop until the Lambda timed out."""
        feed = feed_with(ok({"tutors": [], "has_more": True}))
        page = await feed.fetch_page(offset=0, now=NOW)
        assert not page.has_more


class TestTransport:
    async def test_every_request_is_signed_over_method_path_and_timestamp(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.headers))
            seen["_path"] = request.url.raw_path.decode()
            return httpx.Response(200, json={"tutors": [], "has_more": False})

        await feed_with(handler).fetch_page(offset=40, now=NOW)

        assert seen["x-nxt-signature"].startswith("v1=")
        # The signature must cover the *exact* path including the query, or a
        # captured signature could be replayed against a different page.
        signed_path = f"{FEED_PATH}?limit=2&offset=40"
        assert seen["_path"] == signed_path
        expected = canonical_string("GET", signed_path, int(seen["x-nxt-timestamp"]), b"")
        assert expected.startswith("GET\n" + signed_path)

    async def test_a_4xx_is_not_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(403, json={})

        with pytest.raises(FeedUnavailable, match="403"):
            await feed_with(handler).fetch_page(offset=0, now=NOW)
        assert calls["n"] == 1, "a rejected credential must not burn the retry budget"

    async def test_a_5xx_is_retried_then_gives_up(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, json={})

        with pytest.raises(FeedUnavailable, match="upstream_503"):
            await feed_with(handler).fetch_page(offset=0, now=NOW)
        assert calls["n"] == 3  # initial + 2 retries

    async def test_a_transport_error_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"tutors": [RECORD], "has_more": False})

        page = await feed_with(handler).fetch_page(offset=0, now=NOW)
        assert page.fetched == 1

    async def test_a_host_outside_the_policy_is_refused(self) -> None:
        feed = WebsiteTutorFeed(
            base_url="https://evil.example.com",
            signing_key=KEY,
            url_policy=POLICY,
            client=httpx.AsyncClient(transport=httpx.MockTransport(ok({"tutors": []}))),
        )
        with pytest.raises(Exception, match="(?i)host|allow"):
            await feed.fetch_page(offset=0, now=NOW)


class TestConstruction:
    def test_an_unconfigured_feed_is_none_not_an_error(self) -> None:
        """A scheduled run with no feed configured reports a skip; it must not
        crash the whole scheduled Lambda."""
        assert build_feed(base_url="", signing_key=KEY, url_policy=POLICY) is None
        assert build_feed(base_url=BASE, signing_key="", url_policy=POLICY) is None

    def test_a_configured_feed_is_built(self) -> None:
        assert build_feed(base_url=BASE, signing_key=KEY, url_policy=POLICY) is not None

    @pytest.mark.parametrize("size", [0, 501])
    def test_an_absurd_page_size_is_refused(self, size: int) -> None:
        with pytest.raises(ValueError, match="page_size"):
            WebsiteTutorFeed(base_url=BASE, signing_key=KEY, url_policy=POLICY, page_size=size)


class TestNoMySQLSurvives:
    def test_the_mysql_adapter_is_gone(self) -> None:
        """Its presence would mean a second database engine, a second credential
        and a NAT-requiring network path had crept back in."""
        with pytest.raises(ModuleNotFoundError):
            __import__("tutor_match_meta.repositories.mysql_tutor")

    def test_aiomysql_is_not_a_dependency(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        assert "aiomysql" not in (root / "pyproject.toml").read_text(encoding="utf-8")

    def test_no_mysql_setting_exists(self) -> None:
        from tutor_match_meta.config.settings import Settings

        assert not [n for n in Settings.model_fields if "mysql" in n.lower()]


def test_signed_request_covers_an_empty_body() -> None:
    """GET has no body; the signature must still be well-defined over one."""
    assert canonical_string("GET", FEED_PATH, 1, b"") == canonical_string(
        "GET", FEED_PATH, 1, SignedRequest("GET", FEED_PATH, 1, b"").body
    )
