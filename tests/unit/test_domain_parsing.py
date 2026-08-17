"""Parsing the messages parents actually send.

Every case here is a real message shape, and several are regressions for bugs
found during development — they are marked as such so nobody "simplifies" the
guard away later.
"""

from __future__ import annotations

from datetime import time

import pytest

from tutor_match_meta.contracts.schedule import TimeWindow, Weekday, WeeklySchedule
from tutor_match_meta.domain import academics, fees, localities, modes, scheduling, subjects
from tutor_match_meta.domain.identity import decode_public_ref, encode_public_ref


class TestSubjects:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("need maths tutor", ["Mathematics"]),
            ("class 10 math", ["Mathematics"]),
            ("physics and chemistry", ["Physics", "Chemistry"]),
            ("sst teacher", ["Social Science"]),
            ("social science help", ["Social Science"]),
            ("computer science class 11", ["Computer Science"]),
            ("ganit ka tutor chahiye", ["Mathematics"]),
            ("accounts and eco", ["Accountancy", "Economics"]),
        ],
    )
    def test_extracts_subjects(self, text: str, expected: list[str]) -> None:
        assert subjects.extract(text) == expected

    def test_multiword_beats_single_word(self) -> None:
        """Regression: 'social science' must not be read as bare 'Science'."""
        assert subjects.extract("social science") == ["Social Science"]
        assert subjects.extract("computer science") == ["Computer Science"]

    def test_matching_is_not_substring_containment(self) -> None:
        """Regression: the website's substring match sends a CS tutor to a
        Class 8 Science parent. Ours must not."""
        assert not subjects.matches("Science", "Computer Science")
        assert not subjects.matches("Mathematics", "Hindi")

    def test_umbrella_covers_parts_but_not_the_reverse(self) -> None:
        assert subjects.matches("Physics", "Science")
        assert not subjects.matches("Science", "Physics")

    def test_unknown_subject_is_preserved_not_dropped(self) -> None:
        assert subjects.normalize("Astrophysics") == "Astrophysics"


