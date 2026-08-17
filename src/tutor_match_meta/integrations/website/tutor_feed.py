"""Signed HTTPS tutor feed — the only way tutor data enters this service.

Replaces the former read-only MySQL adapter. MySQL is gone from this
architecture entirely, for two independent reasons:

* **One database.** PostgreSQL is the single application store. A second engine
  meant a second set of credentials, a second connection-limit budget, a second
  failure mode, and an adapter carrying production DDL quirks (mixed collations,
  ratings stored as varchar, capabilities split across two live schemas) that
  belonged to the website, not to us.
* **The network boundary.** Reaching the website's database from Lambda meant
  either a NAT Gateway or a peered private path. Both are forbidden here. An
  HTTPS call is made from a function that lives *outside* the VPC, which is
  exactly where a function that talks to the public internet belongs.

So the website's own service layer does the joining and we consume JSON. The
website owns its schema; we own normalisation and the privacy allowlist.

`FEED_FIELDS` is that allowlist, and it is enforced on the way in: a field the
website adds later — a phone number, an email, a document id — is dropped here
rather than silently projected into `tutor_projection`. `tests/security` asserts
the forbidden set is disjoint from it.

This module makes no database calls. It runs in the Internet-side feed Lambda,
which publishes pages to SQS for the VPC-side ingest worker to persist.
"""

from __future__ import annotations

import time as clock
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tutor_match_meta.contracts.common import Freshness
from tutor_match_meta.contracts.schedule import TimeWindow, Weekday, WeeklySchedule
from tutor_match_meta.contracts.tutor import (
    ReviewAggregate,
    TutorCandidate,
    TutorCapabilities,
)
from tutor_match_meta.domain import academics, fees, modes, subjects
from tutor_match_meta.domain.identity import encode_public_ref
from tutor_match_meta.domain.text import clean
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.security.signing import SignedRequest, sign
from tutor_match_meta.security.urls import UrlPolicy, validate

logger = get_logger("website.tutor_feed")

FEED_PATH = "/internal/agent/tutors"

#: The only fields we read off a feed record. Adding one is a reviewable privacy
#: decision, exactly as the old SQL column allowlist was.
FEED_FIELDS: frozenset[str] = frozenset(
    {
        "user_id",
        "name",
        "gender",
        "avatar",
        "city",
        "locality",
        "district",
        "state",
        "pincode",
        "experience",
        "education",
        "profile_summary",
        "budget",
        "subjects",
        "boards",
        "classes",
        "modes",
        "reviews",
        "availability",
        "updated_at",
    }
)

#: A page larger than this is a misbehaving or hostile upstream, not a big page.
MAX_PAGE_RECORDS = 500
#: 8 MB. A feed response larger than this cannot be a legitimate page of 200.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class FeedReviews(BaseModel):
    model_config = ConfigDict(extra="ignore")

    count: int = Field(default=0, ge=0, le=100_000)
    rating_avg: float | None = Field(default=None, ge=0, le=5)
    expertise_avg: float | None = Field(default=None, ge=0, le=5)
    patience_avg: float | None = Field(default=None, ge=0, le=5)
    reliability_avg: float | None = Field(default=None, ge=0, le=5)
    communication_avg: float | None = Field(default=None, ge=0, le=5)
    #: Parsed by pydantic, so the feed speaks ISO 8601 rather than the five
    #: ad-hoc formats the old MySQL `teacher_review.date` varchar used.
    latest_review_at: datetime | None = None


class FeedTutor(BaseModel):
    """One record as the website publishes it.

    `extra="ignore"` is the allowlist in force: anything the website adds that
    is not declared here never reaches our process memory, let alone the
    projection table.
    """

    model_config = ConfigDict(extra="ignore")

    user_id: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=200)
    gender: str | None = Field(default=None, max_length=20)
    avatar: str | None = Field(default=None, max_length=500)
    city: str | None = Field(default=None, max_length=120)
    locality: str | None = Field(default=None, max_length=120)
    district: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, max_length=20)
    experience: str | None = Field(default=None, max_length=120)
    education: str | None = Field(default=None, max_length=600)
    profile_summary: str | None = Field(default=None, max_length=4_000)
    budget: str | None = Field(default=None, max_length=120)
    subjects: list[str] = Field(default_factory=list, max_length=64)
    boards: list[str] = Field(default_factory=list, max_length=32)
    classes: list[str] = Field(default_factory=list, max_length=64)
    modes: list[str] = Field(default_factory=list, max_length=8)
    reviews: FeedReviews = Field(default_factory=FeedReviews)
    #: Optional. The website historically published none, which is why agent
    #: 013 reports MISSING for most tutors; when it does, the schedule flows
    #: through to `tutor_availability` and the overlap machinery runs for real.
    availability: FeedAvailability | None = None
    updated_at: datetime | None = None


