"""The two-hop pipeline, end to end: enrich (internet) -> match (VPC).

These are the tests that would have caught the defect. The match worker used to
build an OpenAI client and call it from inside the VPC, where no route to
`api.openai.com` exists. Nothing failed loudly — every call blocked until the
client timeout and the turn quietly degraded to a deterministic parse.

So the properties asserted here are:

* the enrich worker resolves the model-dependent work and puts it *on the
  envelope*, where the match worker can read it without a network call;
* the match worker honours what it was given and makes no LLM call of its own;
* a turn still completes when enrichment fails entirely — the parent gets an
  answer, and the degradation is recorded rather than hidden;
* an envelope from an incompatible build is refused, not half-understood.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.e2e.test_conversations import turn
from tutor_match_meta.contracts.enrichment import (
    ENRICHMENT_VERSION,
    EnrichmentV1,
    UnsupportedEnrichment,
)
from tutor_match_meta.contracts.inbound import (
    InboundEnvelope,
    InboundKind,
    ParentSelectionV1,
)
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.orchestration.enrichment import (
    EnrichmentDependencies,
    EnrichmentService,
)
from tutor_match_meta.security.pii import Pseudonymiser

NOW = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)


class ExplodingLLM:
    """Stands in for an unreachable provider — which is what OpenAI looked like
    from inside the VPC: not an error, just something that never answers."""

    def __init__(self) -> None:
        self.calls = 0

    async def structured(self, request: object) -> object:
        self.calls += 1
        raise AssertionError("the match worker must not call a model")


class RecordingMemory:
    def __init__(self, facts: dict[str, str] | None = None, degraded: bool = False) -> None:
        self._facts = facts or {}
        self._degraded = degraded
        self.recalls = 0

    async def recall(self, **kwargs: object) -> object:
        self.recalls += 1

        class Fact:
            def __init__(self, key: str, value: str) -> None:
                self.key, self.value = key, value
                self.confidence, self.denied = 0.9, False

        class Packet:
            pass

        packet = Packet()
        packet.degraded = self._degraded  # type: ignore[attr-defined]
        packet.facts = [Fact(k, v) for k, v in self._facts.items()]  # type: ignore[attr-defined]
        return packet

    async def record(self, **kwargs: object) -> bool:
        return True


def enrichment_service(**overrides: object) -> EnrichmentService:
    deps = EnrichmentDependencies(pseudonymiser=Pseudonymiser("test-pepper"), **overrides)  # type: ignore[arg-type]
    return EnrichmentService(deps)


class TestTheEnrichHop:
    async def test_it_attaches_a_requirement_the_match_worker_can_use(self) -> None:
        enriched = await enrichment_service().enrich(
            turn("class 10 cbse maths gurgaon home tuition")
        )
        assert enriched.enrichment is not None
        requirement = enriched.enrichment.requirement
        assert requirement.value_of("subject") == "Mathematics"
        assert requirement.value_of("student_class") == "Class 10"
        assert requirement.value_of("board") == "CBSE"

    async def test_it_does_not_mutate_the_original_envelope(self) -> None:
        """The envelope is frozen; enrichment must produce a copy so a retry of
        the same record starts from the same input."""
        original = turn("class 9 science")
        enriched = await enrichment_service().enrich(original)
        assert original.enrichment is None
        assert enriched.enrichment is not None
        assert enriched.dedup_key == original.dedup_key
        assert enriched.trace_id == original.trace_id

    async def test_recalled_memory_travels_on_the_envelope(self) -> None:
        service = enrichment_service(memory=RecordingMemory({"board": "ICSE"}))
        enriched = await service.enrich(turn("class 8 maths"))
        assert enriched.enrichment is not None
        assert enriched.enrichment.memory_facts == {"board": "ICSE"}

    async def test_a_degraded_memory_service_is_recorded_not_raised(self) -> None:
        service = enrichment_service(memory=RecordingMemory(degraded=True))
        enriched = await service.enrich(turn("class 8 maths"))
        assert enriched.enrichment is not None
        assert "chitragupta" in enriched.enrichment.degraded
        assert enriched.enrichment.memory_facts == {}

    async def test_a_selection_needs_no_enrichment(self) -> None:
        """It carries no free text; a model call would be pure waste."""
        selection = InboundEnvelope(
            kind=InboundKind.PARENT_SELECTION,
            trace_id="t",
            conversation_id="conv-1",
            dedup_key="conv-1:sel",
            received_at=NOW,
            source_agent="whatsapp-router",
            payload=ParentSelectionV1(
                event_id="e",
                conversation_id="conv-1",
                match_session_id="ms",
                selected_public_ref="ref",
            ),
        )
        assert (await enrichment_service().enrich(selection)).enrichment is None


class TestTheMatchHop:
    async def test_the_match_worker_makes_no_model_call_when_enriched(
        self, turn_service, turn_deps
    ) -> None:
        """The property the whole split exists for."""
        turn_deps.llm = ExplodingLLM()
        enriched = await enrichment_service().enrich(
            turn("class 10 cbse maths gurgaon home tuition")
        )

        result = await turn_service.handle(enriched)

        assert turn_deps.llm.calls == 0
        assert result.matched
        assert result.reply and "nxtutors.com/tutor/" in result.reply

    async def test_it_uses_the_requirement_it_was_handed(self, turn_service, turn_deps) -> None:
        """A requirement the local parser would never produce from this text,
        so a match on it proves the enrichment was honoured, not re-derived."""
        turn_deps.llm = ExplodingLLM()
        envelope = turn("hello there")
        enriched = envelope.model_copy(
            update={
                "enrichment": EnrichmentV1(
                    requirement=MatchRequirementV1.model_validate(
                        {
                            "conversation_id": envelope.conversation_id,
                            "captured_at": NOW,
                            "subject": {"value": "Mathematics"},
                            "student_class": {"value": "Class 10"},
                            "board": {"value": "CBSE"},
                        }
                    ),
                    enriched_at=NOW,
                )
            }
        )

        result = await turn_service.handle(enriched)
        assert result.matched, "the match worker ignored the enrichment it was given"

    async def test_degradation_from_the_enrich_hop_is_reported_on_the_turn(
        self, turn_service
    ) -> None:
        service = enrichment_service(memory=RecordingMemory(degraded=True))
        enriched = await service.enrich(turn("class 10 cbse maths gurgaon home tuition"))
        result = await turn_service.handle(enriched)
        assert "chitragupta" in result.degraded

    async def test_an_unenriched_envelope_still_works(self, turn_service) -> None:
        """The local stack and the CLI run one process for everything, and a
        deployment mid-migration will briefly have both shapes in flight."""
        result = await turn_service.handle(turn("class 10 cbse maths gurgaon home tuition"))
        assert result.matched


class TestVersionSkew:
    """The two sides deploy separately and *will* run different builds."""

    def test_the_current_version_is_accepted(self) -> None:
        from tutor_match_meta.contracts.enrichment import assert_supported

        assert_supported(
            EnrichmentV1(
                requirement=MatchRequirementV1(conversation_id="c", captured_at=NOW),
                enriched_at=NOW,
            )
        )

    def test_a_future_version_is_refused_loudly(self) -> None:
        from tutor_match_meta.contracts.enrichment import assert_supported

        future = EnrichmentV1(
            requirement=MatchRequirementV1(conversation_id="c", captured_at=NOW),
            enriched_at=NOW,
            enrichment_version="99",
        )
        with pytest.raises(UnsupportedEnrichment, match="99"):
            assert_supported(future)

    async def test_a_skewed_envelope_fails_the_turn_rather_than_matching_blind(
        self, turn_service
    ) -> None:
        """It must reach the DLQ. Treating it as 'no enrichment' would produce a
        worse shortlist with no signal that anything was wrong."""
        envelope = turn("class 10 maths")
        skewed = envelope.model_copy(
            update={
                "enrichment": EnrichmentV1(
                    requirement=MatchRequirementV1(
                        conversation_id=envelope.conversation_id, captured_at=NOW
                    ),
                    enriched_at=NOW,
                    enrichment_version="99",
                )
            }
        )
        with pytest.raises(UnsupportedEnrichment):
            await turn_service.handle(skewed)

    def test_the_version_constant_is_a_bare_string(self) -> None:
        """It is compared with `!=`, so a tuple or an int would silently never
        match and refuse every envelope in production."""
        assert isinstance(ENRICHMENT_VERSION, str) and ENRICHMENT_VERSION


class TestBoundedPayload:
    def test_an_oversized_memory_packet_is_refused(self) -> None:
        """SQS caps a message at 256 KB. An unbounded recall would silently push
        past it and every enriched turn would fail to forward."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="memory packet"):
            EnrichmentV1(
                requirement=MatchRequirementV1(conversation_id="c", captured_at=NOW),
                enriched_at=NOW,
                memory_facts={f"k{i}": "v" for i in range(64)},
            )

    async def test_the_service_truncates_rather_than_producing_one(self) -> None:
        service = enrichment_service(memory=RecordingMemory({f"k{i}": "v" for i in range(64)}))
        enriched = await service.enrich(turn("class 8 maths"))
        assert enriched.enrichment is not None
        assert len(enriched.enrichment.memory_facts) == 32

    async def test_an_enriched_envelope_fits_in_an_sqs_message(self) -> None:
        """256 KB is the hard limit; a shortlist-bearing turn must be far inside."""
        service = enrichment_service(memory=RecordingMemory({"board": "CBSE"}))
        enriched = await service.enrich(
            turn("class 10 cbse maths gurgaon home tuition after 6:30pm, budget 900")
        )
        assert len(enriched.model_dump_json().encode()) < 64 * 1024