class TestAcademics:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("class 10", "Class 10"),
            ("10th standard", "Class 10"),
            ("std 8", "Class 8"),
            ("grade 12", "Class 12"),
            ("class X", "Class 10"),
            ("lkg admission", "LKG"),
        ],
    )
    def test_class_extraction(self, text: str, expected: str) -> None:
        assert academics.extract_class(text) == expected

    def test_roman_numeral_needs_a_whole_token(self) -> None:
        """The 'x' in 'xerox' must not become Class 10."""
        assert academics.class_number("xerox shop") is None

    @pytest.mark.parametrize(
        "text",
        [
            "hi i need a tutor",
            "i need help",
            "I want a good teacher",
            "v good tutor chahiye",
            "can i get a demo",
        ],
    )
    def test_a_bare_roman_numeral_in_prose_is_not_a_grade(self, text: str) -> None:
        """`i` is the commonest English word in this inbox.

        Reading it as Roman numeral 1 made "hi i need a tutor" extract Class 1
        at DETERMINISTIC confidence, which then hard-filtered the candidate pool
        to Class 1 tutors for the rest of the conversation.
        """
        assert academics.extract_class(text) is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("class x", "Class 10"),
            ("std vii", "Class 7"),
            ("xii class", "Class 12"),
            ("my son is in class ix", "Class 9"),
            ("grade viii", "Class 8"),
        ],
    )
    def test_a_roman_numeral_next_to_a_class_word_still_counts(
        self, text: str, expected: str
    ) -> None:
        assert academics.extract_class(text) == expected

    @pytest.mark.parametrize(("value", "expected"), [("X", "Class 10"), ("ix", "Class 9")])
    def test_a_field_that_is_only_a_numeral_is_still_a_grade(
        self, value: str, expected: str
    ) -> None:
        """Website records store the grade alone; that is a field, not prose."""
        assert academics.normalize_class(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # `category.cat_title`, as the production dump actually stores it.
            # The hyphenated form covers 1,297 of 1,344 tutor-course rows.
            ("Class - XI", "Class 11"),
            ("Class- XII", "Class 12"),
            ("Class- XI", "Class 11"),
            ("Class-II", "Class 2"),
        ],
    )
    def test_the_websites_hyphenated_roman_labels_are_understood(
        self, value: str, expected: str
    ) -> None:
        """These are the real labels the tutor feed publishes.

        Before the separator was allowed, `"Class - XI"` fell through to the
        title-case fallback and became the string `"Class - Xi"`, which equals
        no requirement — class filtering was broken for almost the whole tutor
        base and nothing failed loudly.
        """
        assert academics.normalize_class(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Class 11-12", "Class 11-12"),
            ("6-12", "Class 6-12"),
            ("6 -12", "Class 6-12"),
            ("Class 6-10", "Class 6-10"),
        ],
    )
    def test_a_range_survives_normalisation(self, value: str, expected: str) -> None:
        """`teacher_courses.for_class` stores ranges. Collapsing one to its
        first grade narrowed the tutor to that grade alone."""
        assert academics.normalize_class(value) == expected

    @pytest.mark.parametrize(
        ("required", "taught"),
        [
            ("Class 12", ["Class 11-12"]),
            ("Class 8", ["Class 6-12"]),
            ("Class 11", ["Class - XI"]),
            ("Class 12", ["Class- XII"]),
        ],
    )
    def test_a_stored_range_still_matches_a_grade_inside_it(
        self, required: str, taught: list[str]
    ) -> None:
        assert academics.teaches_class(required, taught)

    def test_a_grade_outside_the_range_is_still_refused(self) -> None:
        assert not academics.teaches_class("Class 5", ["Class - XI"])
        assert not academics.teaches_class("Class 3", ["Class 6-12"])

    def test_the_cbsc_typo_maps_to_cbse(self) -> None:
        """14 rows in `teacher_courses.board`. Unmapped, it became its own
        board and those tutors never matched a CBSE requirement."""
        assert academics.normalize_board("CBSC") == "CBSE"
        assert academics.normalize_board("cbsc") == "CBSE"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("cbse board", "CBSE"),
            ("IB curriculum", "IB"),
            ("icse school", "ICSE"),
            ("igcse", "IGCSE"),
            ("state board", "State Board"),
        ],
    )
    def test_board_extraction(self, text: str, expected: str) -> None:
        assert academics.extract_board(text) == expected

    def test_board_mandatory_only_where_syllabuses_diverge(self) -> None:
        assert academics.board_is_mandatory("IB", 4) is True
        assert academics.board_is_mandatory("CBSE", 10) is True
        # Below Class 9 the Indian boards overlap heavily; filtering there would
        # delete good tutors for no benefit.
        assert academics.board_is_mandatory("CBSE", 4) is False

    def test_class_range_is_understood(self) -> None:
        assert academics.teaches_class("Class 8", ["Class 6-10"])
        assert not academics.teaches_class("Class 11", ["Class 6-10"])

    def test_no_class_requirement_never_excludes(self) -> None:
        assert academics.teaches_class(None, ["Class 10"])


