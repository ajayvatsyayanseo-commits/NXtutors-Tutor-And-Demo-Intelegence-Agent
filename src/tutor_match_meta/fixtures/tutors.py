"""Synthetic tutor data shaped like the real `register` projection.

**No real NXTutors PII appears here.** Every name is invented, every profile is
written for this file, and there are no phone numbers, emails or addresses —
which is also why the model has no field for them.

The set is built to exercise the awkward cases rather than the happy path:
tutors with no reviews, one review, many stale reviews; blank capability columns;
missing availability; fee bands that overshoot; a deactivated (stale) row; an
online-only tutor; and a Hindi tutor who must never surface for a Maths request.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from tutor_match_meta.contracts.common import Freshness, TuitionMode
from tutor_match_meta.contracts.schedule import TimeWindow, Weekday, WeeklySchedule
from tutor_match_meta.contracts.tutor import (
    FeeBand,
    GeoPoint,
    ReviewAggregate,
    TutorCandidate,
    TutorCapabilities,
)
from tutor_match_meta.domain.identity import encode_public_ref

#: Approximate centroids for Gurugram sectors, as the offline geocoder would
#: return them. Pincode-level precision, which is all we ever use.
GURUGRAM_POINTS: dict[str, tuple[float, float]] = {
    "122003": (28.4211, 77.0490),
    "122011": (28.4089, 77.0507),
    "122018": (28.4247, 77.0722),
    "122001": (28.4595, 77.0266),
    "sector 57": (28.4211, 77.0490),
    "sector 56": (28.4243, 77.0902),
    "gurugram": (28.4595, 77.0266),
    "noida": (28.5355, 77.3910),
}


def _schedule(*windows: tuple[Weekday, int, int]) -> WeeklySchedule:
    return WeeklySchedule(
        timezone="Asia/Kolkata",
        windows=tuple(
            TimeWindow(weekday=day, start=time(start, 0), end=time(end, 0))
            for day, start, end in windows
        ),
    )


def _tutor(
    tutor_id: str,
    name: str,
    **kwargs: object,
) -> TutorCandidate:
    return TutorCandidate(
        tutor_id=tutor_id,
        public_ref=encode_public_ref(tutor_id),
        name=name,
        **kwargs,
    )


def sample_tutors(now: datetime | None = None) -> list[TutorCandidate]:
    """A tutor set covering the realistic edge cases."""
    stamp = now or datetime.now(UTC)
    recent = stamp - timedelta(days=30)
    old = stamp - timedelta(days=1200)

    return [
        # --- the strong, complete match
        _tutor(
            "NXT10001",
            "Anita Sharma",
            gender="Female",
            city="Gurugram",
            locality="Sector 56",
            district="Gurugram",
            state="Haryana",
            pincode="122011",
            geo=GeoPoint(
                latitude=28.4243, longitude=77.0902, granularity="pincode", resolved_at=stamp
            ),
            capabilities=TutorCapabilities(
                subjects=("Mathematics", "Physics"),
                boards=("CBSE",),
                classes=("Class 8", "Class 9", "Class 10"),
                modes=(TuitionMode.HOME, TuitionMode.ONLINE),
            ),
            experience_years=8,
            education="M.Sc Mathematics, B.Ed",
            profile_summary=(
                "Patient and structured teaching with a focus on concept clarity and "
                "step-by-step problem solving for board students."
            ),
            fee=FeeBand(minimum=800, maximum=1000, label="₹800–₹1,000"),
            reviews=ReviewAggregate(
                count=17,
                rating_avg=4.6,
                expertise_avg=4.7,
                patience_avg=4.8,
                reliability_avg=4.5,
                communication_avg=4.4,
                latest_review_at=recent,
            ),
            availability=_schedule(
                (Weekday.MON, 17, 21), (Weekday.WED, 17, 21), (Weekday.FRI, 17, 21)
            ),
            active_students=6,
            freshness=Freshness.FRESH,
            source_updated_at=stamp - timedelta(hours=2),
            synced_at=stamp - timedelta(hours=1),
        ),
        # --- good academically, further away, no availability recorded
        _tutor(
            "NXT10002",
            "Rahul Verma",
            gender="Male",
            city="Gurugram",
            locality="Sector 14",
            pincode="122001",
            geo=GeoPoint(
                latitude=28.4595, longitude=77.0266, granularity="pincode", resolved_at=stamp
            ),
            capabilities=TutorCapabilities(
                subjects=("Mathematics",),
                boards=("CBSE", "ICSE"),
                classes=("Class 9", "Class 10", "Class 11", "Class 12"),
                modes=(TuitionMode.HOME,),
            ),
            experience_years=12,
            education="B.Tech, 12 years teaching",
            profile_summary="Exam-oriented coaching with previous year paper practice.",
            fee=FeeBand(minimum=900, maximum=1200, label="₹900–₹1,200"),
            reviews=ReviewAggregate(
                count=31,
                rating_avg=4.4,
                expertise_avg=4.6,
                patience_avg=4.0,
                reliability_avg=4.3,
                communication_avg=4.2,
                latest_review_at=recent,
            ),
            availability=None,  # the common real case: nothing recorded
            active_students=11,
            freshness=Freshness.FRESH,
            source_updated_at=stamp - timedelta(hours=5),
            synced_at=stamp - timedelta(hours=1),
        ),
        # --- brand new tutor, zero reviews, complete profile otherwise
        _tutor(
            "NXT10003",
            "Priya Nair",
            gender="Female",
            city="Gurugram",
            locality="Sector 57",
            pincode="122003",
            geo=GeoPoint(
                latitude=28.4211, longitude=77.0490, granularity="pincode", resolved_at=stamp
            ),
            capabilities=TutorCapabilities(
                subjects=("Mathematics", "Science"),
                boards=("CBSE",),
                classes=("Class 6", "Class 7", "Class 8", "Class 9", "Class 10"),
                modes=(TuitionMode.HOME,),
            ),
            experience_years=3,
            education="M.Sc Physics",
            profile_summary=(
                "Friendly, beginner-friendly approach. Builds confidence in students who "
                "find the subject difficult."
            ),
            fee=FeeBand(minimum=600, maximum=800, label="₹600–₹800"),
            reviews=ReviewAggregate(count=0),
            availability=_schedule(
                (Weekday.TUE, 18, 21), (Weekday.THU, 18, 21), (Weekday.SAT, 10, 14)
            ),
            active_students=2,
            freshness=Freshness.FRESH,
            source_updated_at=stamp - timedelta(hours=1),
            synced_at=stamp - timedelta(minutes=30),
        ),
        # --- one glowing review: must NOT outrank the 17-review tutor
        _tutor(
            "NXT10004",
            "Sunil Gupta",
            gender="Male",
            city="Gurugram",
            locality="Sector 57",
            pincode="122003",
            capabilities=TutorCapabilities(
                subjects=("Mathematics",),
                boards=("CBSE",),
                classes=("Class 10",),
                modes=(TuitionMode.HOME,),
            ),
            experience_years=2,
            fee=FeeBand(minimum=700, maximum=700, label="₹700"),
            reviews=ReviewAggregate(count=1, rating_avg=5.0, latest_review_at=recent),
            freshness=Freshness.FRESH,
            source_updated_at=stamp - timedelta(hours=3),
            synced_at=stamp - timedelta(hours=1),
        ),
        # --- strong ratings but all of them very old
        _tutor(
            "NXT10005",
            "Meera Iyer",
            gender="Female",
            city="Gurugram",
            locality="Sector 45",
            pincode="122018",
            capabilities=TutorCapabilities(
                subjects=("Mathematics",),
                boards=("CBSE", "ICSE"),
                classes=("Class 9", "Class 10"),
                modes=(TuitionMode.HOME,),
            ),
            experience_years=15,
            fee=FeeBand(minimum=1000, maximum=1400, label="₹1,000–₹1,400"),
            reviews=ReviewAggregate(
                count=22, rating_avg=4.9, reliability_avg=4.7, latest_review_at=old
            ),
            freshness=Freshness.FRESH,
            source_updated_at=stamp - timedelta(hours=6),
            synced_at=stamp - timedelta(hours=2),
        ),
        # --- online only: must be filtered out of a home-tuition request
        _tutor(
            "NXT10006",
            "Arjun Desai",
            gender="Male",
            city="Pune",
            capabilities=TutorCapabilities(
                subjects=("Mathematics", "Physics"),
                boards=("CBSE", "IB"),
                classes=("Class 9", "Class 10", "Class 11", "Class 12"),
                modes=(TuitionMode.ONLINE,),
            ),
            experience_years=9,
            profile_summary="Interactive online sessions with regular doubt-clearing.",
            fee=FeeBand(minimum=1200, maximum=1500, label="₹1,200–₹1,500"),
            reviews=ReviewAggregate(
                count=44,
                rating_avg=4.7,
                communication_avg=4.8,
                patience_avg=4.5,
                latest_review_at=recent,
            ),
            availability=_schedule(
                (Weekday.MON, 19, 22), (Weekday.TUE, 19, 22), (Weekday.THU, 19, 22)
            ),
            freshness=Freshness.FRESH,
            source_updated_at=stamp - timedelta(hours=4),
            synced_at=stamp - timedelta(hours=1),
        ),
        # --- wrong subject: the containment-bug canary
        _tutor(
            "NXT10007",
            "Kavita Joshi",
            gender="Female",
            city="Gurugram",
            locality="Sector 56",
            pincode="122011",
            capabilities=TutorCapabilities(
                subjects=("Hindi", "Sanskrit"),
                boards=("CBSE",),
                classes=("Class 6", "Class 7", "Class 8", "Class 9", "Class 10"),
                modes=(TuitionMode.HOME,),
            ),
            experience_years=6,
            fee=FeeBand(minimum=500, maximum=700, label="₹500–₹700"),
            reviews=ReviewAggregate(count=9, rating_avg=4.5, latest_review_at=recent),
            freshness=Freshness.FRESH,
            source_updated_at=stamp - timedelta(hours=8),
            synced_at=stamp - timedelta(hours=2),
        ),
        # --- deactivated since the last sync: must never be linked
        _tutor(
            "NXT10008",
            "Deepak Rao",
            gender="Male",
            city="Gurugram",
            locality="Sector 57",
            pincode="122003",
            capabilities=TutorCapabilities(
                subjects=("Mathematics",),
                boards=("CBSE",),
                classes=("Class 10",),
                modes=(TuitionMode.HOME,),
            ),
            experience_years=10,
            fee=FeeBand(minimum=750, maximum=900, label="₹750–₹900"),
            reviews=ReviewAggregate(count=12, rating_avg=4.8, latest_review_at=recent),
            freshness=Freshness.STALE,
            source_updated_at=stamp - timedelta(days=9),
            synced_at=stamp - timedelta(days=9),
        ),
        # --- almost no data at all: the thin-profile case
        _tutor(
            "NXT10009",
            "Rohit Bansal",
            city="Gurugram",
            capabilities=TutorCapabilities(subjects=("Mathematics",)),
            freshness=Freshness.FRESH,
            source_updated_at=stamp - timedelta(hours=12),
            synced_at=stamp - timedelta(hours=3),
        ),
        # --- fee far beyond a typical budget
        _tutor(
            "NXT10010",
            "Sanjay Mehta",
            gender="Male",
            city="Gurugram",
            locality="Golf Course Road",
            pincode="122011",
            capabilities=TutorCapabilities(
                subjects=("Mathematics", "Physics", "Chemistry"),
                boards=("CBSE", "IB", "IGCSE"),
                classes=("Class 11", "Class 12"),
                modes=(TuitionMode.HOME, TuitionMode.ONLINE),
            ),
            experience_years=18,
            education="PhD Physics, IIT",
            profile_summary="Intensive crash courses for JEE and NEET aspirants.",
            fee=FeeBand(minimum=3000, maximum=4000, label="₹3,000–₹4,000"),
            reviews=ReviewAggregate(
                count=28,
                rating_avg=4.8,
                expertise_avg=4.9,
                patience_avg=3.8,
                reliability_avg=4.6,
                latest_review_at=recent,
            ),
            availability=_schedule((Weekday.SAT, 9, 13), (Weekday.SUN, 9, 13)),
            active_students=20,
            freshness=Freshness.FRESH,
            source_updated_at=stamp - timedelta(hours=2),
            synced_at=stamp - timedelta(hours=1),
        ),
    ]