class FeedWindow(BaseModel):
    """One availability window, as the website publishes it.

    Availability is the dimension most likely to be quietly wrong, so it is
    strict here: an unparseable window fails its record rather than being
    dropped, because a *partial* schedule is worse than none — agent 013 would
    then confidently tell a parent a tutor is free at a time they are not.
    """

    model_config = ConfigDict(extra="ignore")

    #: Monday = 0, matching `datetime.weekday()` and `contracts.schedule.Weekday`.
    weekday: int = Field(ge=0, le=6)
    #: "HH:MM", 24-hour. "24:00" is not accepted; use "00:00" as an end bound.
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class FeedAvailability(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timezone: str = Field(default="Asia/Kolkata", max_length=48)
    windows: list[FeedWindow] = Field(default_factory=list, max_length=64)


class FeedPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tutors: list[FeedTutor] = Field(default_factory=list, max_length=MAX_PAGE_RECORDS)
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class SyncPage:
    """What the ingest side consumes. Same shape the MySQL source produced, so
    `sync/projection.py` did not have to change with the transport."""

    tutors: tuple[TutorCandidate, ...]
    fetched: int
    has_more: bool


class FeedUnavailable(Exception):
    """The feed could not be read. Retryable; the checkpoint does not advance."""


class WebsiteTutorFeed:
    """Pages the website's tutor feed over signed HTTPS.

    Retries transient failures only. A 4xx means our request or our credentials
    are wrong, and retrying it just delays the alarm.
    """

    def __init__(
        self,
        *,
        base_url: str,
        signing_key: str,
        url_policy: UrlPolicy,
        page_size: int = 200,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not signing_key:
            raise ValueError("WebsiteTutorFeed requires a base_url and signing_key")
        if not 1 <= page_size <= MAX_PAGE_RECORDS:
            raise ValueError(f"page_size must be 1..{MAX_PAGE_RECORDS}")
        self._base_url = base_url.rstrip("/")
        self._signing_key = signing_key
        self._url_policy = url_policy
        self._page_size = page_size
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self.failures = 0

    async def fetch_page(self, *, offset: int, now: datetime | None = None) -> SyncPage:
        stamp = now or datetime.now(UTC)
        query = f"?limit={self._page_size}&offset={max(0, offset)}"
        payload = await self._get(f"{FEED_PATH}{query}")

        try:
            page = FeedPage.model_validate(payload)
        except ValidationError as exc:
            # Malformed upstream data is a feed problem, not a reason to write
            # garbage into the projection. Fail the page; the checkpoint holds.
            raise FeedUnavailable(f"malformed feed page: {exc.error_count()} errors") from exc

        tutors = tuple(_to_candidate(record, stamp) for record in page.tutors)
        return SyncPage(
            tutors=tutors,
            fetched=len(tutors),
            # Trust the server's flag, but never loop forever on an empty page.
            has_more=bool(page.has_more and tutors),
        )

    async def _get(self, path: str) -> Any:
        url = validate(f"{self._base_url}{path}", self._url_policy)
        last: str = "unknown"
        attempts = 0

        for _ in range(self._max_retries + 1):
            attempts += 1
            timestamp = int(clock.time())
            headers = {
                "Accept": "application/json",
                "X-Nxt-Signature": sign(
                    self._signing_key, SignedRequest("GET", path, timestamp, b"")
                ),
                "X-Nxt-Timestamp": str(timestamp),
                "X-Nxt-Agent": "tutor_match_meta_agent",
            }
            try:
                response = await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                self.failures += 1
                last = f"transport_error:{type(exc).__name__}"
                continue

            if 400 <= response.status_code < 500:
                self.failures += 1
                raise FeedUnavailable(f"feed rejected the request: {response.status_code}")
            if response.status_code >= 500:
                self.failures += 1
                last = f"upstream_{response.status_code}"
                continue

            if len(response.content) > MAX_RESPONSE_BYTES:
                raise FeedUnavailable("feed response exceeds the size ceiling")
            try:
                return response.json()
            except ValueError as exc:
                raise FeedUnavailable("feed returned a non-JSON body") from exc

        logger.warning(
            "tutor feed unavailable", extra={"tmm_reason": last, "tmm_attempts": attempts}
        )
        raise FeedUnavailable(last)

    async def close(self) -> None:
        await self._client.aclose()


def _to_candidate(record: FeedTutor, stamp: datetime) -> TutorCandidate:
    """Map one feed record into the service's shape, normalising as we go.

    Normalisation is ours, not the website's: "Maths", "Mathematics" and "Math"
    must collapse to one canonical subject or the hard filter silently drops
    tutors who teach exactly what was asked for.
    """
    subject_names = [c for s in record.subjects if (c := subjects.normalize(clean(s)))]
    board_names = [c for b in record.boards if (c := academics.normalize_board(clean(b)))]
    class_names = [c for k in record.classes if (c := academics.normalize_class(clean(k)))]

    return TutorCandidate(
        tutor_id=record.user_id,
        public_ref=encode_public_ref(record.user_id),
        name=clean(record.name) or "Tutor",
        gender=_gender(record.gender),
        avatar_url=clean(record.avatar) or None,
        city=clean(record.city) or None,
        locality=clean(record.locality) or None,
        district=clean(record.district) or None,
        state=clean(record.state) or None,
        pincode=_pincode(record.pincode),
        capabilities=TutorCapabilities(
            subjects=tuple(dict.fromkeys(subject_names))[:12],
            boards=tuple(dict.fromkeys(board_names))[:8],
            classes=tuple(dict.fromkeys(class_names))[:16],
            modes=modes.union_modes(*record.modes),
        ),
        experience_years=fees.parse_experience_years(clean(record.experience)),
        education=clean(record.education)[:400] or None,
        profile_summary=clean(record.profile_summary)[:1_200] or None,
        fee=fees.parse_tutor_fee(clean(record.budget)),
        reviews=_reviews(record.reviews),
        availability=_availability(record.availability),
        freshness=Freshness.FRESH,
        source_updated_at=record.updated_at or stamp,
        synced_at=stamp,
    )


def _availability(row: FeedAvailability | None) -> WeeklySchedule | None:
    """Build a normalised weekly schedule, or None when nothing was published.

    `WeeklySchedule` merges touching windows and rejects `end <= start`, so a
    malformed pair raises here and fails the whole page rather than producing
    a schedule that is confidently wrong.
    """
    if row is None or not row.windows:
        return None
    windows = tuple(
        TimeWindow(
            weekday=Weekday(window.weekday),
            start=time.fromisoformat(window.start),
            end=time.fromisoformat(window.end),
        )
        for window in row.windows
    )
    return WeeklySchedule(timezone=row.timezone, windows=windows)


def _reviews(row: FeedReviews) -> ReviewAggregate:
    return ReviewAggregate(
        count=row.count,
        rating_avg=_round(row.rating_avg),
        expertise_avg=_round(row.expertise_avg),
        patience_avg=_round(row.patience_avg),
        reliability_avg=_round(row.reliability_avg),
        communication_avg=_round(row.communication_avg),
        latest_review_at=row.latest_review_at,
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _gender(value: str | None) -> str | None:
    text = clean(value or "").lower()
    return text.capitalize() if text in {"male", "female"} else None


def _pincode(value: str | None) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if len(digits) == 6 else None


def build_feed(
    *,
    base_url: str,
    signing_key: str,
    url_policy: UrlPolicy,
    page_size: int = 200,
    timeout_seconds: float = 15.0,
) -> WebsiteTutorFeed | None:
    """`None` when the feed is not configured — the caller reports a skip rather
    than crashing a scheduled run that was never meant to do anything."""
    if not base_url or not signing_key:
        return None
    return WebsiteTutorFeed(
        base_url=base_url,
        signing_key=signing_key,
        url_policy=url_policy,
        page_size=page_size,
        timeout_seconds=timeout_seconds,
    )