class TestFees:
    @pytest.mark.parametrize(
        ("text", "low", "high"),
        [
            ("budget 800 to 1200", 800, 1200),
            ("under 1000", None, 1000),
            ("at least 500", 500, None),
            ("around 900 per hour", 765, 1035),
            ("fees 15k monthly", 12750, 17250),
            ("800 se 1200", 800, 1200),
        ],
    )
    def test_budget_parsing(self, text: str, low: int | None, high: int | None) -> None:
        parsed = fees.parse_budget(text)
        assert parsed is not None
        assert (parsed.minimum, parsed.maximum) == (low, high)

    def test_address_numbers_are_not_money(self) -> None:
        """Regression: 'sector 57' was parsed as a Rs 57 budget."""
        parsed = fees.parse_budget(
            "class 10 cbse maths near sector 57 after 6:30, around 900 per hour"
        )
        assert parsed is not None
        assert parsed.minimum == 765 and parsed.maximum == 1035

    def test_hindi_ka_is_not_a_thousands_multiplier(self) -> None:
        """Regression: 'class 9 ka' parsed as 9,000 because 'k' matched 'ka'."""
        parsed = fees.parse_budget("class 9 ka tutor chahiye budget 800 se 1200")
        assert parsed is not None
        assert parsed.minimum == 800

    def test_no_amount_returns_none(self) -> None:
        assert fees.parse_budget("need a maths tutor for class 10") is None

    def test_tutor_fee_never_claims_a_unit(self) -> None:
        band = fees.parse_tutor_fee("800-1000")
        assert band.unit_known is False
        assert band.label is not None
        assert "hour" not in band.label and "month" not in band.label


class TestScheduling:
    def test_after_time_binds_to_the_right_number(self) -> None:
        """Regression: 'class 10 ... after 6:30' scheduled tuition at 10:00."""
        schedule = scheduling.parse_schedule("class 10 cbse maths after 6:30")
        assert schedule is not None
        assert all(w.start == time(18, 30) for w in schedule.windows)

    def test_class_number_is_not_a_clock_reading(self) -> None:
        schedule = scheduling.parse_schedule("IB class 8 physics on weekends")
        assert schedule is not None
        assert {w.weekday for w in schedule.windows} == {Weekday.SAT, Weekday.SUN}
        assert all(w.start == time(6, 0) for w in schedule.windows)

    def test_exam_does_not_imply_am(self) -> None:
        """'am' as a substring of 'exam' must not force a morning reading."""
        schedule = scheduling.parse_schedule("board exam prep after 7")
        assert schedule is not None
        assert all(w.start == time(19, 0) for w in schedule.windows)

    def test_daypart_narrowed_by_explicit_range(self) -> None:
        schedule = scheduling.parse_schedule("weekdays morning 9 to 11")
        assert schedule is not None
        assert all(w.start == time(9, 0) and w.end == time(11, 0) for w in schedule.windows)
        assert Weekday.SAT not in {w.weekday for w in schedule.windows}

    def test_named_days(self) -> None:
        schedule = scheduling.parse_schedule("mon wed fri evening after 7pm")
        assert schedule is not None
        assert {w.weekday for w in schedule.windows} == {Weekday.MON, Weekday.WED, Weekday.FRI}

    def test_no_time_signal_returns_none(self) -> None:
        assert scheduling.parse_schedule("need a maths tutor") is None


class TestScheduleAlgebra:
    def test_touching_windows_merge(self) -> None:
        schedule = WeeklySchedule(
            windows=(
                TimeWindow(weekday=Weekday.MON, start=time(16, 0), end=time(18, 0)),
                TimeWindow(weekday=Weekday.MON, start=time(18, 0), end=time(20, 0)),
            )
        )
        assert len(schedule.windows) == 1
        assert schedule.total_minutes == 240

    def test_overlap_is_symmetric_and_exact(self) -> None:
        a = WeeklySchedule(
            windows=(TimeWindow(weekday=Weekday.MON, start=time(16, 0), end=time(20, 0)),)
        )
        b = WeeklySchedule(
            windows=(TimeWindow(weekday=Weekday.MON, start=time(18, 30), end=time(21, 0)),)
        )
        assert a.overlap_minutes(b) == b.overlap_minutes(a) == 90

    def test_cross_timezone_overlap_is_refused_not_guessed(self) -> None:
        a = WeeklySchedule(timezone="Asia/Kolkata", windows=())
        b = WeeklySchedule(timezone="UTC", windows=())
        with pytest.raises(ValueError, match="timezone"):
            a.overlap_minutes(b)

    def test_end_of_day_is_expressible(self) -> None:
        window = TimeWindow(weekday=Weekday.MON, start=time(23, 0), end=time(0, 0))
        assert window.duration_minutes == 60

    def test_zero_length_window_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="after start"):
            TimeWindow(weekday=Weekday.MON, start=time(18, 0), end=time(18, 0))


