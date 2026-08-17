"""Composition root.

The only module that knows which concrete adapter backs which port. Everything
else depends on Protocols, which is what lets the whole matching pipeline run in
tests and in `make e2e` with no infrastructure.

Two rules govern this file.

**Degrade, never crash on startup.** A missing OpenAI key means the stub
provider, not a boot failure; write-back off means a gateway that refuses
loudly. A Lambda that will not initialise cannot even report why.

**Build once per container, not once per request.** Every expensive object here
is cached at module scope, guarded by a per-event-loop `asyncio.Lock` for the
async ones. The
previous version constructed a fresh `AsyncEngine` — and therefore a fresh
connection pool that was never disposed — on *every* call to
`build_handoff_service`, which the FastAPI route calls per request. Under any
sustained traffic that exhausts RDS Proxy's connection limit and takes the
database down for every other service sharing it. Caching is not an
optimisation here; it is the difference between working and not.

Warm-container caching is safe because `get_settings()` is itself
`lru_cache`d, so configuration cannot change under a cached object without a
new container.
"""

from __future__ import annotations

import asyncio
import weakref
from pathlib import Path
from typing import Any

from tutor_match_meta.cache import Cache, build_cache
from tutor_match_meta.config.settings import Settings, get_settings
from tutor_match_meta.integrations.llm import build_provider
from tutor_match_meta.integrations.llm.routing import ModelRouting, routing_from_settings
from tutor_match_meta.integrations.website.gateway import WebsiteCommandGateway, build_gateway
from tutor_match_meta.integrations.whatsapp.outbound import WhatsAppSender, build_sender
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.orchestration.orchestrator import TutorMatchOrchestrator
from tutor_match_meta.orchestration.turn_service import TurnDependencies, TurnService
from tutor_match_meta.repositories.memory_store import (
    InMemoryConversationStore,
    InMemoryDecisionStore,
    InMemoryIdempotencyStore,
    InMemoryOutbox,
    InMemoryRequirementStore,
    InMemoryTutorRepository,
    NullMemory,
)
from tutor_match_meta.scoring.policy import PolicyRegistry, _resolve_policy_dir
from tutor_match_meta.security.pii import Pseudonymiser
from tutor_match_meta.security.rate_limit import (
    InMemoryBucketStore,
    LayeredRateLimiter,
    policies_from_settings,
)
from tutor_match_meta.security.urls import build_policy

logger = get_logger("bootstrap")

# ------------------------------------------------------- container-lifetime state
#
# Keyed by nothing: `get_settings()` is process-wide, so one container has
# exactly one of each.
_singletons: dict[str, Any] = {}

