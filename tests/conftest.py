"""Shared fixtures.

Everything is wired from in-memory adapters, so the whole suite runs with no
database, no AWS and no API key. The adapters implement the same semantics as
the real ones (see `repositories/memory_store.py`), so these are integration
tests of the logic even though they need no infrastructure.

**The suite must never touch real infrastructure.** A developer's `.env` points
at the live shared RDS and holds a real OpenAI key; without the isolation
below, importing anything that calls `get_settings()` would have the test suite
open connections to production and spend money. The environment is neutralised
at *import* time rather than in a fixture, because `pydantic-settings` reads
`.env` the first time a `Settings` is constructed — which can happen during
collection, before any fixture runs.

Environment variables outrank `.env` in pydantic-settings, so setting them here
is sufficient. `tests/integration/` opts back in explicitly through its own
`TMM_INTEGRATION_DSN`, which is a deliberately different variable name.
"""

from __future__ import annotations

import os

_SAFE_ENVIRONMENT = {
    "TMM_ENVIRONMENT": "local",
    # No DSN => in-memory stores, and no engine is ever created.
    "TMM_POSTGRES_DSN": "",
    "TMM_CACHE_BACKEND": "memory",
    # No provider call, ever, from a test run.
    "TMM_LLM_PROVIDER": "stub",
    "TMM_OPENAI_API_KEY": "",
    "TMM_CHITRAGUPTA_ENABLED": "false",
    "TMM_WHATSAPP_ENABLED": "false",
    "TMM_OUTBOUND_OWNERSHIP": "caller_sends",
    "TMM_WEBSITE_WRITE_ENABLED": "false",
    "TMM_MATCH_QUEUE_URL": "",
    "TMM_OUTBOUND_QUEUE_URL": "",
    "TMM_GEOCODER": "disabled",
    "TMM_HASH_PEPPER": "test-pepper-not-a-real-secret",
    "TMM_INGRESS_SIGNING_KEY": "test-ingress-key",
    "TMM_CONTINUATION_SIGNING_KEY": "test-continuation-key",
    "TMM_PROMPT_PINS": "",
}
os.environ.update(_SAFE_ENVIRONMENT)

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tutor_match_meta.analytics import RecordingAnalytics
from tutor_match_meta.cache import InMemoryCache
from tutor_match_meta.config.kill_switches import KillSwitches, NoKillSwitches
from tutor_match_meta.contracts.common import Provenance, Tracked, TuitionMode
from tutor_match_meta.contracts.requirement import LocationRequirement, MatchRequirementV1
from tutor_match_meta.fixtures.tutors import sample_tutors
from tutor_match_meta.matching.base import EvaluationContext
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
from tutor_match_meta.scoring.policy import PolicyRegistry, ScoringPolicy
from tutor_match_meta.security.pii import Pseudonymiser
from tutor_match_meta.security.rate_limit import (
    InMemoryBucketStore,
    LayeredRateLimiter,
    LimitPolicy,
    LimitScope,
)

PUBLIC_BASE_URL = "https://www.nxtutors.com"
POLICY_DIR = Path(__file__).resolve().parents[1] / "config" / "policies"


@pytest.fixture(scope="session")
def policy_dir() -> Path:
    assert POLICY_DIR.is_dir(), f"policy directory missing at {POLICY_DIR}"
    return POLICY_DIR


@pytest.fixture
def registry(policy_dir: Path) -> PolicyRegistry:
    return PolicyRegistry(policy_dir, "regular_school_support.v1")


@pytest.fixture
def policy(registry: PolicyRegistry) -> ScoringPolicy:
    return registry.get("regular_school_support.v1")


@pytest.fixture
def now() -> datetime:
    # Fixed so recency decay and freshness are reproducible across runs.
    return datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def tutors(now: datetime) -> list:
    return sample_tutors(now)


@pytest.fixture
def tutor_repo(tutors: list) -> InMemoryTutorRepository:
    return InMemoryTutorRepository(tutors)


@pytest.fixture
def orchestrator(
    tutor_repo: InMemoryTutorRepository, registry: PolicyRegistry
) -> TutorMatchOrchestrator:
    return TutorMatchOrchestrator(
        tutors=tutor_repo, policies=registry, public_base_url=PUBLIC_BASE_URL
    )


