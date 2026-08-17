"""Build identity, and the outbox relay's decision table.

Two small modules that carry disproportionate operational weight: `version.py`
is what an on-call engineer reads first, and the relay is what decides whether
a parent's reply is sent, retried, or given up on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tutor_match_meta.contracts.outbound import (
    OUTBOX_KIND_CALLER_REPLY,
    OUTBOX_KIND_REPLY,
    OutboundDeliveryV1,
)
from tutor_match_meta.repositories.memory_store import InMemoryOutbox
from tutor_match_meta.repositories.ports import OutboxMessage
from tutor_match_meta.sync.outbox_relay import CLAIM_LEASE_SECONDS, _delivery, _retry_at


class TestBuildInfo:
    def test_it_reports_every_independently_rollbackable_version(self) -> None:
        """Six versions, separately, because they roll back on separate axes."""
        from tutor_match_meta.version import build_info

        info = build_info().as_dict()
        assert set(info) == {
            "app_version",
            "git_sha",
            "build_id",
            "schema_revision",
            "event_contract_version",
            "default_policy",
            "prompt_versions",
        }

    def test_it_names_every_registered_prompt(self) -> None:
        from tutor_match_meta.prompts.registry import REGISTRY
        from tutor_match_meta.version import build_info

        reported = build_info().prompt_versions
        assert set(reported) == {t.prompt_id for t in REGISTRY.active()}

    def test_an_unstamped_build_says_unknown_rather_than_guessing(self, monkeypatch) -> None:
        from tutor_match_meta import version

        monkeypatch.delenv("TMM_GIT_SHA", raising=False)
        version.build_info.cache_clear()
        try:
            assert version.build_info().git_sha == "unknown"
            assert version.build_info().short_sha == "unknown"
        finally:
            version.build_info.cache_clear()

    def test_the_schema_revision_matches_the_latest_migration(self) -> None:
        """A mismatch means the migration ran but the constant was not bumped —
        so `/version` would report a schema the code does not expect."""
        from pathlib import Path

        from tutor_match_meta.version import EXPECTED_SCHEMA_REVISION

        versions = Path(__file__).resolve().parents[2] / "migrations" / "versions"
        revisions = sorted(p.stem.split("_")[0] for p in versions.glob("*.py"))
        assert EXPECTED_SCHEMA_REVISION == revisions[-1]


class TestPromptRegistry:
    def test_a_pin_resolves_to_the_named_version(self, monkeypatch) -> None:
        from tutor_match_meta.prompts.registry import REGISTRY

        monkeypatch.setenv("TMM_PROMPT_PINS", "extraction=v1")
        assert REGISTRY.get("extraction").version == "v1"

    def test_an_unknown_pin_raises_rather_than_falling_back(self, monkeypatch) -> None:
        """Quietly serving a different prompt than an operator asked for during
        a rollback is worse than a loud failure."""
        from tutor_match_meta.prompts.registry import REGISTRY

        monkeypatch.setenv("TMM_PROMPT_PINS", "extraction=v99")
        with pytest.raises(KeyError, match="v99"):
            REGISTRY.get("extraction")

    def test_a_checksum_changes_when_the_text_changes(self) -> None:
        from tutor_match_meta.prompts.registry import PromptTemplate

        base = PromptTemplate(prompt_id="p", version="v1", stable_prefix="hello")
        edited = PromptTemplate(prompt_id="p", version="v1", stable_prefix="hello!")
        assert base.checksum != edited.checksum

    def test_render_puts_the_stable_prefix_first(self) -> None:
        """Inverting this destroys the provider cache hit rate silently."""
        from tutor_match_meta.prompts.registry import PromptTemplate

        template = PromptTemplate(
            prompt_id="p", version="v1", stable_prefix="RULES", variable_suffix="\n{ctx}"
        )
        assert template.render(ctx="today").startswith("RULES")


class TestCompositionRoot:
    """The build locks. Both properties here were real deadlocks."""

    async def test_nested_builders_do_not_deadlock(self) -> None:
        """`build_handoff_service` awaits `build_turn_service`.

        With one shared lock the outer builder held it while the inner one
        waited for it — `asyncio.Lock` is not reentrant — and every cold start
        of the internal API hung until the function timed out. Lead Intake
        would have seen a 504 on the first request to every new container.
        """
        import asyncio

        from tutor_match_meta.bootstrap import build_handoff_service, reset_singletons
        from tutor_match_meta.config.settings import get_settings

        reset_singletons()
        service, pseudonymiser = await asyncio.wait_for(
            build_handoff_service(get_settings()), timeout=10
        )
        assert service is not None
        assert pseudonymiser is not None

    async def test_a_second_call_returns_the_same_instance(self) -> None:
        """Per *request* construction is what exhausted the connection pool."""
        from tutor_match_meta.bootstrap import build_turn_service, reset_singletons
        from tutor_match_meta.config.settings import get_settings

        reset_singletons()
        first, _ = await build_turn_service(get_settings())
        second, _ = await build_turn_service(get_settings())
        assert first is second

    async def test_concurrent_cold_starts_build_once(self) -> None:
        import asyncio

        from tutor_match_meta.bootstrap import build_turn_service, reset_singletons
        from tutor_match_meta.config.settings import get_settings

        reset_singletons()
        settings = get_settings()
        results = await asyncio.gather(*(build_turn_service(settings) for _ in range(8)))
        assert len({id(service) for service, _ in results}) == 1

    async def test_the_lock_survives_a_new_event_loop(self) -> None:
        """`asyncio.run()` creates a fresh loop on every Lambda invocation.

        A module-level lock binds to the first loop and breaks on every one
        after it — a failure that only shows up on the *second* cold start.
        """
        import asyncio

        from tutor_match_meta.bootstrap import build_turn_service, reset_singletons
        from tutor_match_meta.config.settings import get_settings

        def one_invocation() -> object:
            reset_singletons()
            return asyncio.run(build_turn_service(get_settings()))[0]

        # Two separate loops, exactly as two Lambda invocations would be.
        assert await asyncio.to_thread(one_invocation) is not None
        assert await asyncio.to_thread(one_invocation) is not None


def _row(key: str, *, kind: str = OUTBOX_KIND_REPLY, attempts: int = 0) -> OutboxMessage:
    delivery = OutboundDeliveryV1(
        kind=kind,
        recipient="+919876543210" if kind == OUTBOX_KIND_REPLY else None,
        body="here are 3 tutors",
        dedup_key=key,
        trace_id="t",
    )
    return OutboxMessage(
        kind=kind,
        conversation_id="c1",
        payload=delivery.to_payload(),
        dedup_key=key,
        trace_id="t",
        attempts=attempts,
    )


class TestRelayDecisions:
    def test_a_deliverable_row_parses_as_deliverable(self) -> None:
        assert _delivery(_row("d1")).deliverable

    def test_an_audit_row_parses_as_not_deliverable(self) -> None:
        delivery = _delivery(_row("d2", kind=OUTBOX_KIND_CALLER_REPLY))
        assert not delivery.deliverable
        assert delivery.body, "the audit record must still carry what was said"

    def test_a_row_from_an_older_deployment_is_rejected_not_guessed(self) -> None:
        from pydantic import ValidationError

        stale = OutboxMessage(
            kind=OUTBOX_KIND_REPLY,
            conversation_id="c1",
            # The pre-hardening shape: no recipient, no body.
            payload={"text": "hello", "conversation_hash": "cv_abc"},
            dedup_key="old",
            trace_id="t",
        )
        with pytest.raises(ValidationError):
            _delivery(stale)

    def test_backoff_is_capped_not_unbounded(self) -> None:
        now = datetime.now(UTC)
        delays = [(_retry_at(n, now) - now).total_seconds() for n in range(10)]
        assert delays == sorted(delays), "backoff must be monotonic"
        assert max(delays) <= 3600, "backoff must be capped"

    def test_the_claim_lease_exceeds_the_function_timeout(self) -> None:
        """A lease shorter than the Lambda timeout would reclaim rows a live
        relay is still working on — and send them twice."""
        assert CLAIM_LEASE_SECONDS > 300


class TestOutboxLeaseSemantics:
    """The in-memory store models the SQL lease, so these assertions transfer."""

    async def test_a_claim_removes_the_row_from_the_pending_set(self) -> None:
        outbox = InMemoryOutbox()
        await outbox.enqueue(_row("d1"))
        claimed = await outbox.claim_batch(limit=10, now=datetime.now(UTC))
        assert [m.dedup_key for m in claimed] == ["d1"]
        assert await outbox.claim_batch(limit=10, now=datetime.now(UTC)) == []

    async def test_two_relays_cannot_claim_the_same_row(self) -> None:
        import asyncio

        outbox = InMemoryOutbox()
        for index in range(6):
            await outbox.enqueue(_row(f"d{index}"))
        batches = await asyncio.gather(
            *(outbox.claim_batch(limit=6, now=datetime.now(UTC)) for _ in range(3))
        )
        claimed = [m.dedup_key for batch in batches for m in batch]
        assert sorted(claimed) == sorted(set(claimed))
        assert len(claimed) == 6

    async def test_an_abandoned_lease_is_reclaimed(self) -> None:
        outbox = InMemoryOutbox()
        await outbox.enqueue(_row("d1"))
        await outbox.claim_batch(limit=10, now=datetime.now(UTC))
        reclaimed = await outbox.reclaim_stale(older_than=datetime.now(UTC) + timedelta(hours=1))
        assert reclaimed == 1
        assert len(await outbox.claim_batch(limit=10, now=datetime.now(UTC))) == 1

    async def test_five_failures_reach_dead_rather_than_retrying_forever(self) -> None:
        outbox = InMemoryOutbox()
        await outbox.enqueue(_row("d1"))
        past = datetime.now(UTC) - timedelta(hours=1)
        for _ in range(5):
            await outbox.claim_batch(limit=10, now=datetime.now(UTC))
            await outbox.mark_failed("d1", error="sqs down", retry_at=past)
        assert [m.dedup_key for m in outbox.dead] == ["d1"]
        assert await outbox.claim_batch(limit=10, now=datetime.now(UTC)) == []

    async def test_a_dead_row_is_never_claimed_again(self) -> None:
        outbox = InMemoryOutbox()
        await outbox.enqueue(_row("d1"))
        await outbox.claim_batch(limit=10, now=datetime.now(UTC))
        await outbox.mark_dead("d1", error="unaddressable")
        assert await outbox.claim_batch(limit=10, now=datetime.now(UTC)) == []

    async def test_enqueue_is_idempotent_across_every_state(self) -> None:
        outbox = InMemoryOutbox()
        await outbox.enqueue(_row("d1"))
        await outbox.claim_batch(limit=10, now=datetime.now(UTC))
        await outbox.mark_delivered("d1")
        await outbox.enqueue(_row("d1"))  # a redelivery after success
        assert await outbox.claim_batch(limit=10, now=datetime.now(UTC)) == []