class TestTimezones:
    def test_conversion_splits_at_the_day_boundary(self) -> None:
        ist = WeeklySchedule(
            timezone="Asia/Kolkata",
            windows=(TimeWindow(weekday=Weekday.SUN, start=time(3, 0), end=time(5, 0)),),
        )
        utc = scheduling.to_timezone(ist, "UTC")
        assert [w.label() for w in utc.windows] == ["Sat 21:30-23:30"]

    def test_roundtrip_is_lossless(self) -> None:
        original = WeeklySchedule(
            timezone="Asia/Kolkata",
            windows=(TimeWindow(weekday=Weekday.MON, start=time(6, 0), end=time(8, 0)),),
        )
        there = scheduling.to_timezone(original, "America/New_York")
        back = scheduling.to_timezone(there, "Asia/Kolkata")
        assert back.windows == original.windows

    def test_midnight_end_survives_conversion(self) -> None:
        ist = WeeklySchedule(
            timezone="Asia/Kolkata",
            windows=(TimeWindow(weekday=Weekday.MON, start=time(23, 0), end=time(0, 0)),),
        )
        assert scheduling.to_timezone(ist, "UTC").total_minutes == 60


class TestLocalities:
    def test_city_aliases(self) -> None:
        assert localities.normalize_city("gurgaon") == "Gurugram"
        assert localities.same_city("Gurgaon", "Gurugram")
        assert "gurgaon" in localities.city_aliases("Gurugram")

    def test_locality_extraction(self) -> None:
        assert localities.extract_locality("near sector 57") == "Sector 57"
        assert localities.extract_locality("dlf phase 3") == "Phase 3"

    def test_ambiguous_pincode_is_refused(self) -> None:
        """Two candidate pincodes means we do not know; asking beats guessing."""
        assert localities.extract_pincode("122003 or 122011") is None
        assert localities.extract_pincode("pincode 122003") == "122003"

    def test_distance_is_plausible(self) -> None:
        from tutor_match_meta.contracts.tutor import GeoPoint

        a = GeoPoint(latitude=28.4211, longitude=77.0490, granularity="pincode")
        b = GeoPoint(latitude=28.4243, longitude=77.0902, granularity="pincode")
        assert 3.0 < localities.haversine_km(a, b) < 5.0


class TestModes:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("home tuition", "home"), ("online classes", "online"), ("ghar par", "home")],
    )
    def test_mode_extraction(self, text: str, expected: str) -> None:
        found = modes.extract_mode(text)
        assert found is not None and found.value == expected

    def test_both_columns_union(self) -> None:
        assert set(modes.union_modes("Online", "Home")) == {
            modes.TuitionMode.ONLINE,
            modes.TuitionMode.HOME,
        }

    def test_both_short_circuits(self) -> None:
        assert len(modes.parse_mode_tokens("Both")) == 2


class TestIdentity:
    def test_public_ref_roundtrip(self) -> None:
        assert decode_public_ref(encode_public_ref("NXT12345")) == "NXT12345"

    def test_encoding_matches_the_live_site(self) -> None:
        """Pinned to the blade template's
        `rtrim(strtr(base64_encode($id.'-nxt'), '+/', '-_'), '=')`."""
        assert encode_public_ref("NXT10001") == "TlhUMTAwMDEtbnh0"

    @pytest.mark.parametrize("bad", ["", "abcd", "!!!!", "bm90LW54dA=="])
    def test_malformed_refs_are_rejected(self, bad: str) -> None:
        assert decode_public_ref(bad) is None
