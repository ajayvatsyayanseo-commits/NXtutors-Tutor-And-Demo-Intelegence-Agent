"""Analytics must be exportable without a second privacy review.

The export lands in S3 and is catalogued by Glue for a BI tool. Whatever ends
up in `analytics_event` is therefore read by people who have no business seeing
a parent's message — so the shape has to make that impossible rather than
discouraged (§3, §23, §31).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tutor_match_meta.analytics import (
    ALLOWED_DIMENSIONS,
    AnalyticsEventName,
    AnalyticsEventV1,
    UnsafeDimension,
    class_band,
    coverage_band,
)
from tutor_match_meta.contracts.inbound import InboundEnvelope, InboundKind, WhatsAppTurnV1
from tutor_match_meta.orchestration.turn_service import TurnService

pytestmark = pytest.mark.security


def event(**dimensions: object) -> AnalyticsEventV1:
    return AnalyticsEventV1(
        name=AnalyticsEventName.SHORTLIST_GENERATED,
        conversation_ref="cv_abc123",
        dimensions=dict(dimensions),  # type: ignore[arg-type]
    )


class TestClosedShape:
    def test_an_unlisted_dimension_is_refused_at_construction(self) -> None:
        with pytest.raises(UnsafeDimension, match="allowlist"):
            event(message_text="I need a maths tutor")

    def test_an_identifying_dimension_is_refused(self) -> None:
        with pytest.raises(UnsafeDimension):
            event(phone="9876543210")

    def test_a_raw_conversation_id_is_refused(self) -> None:
        with pytest.raises(UnsafeDimension, match="pseudonym"):
            AnalyticsEventV1(
                name=AnalyticsEventName.MATCH_STARTED, conversation_ref="wa:+919876543210"
            )

    def test_pii_smuggled_into_an_allowed_dimension_is_refused(self) -> None:
        """Belt and braces behind the allowlist, for the day a bounded field
        starts carrying free text."""
        with pytest.raises(UnsafeDimension, match="direct identifier"):
            event(city="Gurugram, call 9876543210")

    def test_no_allowed_dimension_is_free_text_shaped(self) -> None:
        """A reviewable statement of what the export may ever contain."""
        assert "message" not in " ".join(ALLOWED_DIMENSIONS)
        assert "text" not in " ".join(ALLOWED_DIMENSIONS)
        assert "locality" not in ALLOWED_DIMENSIONS
        assert "pincode" not in ALLOWED_DIMENSIONS
        assert "name" not in ALLOWED_DIMENSIONS

    def test_an_ordinary_funnel_event_is_accepted(self) -> None:
        row = event(subject="Mathematics", city="Gurugram", shortlist_size=3).as_row()
        assert row["event_name"] == "shortlist_generated"
        assert row["dimensions"]["shortlist_size"] == 3


class TestBucketing:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Class 3", "primary"),
            ("Class 7", "middle"),
            ("Class 10", "secondary"),
            ("Class 12", "senior"),
            (None, "unknown"),
        ],
    )
    def test_classes_are_banded(self, label: str | None, expected: str) -> None:
        assert class_band(label) == expected

    @pytest.mark.parametrize(("value", "expected"), [(0.9, "high"), (0.5, "medium"), (0.1, "low")])
    def test_coverage_is_banded_not_exact(self, value: float, expected: str) -> None:
        """A float like 0.4137 is identifier-shaped in a small population."""
        assert coverage_band(value) == expected


class TestEmittedByTheRealTurn:
    async def test_a_successful_turn_emits_a_clean_funnel(
        self, turn_service: TurnService, analytics
    ) -> None:
        await turn_service.handle(
            InboundEnvelope(
                kind=InboundKind.WHATSAPP_TURN,
                trace_id="an",
                conversation_id="c-an",
                dedup_key="an:1",
                received_at=datetime.now(UTC),
                source_agent="lead_intake_agent",
                payload=WhatsAppTurnV1(
                    event_id="e",
                    conversation_id="c-an",
                    provider_message_id="m",
                    text="class 10 cbse maths near sector 57 gurgaon, home tuition, 9876543210",
                ),
            )
        )
        assert "match_started" in analytics.names()
        assert "shortlist_generated" in analytics.names()

        blob = repr([e.as_row() for e in analytics.events])
        assert "9876543210" not in blob, "a phone number reached the analytics export"
        assert "sector 57" not in blob.lower(), "street-level locality reached the export"
        assert "c-an" not in blob, "a raw conversation id reached the export"

    async def test_a_no_match_turn_records_its_reason(
        self, turn_service: TurnService, analytics
    ) -> None:
        await turn_service.handle(
            InboundEnvelope(
                kind=InboundKind.WHATSAPP_TURN,
                trace_id="an2",
                conversation_id="c-an2",
                dedup_key="an2:1",
                received_at=datetime.now(UTC),
                source_agent="test",
                payload=WhatsAppTurnV1(
                    event_id="e",
                    conversation_id="c-an2",
                    provider_message_id="m",
                    text="class 12 ib astrophysics tutor in reykjavik, home tuition",
                ),
            )
        )
        names = analytics.names()
        assert "no_candidate" in names or "clarification_asked" in names