#: Build locks, keyed by **(event loop, singleton name)**. Both halves of that
#: key are load-bearing, and each was a real deadlock.
#:
#: *Per loop*, because a module-level `asyncio.Lock` binds to the first loop
#: that awaits it and then raises or hangs on every later one — and
#: `handlers/lambda_entry._run` calls `asyncio.run()`, creating a fresh loop on
#: every single invocation. A process-wide lock works exactly once.
#:
#: *Per name*, because `asyncio.Lock` is **not reentrant** and the builders
#: nest: `build_handoff_service` awaits `build_turn_service`. One shared lock
#: meant the outer builder held it while the inner one waited for it, forever.
#: That deadlocked every cold start of the internal API — the Lambda Lead
#: Intake calls — until the function timed out.
#:
#: A `WeakKeyDictionary` on the loop keeps this from leaking: when a loop is
#: closed and collected, its locks go with it.
_build_locks: weakref.WeakKeyDictionary[Any, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()


def _build_lock(name: str) -> asyncio.Lock:
    """The build lock for `name` on the currently running loop.

    Serialises concurrent first-builds of one singleton; without it two
    coroutines racing a cold start would both create an engine, and the loser's
    would be orphaned with its connections still checked out.
    """
    loop = asyncio.get_running_loop()
    locks = _build_locks.get(loop)
    if locks is None:
        locks = {}
        _build_locks[loop] = locks
    lock = locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        locks[name] = lock
    return lock


def reset_singletons() -> None:
    """Drop every cached object. Tests only — never called in production.

    Deliberately does *not* dispose engines: the tests that use it never open
    one, and adding an await here would make this unusable from a sync fixture.
    """
    _singletons.clear()


def _cached(key: str, factory: Any) -> Any:
    if key not in _singletons:
        _singletons[key] = factory()
    return _singletons[key]


# ------------------------------------------------------------------- primitives
def build_policy_registry(settings: Settings) -> PolicyRegistry:
    """Versioned scoring policies. Cached: they are read-only YAML."""
    registry: PolicyRegistry = _cached(
        "policies",
        lambda: PolicyRegistry(
            _resolve_policy_dir(Path(settings.policy_dir)), settings.default_policy
        ),
    )
    return registry


def build_pseudonymiser(settings: Settings) -> Pseudonymiser:
    def make() -> Pseudonymiser:
        pepper = settings.hash_pepper.get_secret_value()
        if not pepper:
            if settings.is_deployed:  # pragma: no cover - settings validation catches this
                raise RuntimeError("hash_pepper is required outside local")
            # Local only: a fixed pepper keeps hashes stable across a dev restart.
            pepper = "local-development-pepper"
        return Pseudonymiser(pepper)

    return _cached("pseudonymiser", make)  # type: ignore[no-any-return]


def build_url_policy(settings: Settings) -> Any:
    return build_policy(
        settings.website_api_base_url,
        settings.chitragupta_base_url,
        settings.geocoder_base_url,
        allow_local=not settings.is_deployed,
    )


def build_model_routing(settings: Settings) -> ModelRouting:
    return _cached("routing", lambda: routing_from_settings(settings))  # type: ignore[no-any-return]


def build_llm(settings: Settings) -> Any:
    """The raw provider. Callers wrap it in `GuardedProvider` per turn.

    Cached because the OpenAI client holds a connection pool and a circuit
    breaker — a per-turn client would reset the breaker on every message, which
    is exactly the behaviour a breaker exists to prevent.
    """

    def make() -> Any:
        routing = build_model_routing(settings)
        return build_provider(
            provider=settings.llm_provider,
            api_key=settings.openai_api_key.get_secret_value(),
            tier1_model=routing.extraction,
            tier2_model=routing.escalation,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            max_output_tokens=settings.llm_max_output_tokens,
            store_responses=settings.llm_store_responses,
        )

    return _cached("llm", make)


def build_sender_from_settings(settings: Settings) -> WhatsAppSender:
    return _cached(  # type: ignore[no-any-return]
        "sender",
        lambda: build_sender(
            enabled=settings.whatsapp_enabled,
            phone_number_id=settings.whatsapp_phone_number_id,
            access_token=settings.whatsapp_access_token.get_secret_value(),
            api_version=settings.whatsapp_api_version,
        ),
    )


def build_website_gateway(settings: Settings) -> WebsiteCommandGateway:
    return _cached(  # type: ignore[no-any-return]
        "website",
        lambda: build_gateway(
            enabled=settings.website_write_enabled,
            base_url=settings.website_api_base_url,
            signing_key=settings.website_api_signing_key.get_secret_value(),
            url_policy=build_url_policy(settings),
        ),
    )


def build_memory(settings: Settings) -> Any:
    def make() -> Any:
        if not settings.chitragupta_enabled or not settings.chitragupta_base_url:
            logger.info("chitragupta disabled; memory recall and deeds are no-ops")
            return NullMemory()
        from tutor_match_meta.integrations.chitragupta.client import ChitraguptaMemory, FileWal

        return ChitraguptaMemory(
            base_url=settings.chitragupta_base_url,
            api_key=settings.chitragupta_api_key.get_secret_value(),
            agent_id=settings.chitragupta_agent_id,
            environment=settings.environment.value,
            timeout_seconds=settings.chitragupta_timeout_seconds,
            # /tmp is the only writable path in Lambda, and it is per-container —
            # so the WAL is best-effort and the relay job drains it.
            wal=FileWal(Path("/tmp/tmm-memory.wal")),  # noqa: S108
        )

    return _cached("memory", make)


# ------------------------------------------------------------- shared PostgreSQL
def _sessions(settings: Settings) -> Any | None:
    """One sessionmaker per container, or None when there is no DSN.

    This is the object whose duplication caused connection exhaustion. It is
    now built exactly once and shared by the repositories, the rate-limit
    store, the kill switches, the cache and analytics.
    """

    def make() -> Any:
        if not settings.postgres_dsn:
            return None
        if settings.network_zone == "internet":
            # This function is outside the VPC. There is no route to RDS Proxy,
            # so an engine here would resolve, connect, and hang until the
            # socket timeout on the first query of every cold container.
            logger.info("network_zone=internet: no database session factory built")
            return None
        from tutor_match_meta.repositories.postgres import build_sessions, create_engine

        return build_sessions(create_engine(settings))

    return _cached("sessions", make)


def database_sessions(settings: Settings) -> Any | None:
    """Public accessor for the shared sessionmaker.

    Exposed so the readiness probe and the doctor can verify the schema
    without reaching into a private helper — and, more importantly, without
    creating a second engine to do it.
    """
    return _sessions(settings)


def _shared_stores(settings: Settings) -> tuple[Any, Any, Any]:
    """The PostgreSQL shared store, or in-process doubles when there is no DSN.

    Cross-container rate limiting and kill switches need shared state. There is
    no Redis; PostgreSQL is already on the path and scales to zero.

    **Keyed on the DSN, not on `cache_backend`.** Those are different questions
    and conflating them was a silent failure: with `TMM_CACHE_BACKEND=memory`
    and a perfectly good database configured, this returned `NoKillSwitches`,
    so all seven emergency controls did nothing and the per-conversation rate
    limit degraded to `N_containers × limit`. Nothing logged an error, because
    from the code's point of view that was a valid configuration.

    `cache_backend` now controls only the *cache tier* — which is an
    optimisation. Rate limits and kill switches are correctness-critical, so
    they follow the database.
    """

    def make() -> tuple[Any, Any, Any]:
        sessions = _sessions(settings)
        if sessions is None:
            from tutor_match_meta.config.kill_switches import NoKillSwitches

            logger.warning(
                "no PostgreSQL DSN: rate limits are per-container and kill "
                "switches are inert. Acceptable locally, never in a deployment."
            )
            return None, InMemoryBucketStore(), NoKillSwitches()
        from tutor_match_meta.cache.postgres_store import build_shared_stores

        return build_shared_stores(sessions, schema=settings.postgres_schema)

    return _cached("shared_stores", make)  # type: ignore[no-any-return]


def build_cache_for(settings: Settings) -> Cache:
    """L1-over-L2 cache. One per container so the L1 actually stays warm."""
    shared, _buckets, _switches = _shared_stores(settings)
    return _cached("cache", lambda: build_cache(settings.cache_backend, shared))  # type: ignore[no-any-return]


def build_limiter(settings: Settings) -> LayeredRateLimiter:
    _shared, buckets, _switches = _shared_stores(settings)
    return _cached(  # type: ignore[no-any-return]
        "limiter", lambda: LayeredRateLimiter(buckets, policies_from_settings(settings))
    )


def kill_switches(settings: Settings) -> Any:
    """Operator emergency flags, cached with their own short TTL inside."""

    def make() -> Any:
        from tutor_match_meta.config.kill_switches import KillSwitches

        _shared, _buckets, switches = _shared_stores(settings)
        return KillSwitches(switches)

    return _cached("kill_switches", make)


#: Kept for callers that predate the rename.
build_kill_switches = kill_switches


def build_analytics(settings: Settings) -> Any:
    def make() -> Any:
        from tutor_match_meta.analytics import (
            LoggingAnalytics,
            NullAnalytics,
            PostgresAnalytics,
        )

        if not settings.analytics_enabled:
            return NullAnalytics()
        sessions = _sessions(settings)
        if sessions is None:
            return LoggingAnalytics()
        return PostgresAnalytics(sessions, schema=settings.postgres_schema)

    return _cached("analytics", make)


def build_geocoder_for(settings: Settings) -> Any:
    """A geocoder per turn would lose its per-turn budget; per container keeps
    the cache warm, and the budget is reset explicitly by the turn service."""
    from tutor_match_meta.integrations.geo import build_geocoder

    return build_geocoder(settings, sessions=_sessions(settings), cache=build_cache_for(settings))


# ----------------------------------------------------------------------- ingress
def build_ingress_service(settings: Settings) -> Any:
    def make() -> Any:
        from tutor_match_meta.handlers.ingress import IngressConfig, IngressService

        config = IngressConfig(
            signing_key=settings.ingress_signing_key.get_secret_value(),
            timestamp_tolerance_seconds=settings.ingress_timestamp_tolerance_seconds,
            max_body_bytes=settings.ingress_max_body_bytes,
            per_conversation_per_minute=settings.rate_limit_per_conversation_per_minute,
            global_per_minute=settings.rate_limit_global_per_minute,
        )
        return IngressService(
            config=config,
            limiter=build_limiter(settings),
            pseudonymiser=build_pseudonymiser(settings),
            enqueue=_build_enqueue(settings),
            switches=kill_switches(settings),
        )

    return _cached("ingress", make)


def _build_enqueue(settings: Settings) -> Any:
    """Ingress publishes to the enrich queue, not the match queue.

    Enrichment comes first because requirement extraction needs OpenAI, and the
    only functions with a route to OpenAI are the ones outside the VPC. Falling
    back to the match queue keeps a deployment that has not been migrated yet
    working — degraded to deterministic extraction rather than broken.
    """
    return _build_sqs_publisher(
        settings,
        settings.enrich_queue_url or settings.match_queue_url,
        name="enrich" if settings.enrich_queue_url else "match",
    )


def build_match_enqueue(settings: Settings) -> Any:
    """The enrich worker's output hop: enriched envelope -> match queue."""
    return _build_sqs_publisher(settings, settings.match_queue_url, name="match")


def _build_sqs_publisher(settings: Settings, queue_url: str, *, name: str) -> Any:
    """Return an async `enqueue(envelope)` for one FIFO queue.

    Falls back to logging when no queue is configured, so a local run still
    demonstrates the full request path without AWS.
    """
    if not queue_url:

        async def log_only(envelope: Any) -> None:
            logger.info(
                "queue not configured; envelope not enqueued",
                extra={"tmm_queue": name, "tmm_kind": str(envelope.kind)},
            )

        return log_only

    import boto3

    client = _cached("sqs", lambda: boto3.client("sqs", region_name=settings.aws_region))

    async def enqueue(envelope: Any) -> None:
        from tutor_match_meta.handlers.ingress import sqs_message_from

        client.send_message(QueueUrl=queue_url, **sqs_message_from(envelope))

    return enqueue


# ----------------------------------------------------------------- enrich worker
def build_enrichment_service(settings: Settings) -> Any:
    """Assemble the internet-side worker. Built once per container.

    Deliberately builds **no** session factory and takes **no** repository: this
    function runs outside the VPC and has no route to PostgreSQL. Everything it
    needs is either in the envelope or reachable over the public internet.
    """

    def make() -> Any:
        from tutor_match_meta.orchestration.enrichment import (
            EnrichmentDependencies,
            EnrichmentService,
        )

        return EnrichmentService(
            EnrichmentDependencies(
                pseudonymiser=build_pseudonymiser(settings),
                llm=build_llm(settings),
                routing=build_model_routing(settings),
                memory=build_memory(settings),
                # Both the limiter and the switches fall back to in-process
                # doubles without a DSN, which is exactly what happens here —
                # see `_shared_stores`. The durable copies are enforced on the
                # database side; these bound one container's own spend.
                limiter=build_limiter(settings),
                switches=kill_switches(settings),
                budget=_budget_settings(settings),
            )
        )

    return _cached("enrichment", make)


# ------------------------------------------------------------------ match worker
async def build_turn_service(settings: Settings) -> tuple[TurnService, Pseudonymiser]:
    """Assemble the match worker. Built once per container.

    PostgreSQL-backed repositories are used when a DSN is configured; otherwise
    the in-memory ones keep the worker runnable for local demos.
    """
    cached = _singletons.get("turn_service")
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    async with _build_lock("turn_service"):
        # Re-check inside the lock: two coroutines can both miss the fast path.
        cached = _singletons.get("turn_service")
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        registry = build_policy_registry(settings)
        pseudonymiser = build_pseudonymiser(settings)
        tutors, conversations, requirements, decisions, idempotency, outbox = await _build_stores(
            settings
        )

        # The pool cache sits *outside* the repository, so the repository stays
        # a plain reader and the cache can be removed by deleting one line.
        # Only wrapped against a real database: caching the in-memory fixture
        # store would measure the cache's own overhead and nothing else.
        if _sessions(settings) is not None:
            from tutor_match_meta.repositories.cached_tutors import CachedTutorRepository

            tutors = CachedTutorRepository(
                tutors,
                build_cache_for(settings),
                policy_ref=settings.default_policy,
                fresh_hours=settings.projection_fresh_hours,
                aging_hours=settings.projection_aging_hours,
            )

        orchestrator = TutorMatchOrchestrator(
            tutors=tutors,
            policies=registry,
            public_base_url=settings.website_public_base_url,
        )
        deps = TurnDependencies(
            orchestrator=orchestrator,
            conversations=conversations,
            requirements=requirements,
            decisions=decisions,
            idempotency=idempotency,
            outbox=outbox,
            # A `NullMemory` in the VPC zone: `_enforce_network_boundary`
            # refuses `chitragupta_enabled` there, so `build_memory` cannot
            # return a live HTTP client. Deeds leave through the outbox.
            memory=build_memory(settings),
            pseudonymiser=pseudonymiser,
            # `None` in the VPC zone, and that is the whole point. This worker
            # has no route to api.openai.com, so wiring a provider would hang
            # every turn until the client timeout and then degrade silently.
            # The internet-side enrich worker already extracted the
            # requirement; it arrives on the envelope (contracts/enrichment.py).
            llm=build_llm(settings) if settings.network_zone != "vpc" else None,
            routing=build_model_routing(settings),
            limiter=build_limiter(settings),
            switches=kill_switches(settings),
            analytics=build_analytics(settings),
            cache=build_cache_for(settings),
            budget=_budget_settings(settings),
            record_deeds=settings.chitragupta_enabled,
        )
        built = (TurnService(deps), pseudonymiser)
        _singletons["turn_service"] = built
        return built


def _budget_settings(settings: Settings) -> dict[str, int]:
    """Every LLM ceiling in one dict, so no handler holds a business number."""
    return {
        "tokens": settings.llm_conversation_token_budget,
        "calls_per_turn": settings.llm_max_calls_per_turn,
        "calls_per_conversation": settings.llm_max_calls_per_conversation,
        "escalations": settings.llm_max_escalations_per_conversation,
    }


async def _build_stores(settings: Settings) -> tuple[Any, ...]:
    """PostgreSQL when a DSN is configured; in-memory fixtures otherwise.

    **The gate is the DSN, not the environment name.** It used to also require
    `settings.is_deployed`, which meant an operator could point
    `TMM_POSTGRES_DSN` at a real, migrated database, run with
    `TMM_ENVIRONMENT=local`, and silently get the synthetic fixture tutors —
    with every table sitting empty and no error to explain why. "I gave you a
    database" is an unambiguous instruction to use it.

    The old warning claimed "no PostgreSQL DSN configured" while a DSN was
    configured, which made the symptom actively misleading.
    """
    sessions = _sessions(settings)
    if sessions is not None:
        from tutor_match_meta.repositories.postgres import build_postgres_stores

        logger.info("using PostgreSQL stores", extra={"tmm_schema": settings.postgres_schema})
        return await build_postgres_stores(settings, sessions=sessions)

    if settings.is_deployed:
        # A deployed environment with no database cannot record a decision, and
        # a recommendation we cannot audit is one we do not make.
        raise RuntimeError(
            "TMM_POSTGRES_DSN is required outside local: without it no decision "
            "can be persisted, and an unauditable shortlist must not be sent."
        )

    logger.warning("no PostgreSQL DSN; using in-memory stores and synthetic tutors")
    from tutor_match_meta.fixtures.tutors import sample_tutors

    return (
        InMemoryTutorRepository(sample_tutors()),
        InMemoryConversationStore(),
        InMemoryRequirementStore(),
        InMemoryDecisionStore(),
        InMemoryIdempotencyStore(),
        InMemoryOutbox(),
    )


# ---------------------------------------------------------------------- handoff
async def build_handoff_service(settings: Settings) -> tuple[Any, Pseudonymiser]:
    """Assemble the Lead Intake handoff service. Built once per container.

    Reuses the same turn service the SQS worker uses, so a handoff and a queued
    message take an identical path through matching — two code paths would drift
    and produce different shortlists for the same requirement.
    """
    cached = _singletons.get("handoff_service")
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    async with _build_lock("handoff_service"):
        cached = _singletons.get("handoff_service")
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        from tutor_match_meta.config.flags import from_settings
        from tutor_match_meta.contracts.handoff import OutboundOwnership
        from tutor_match_meta.orchestration.continuation import ContinuationCodec
        from tutor_match_meta.orchestration.handoff_service import (
            HandoffDependencies,
            HandoffService,
        )

        turn_service, pseudonymiser = await build_turn_service(settings)
        signing_key = settings.continuation_signing_key.get_secret_value()
        if not signing_key:
            if settings.is_deployed:
                raise RuntimeError("continuation_signing_key is required outside local")
            signing_key = "local-development-continuation-key"

        service = HandoffService(
            HandoffDependencies(
                turn_service=turn_service,
                flags=from_settings(settings),
                continuations=ContinuationCodec(signing_key),
                pseudonymiser=pseudonymiser,
                outbound_ownership=OutboundOwnership(settings.outbound_ownership),
                account_required_for_match=settings.account_required_for_match,
                switches=kill_switches(settings),
            )
        )
        built = (service, pseudonymiser)
        _singletons["handoff_service"] = built
        return built


# ------------------------------------------------------------------- local stack
def build_local_stack() -> tuple[TurnService, TurnDependencies]:
    """Fully in-memory stack for `make e2e` and the doctor command."""
    from tutor_match_meta.analytics import RecordingAnalytics
    from tutor_match_meta.cache import InMemoryCache
    from tutor_match_meta.config.kill_switches import KillSwitches, NoKillSwitches
    from tutor_match_meta.fixtures.tutors import sample_tutors

    settings = get_settings()
    registry = build_policy_registry(settings)
    deps = TurnDependencies(
        orchestrator=TutorMatchOrchestrator(
            tutors=InMemoryTutorRepository(sample_tutors()),
            policies=registry,
            public_base_url=settings.website_public_base_url,
        ),
        conversations=InMemoryConversationStore(),
        requirements=InMemoryRequirementStore(),
        decisions=InMemoryDecisionStore(),
        idempotency=InMemoryIdempotencyStore(),
        outbox=InMemoryOutbox(),
        memory=NullMemory(),
        pseudonymiser=build_pseudonymiser(settings),
        llm=build_llm(settings),
        routing=build_model_routing(settings),
        limiter=LayeredRateLimiter(InMemoryBucketStore(), policies_from_settings(settings)),
        switches=KillSwitches(NoKillSwitches()),
        analytics=RecordingAnalytics(),
        cache=InMemoryCache(),
        budget=_budget_settings(settings),
    )
    return TurnService(deps), deps