def make_requirement(
    *,
    conversation_id: str = "conv-1",
    subject: str | None = "Mathematics",
    board: str | None = "CBSE",
    student_class: str | None = "Class 10",
    mode: TuitionMode | None = TuitionMode.HOME,
    city: str | None = "Gurugram",
    locality: str | None = "Sector 57",
    pincode: str | None = "122003",
    provenance: Provenance = Provenance.DETERMINISTIC,
    confidence: float = 0.92,
    **extra: object,
) -> MatchRequirementV1:
    """A well-formed requirement. Override any field per test."""

    def tracked(value: object) -> Tracked | None:
        if value is None:
            return None
        return Tracked(value=value, confidence=confidence, provenance=provenance)

    # An explicit `location=` overrides the city/locality/pincode shorthand, so
    # a test needing coordinates does not have to fight the defaults.
    location = extra.pop(
        "location", LocationRequirement(city=city, locality=locality, pincode=pincode)
    )
    return MatchRequirementV1(
        conversation_id=conversation_id,
        subject=tracked(subject),
        board=tracked(board),
        student_class=tracked(student_class),
        mode=tracked(mode),
        location=location,  # type: ignore[arg-type]
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def requirement() -> MatchRequirementV1:
    return make_requirement()


@pytest.fixture
def context(
    requirement: MatchRequirementV1, policy: ScoringPolicy, now: datetime
) -> EvaluationContext:
    return EvaluationContext(requirement=requirement, policy=policy, now=now)


@pytest.fixture
def pseudonymiser() -> Pseudonymiser:
    return Pseudonymiser("test-pepper-not-a-real-secret")


@pytest.fixture
def cache() -> InMemoryCache:
    return InMemoryCache()


@pytest.fixture
def analytics() -> RecordingAnalytics:
    return RecordingAnalytics()


@pytest.fixture
def turn_deps(
    orchestrator: TutorMatchOrchestrator,
    pseudonymiser: Pseudonymiser,
    analytics: RecordingAnalytics,
) -> TurnDependencies:
    """The default worker wiring: every guard present, nothing paused.

    The guards are wired here rather than left as `None` so the standard suite
    exercises the same code path production takes. Tests that need a guard to
    *fire* replace that one dependency; tests that need it absent set it to
    None explicitly, which is then visible in the test rather than inherited.
    """
    return TurnDependencies(
        orchestrator=orchestrator,
        conversations=InMemoryConversationStore(),
        requirements=InMemoryRequirementStore(),
        decisions=InMemoryDecisionStore(),
        idempotency=InMemoryIdempotencyStore(),
        outbox=InMemoryOutbox(),
        memory=NullMemory(),
        pseudonymiser=pseudonymiser,
        limiter=LayeredRateLimiter(
            InMemoryBucketStore(),
            {
                LimitScope.LLM: LimitPolicy(per_minute=4),
                LimitScope.CONVERSATION: LimitPolicy(per_minute=12),
            },
        ),
        switches=KillSwitches(NoKillSwitches()),
        analytics=analytics,
        cache=InMemoryCache(),
    )


@pytest.fixture
def turn_service(turn_deps: TurnDependencies) -> TurnService:
    return TurnService(turn_deps)


@pytest.fixture(autouse=True)
def _quiet_logging() -> Iterator[None]:
    """Keep test output readable without disabling the logging code path."""
    import logging

    logging.getLogger("tutor_match_meta").setLevel(logging.CRITICAL)
    yield
    logging.getLogger("tutor_match_meta").setLevel(logging.INFO)


@pytest.fixture(autouse=True)
def _isolated_singletons() -> Iterator[None]:
    """No composition-root state leaks between tests.

    `bootstrap` caches an engine, a provider and a service per container. A
    test that builds one must not hand it to the next test — and more
    importantly, a cached object built from one test's monkeypatched settings
    must not survive into another's.
    """
    from tutor_match_meta.bootstrap import reset_singletons

    reset_singletons()
    yield
    reset_singletons()


def test_the_suite_cannot_reach_real_infrastructure() -> None:
    """A guard, not a fixture: if this fails, stop and fix the environment.

    Lives in conftest so it runs regardless of which subset is selected. The
    failure it prevents is a developer running `pytest` with a production
    `.env` and having the suite write to the shared RDS.
    """
    from tutor_match_meta.config.settings import get_settings

    settings = get_settings()
    assert settings.environment.value == "local"
    assert not settings.postgres_dsn, "the suite is pointed at a real database"
    assert settings.llm_provider == "stub", "the suite would call a real provider"
    assert not settings.whatsapp_enabled, "the suite could send a real message"
    assert not settings.website_write_enabled, "the suite could write to the website"
