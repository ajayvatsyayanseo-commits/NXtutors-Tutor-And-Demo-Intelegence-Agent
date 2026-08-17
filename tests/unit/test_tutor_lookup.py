"""Asking about a named tutor.

Two failure modes matter and they pull in opposite directions:

* **missing a name** — the parent says "tell me about Sneha Joshi" and gets
  "which subject?", which reads as not listening;
* **inventing a name** — a requirement message gets read as a person, and the
  turn stops asking for the class it actually needs.

The second is worse, so `detect()` returns None on every uncertain path and the
database is what confirms a candidate. These tests pin both directions.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tutor_match_meta.contracts.common import Freshness, TuitionMode
from tutor_match_meta.contracts.tutor import (
    FeeBand,
    ReviewAggregate,
    TutorCandidate,
    TutorCapabilities,
)
from tutor_match_meta.domain.identity import encode_public_ref
from tutor_match_meta.orchestration import tutor_lookup

NOW = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def tutor(**overrides: object) -> TutorCandidate:
    base: dict[str, object] = {
        "tutor_id": "2938",
        "public_ref": encode_public_ref("2938"),
        "name": "Sneha Joshi",
        "city": "Gurgaon",
        "capabilities": TutorCapabilities(
            subjects=("Mathematics",),
            boards=("CBSE",),
            classes=("Class 11",),
            modes=(TuitionMode.HOME,),
        ),
        "experience_years": 8,
        "education": "Delhi University",
        "fee": FeeBand(minimum=600, maximum=600, label="₹600"),
        "reviews": ReviewAggregate(count=60, rating_avg=4.63),
        "freshness": Freshness.FRESH,
        "synced_at": NOW,
    }
    base.update(overrides)
    return TutorCandidate(**base)  # type: ignore[arg-type]


class TestDetectsAName:
    @pytest.mark.parametrize(
        ("text", "expected", "summary"),
        [
            ("ajay vatsyan tutor", "ajay vatsyan", False),
            ("tell me about Sneha Joshi", "sneha joshi", True),
            ("is Rajesh mahera available?", "rajesh mahera", False),
            ("summarize Anjali Sharma", "anjali sharma", True),
            ("who is Karan Singhania", "karan singhania", True),
            ("profile of Meera Iyer", "meera iyer", True),
            ("Sneha Joshi ke baare me batao", "sneha joshi", True),
            ("tell me about Sneha", "sneha", True),
        ],
    )
    def test_the_name_is_extracted(self, text: str, expected: str, summary: bool) -> None:
        found = tutor_lookup.detect(text)
        assert found is not None, f"no name found in {text!r}"
        assert found.name == expected
        assert found.wants_summary is summary


class TestRefusesToInventAName:
    @pytest.mark.parametrize(
        "text",
        [
            "class 10 cbse maths tutor in gurugram",
            "class 11 physics online tuition",
            "hi i need a tutor",
            "maths",
            "class 12",
            "need a tutor in patna home tuition",
            "class 9 science tutor near sector 57 gurgaon",
            "class 10 maths tutor under 800 per hour",
            "online tuition chahiye",
            "ok",
            "",
        ],
    )
    def test_a_requirement_is_never_a_name(self, text: str) -> None:
        assert tutor_lookup.detect(text) is None

    def test_a_message_carrying_a_subject_or_class_is_a_requirement(self) -> None:
        """ "class 12 IB astrophysics tutor in reykjavik" leaves "astrophysics
        reykjavik" behind — a subject and a place, not a person."""
        assert tutor_lookup.detect("class 12 IB astrophysics tutor in reykjavik") is None

    def test_a_lone_unknown_word_is_not_a_name_without_an_explicit_ask(self) -> None:
        """Otherwise every unlisted city becomes a tutor to look up."""
        assert tutor_lookup.detect("i need a tutor in kohima") is None
        # ...but an explicit ask still works on a single word.
        assert tutor_lookup.detect("tell me about Kohima") is not None

    @pytest.mark.parametrize(
        "hostile",
        [
            "Robert'); DROP TABLE tutor_projection;--",
            "1; DELETE FROM match_decision WHERE 1=1;",
            "%' UNION SELECT * FROM llm_usage --",
            "' OR 1=1 LIMIT 1 OFFSET 0 --",
        ],
    )
    def test_hostile_input_is_not_read_as_a_person(self, hostile: str) -> None:
        """Not a security boundary — the terms are bound parameters either way —
        but a lookup on injection text is noise, and at four name words these
        started matching."""
        assert tutor_lookup.detect(hostile) is None


class TestTheSummaryIsGrounded:
    def test_it_reports_only_what_the_projection_holds(self) -> None:
        text = tutor_lookup.summarise(tutor())
        assert "Sneha Joshi" in text
        assert "Gurgaon" in text
        assert "₹600" in text
        assert "8 years of experience" in text
        assert "Mathematics" in text
        assert "Class 11" in text
        assert "CBSE" in text

    def test_a_tutor_with_no_data_gets_no_invented_lines(self) -> None:
        bare = TutorCandidate(tutor_id="X", public_ref=encode_public_ref("X"), name="No Data")
        text = tutor_lookup.summarise(bare)
        assert text.strip() == "*No Data*"
        for absent in ("rating", "review", "experience", "₹", "not available", "unknown"):
            assert absent.lower() not in text.lower()

    def test_no_rating_line_without_a_rating(self) -> None:
        text = tutor_lookup.summarise(tutor(reviews=ReviewAggregate()))
        assert "Rated" not in text
        assert "review" not in text.lower()

    def test_a_review_count_without_an_average_is_not_presented_as_a_score(self) -> None:
        text = tutor_lookup.summarise(tutor(reviews=ReviewAggregate(count=3)))
        assert "Rated" not in text
        assert "3 reviews on file" in text

    def test_the_rating_matches_how_the_shortlist_renders_it(self) -> None:
        """4.63 stored, 4.6 shown — the same tutor must not appear to have two
        different ratings depending on which reply the parent is reading."""
        assert "Rated 4.6 across 60 reviews" in tutor_lookup.summarise(tutor())

    def test_the_profile_link_is_included_when_given(self) -> None:
        url = "https://www.nxtutors.com/tutor/gurgaon/x/sneha-joshi"
        assert url in tutor_lookup.summarise(tutor(), profile_url=url)

    def test_no_link_is_invented_when_none_is_given(self) -> None:
        assert "http" not in tutor_lookup.summarise(tutor())

    def test_a_long_profile_blurb_is_clipped(self) -> None:
        text = tutor_lookup.summarise(tutor(profile_summary="word " * 200))
        assert len(text) < 600
        assert text.endswith("…") or "…" in text
