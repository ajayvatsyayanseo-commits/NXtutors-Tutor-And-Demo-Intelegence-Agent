"""Shared fixtures.

The environment is neutralised **before** anything that reads settings is
imported. `Settings` is `lru_cache`d, so a stray `DCC_*` variable from a
developer's shell would otherwise be baked into every test in the session and
produce failures that reproduce on one machine and nowhere else.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

# Neutralise first, import second. E402 is silenced for this file in pyproject.
for _key in [name for name in os.environ if name.startswith("DCC_")]:
    os.environ.pop(_key, None)
os.environ["DCC_ENVIRONMENT"] = "local"
os.environ["DCC_HASH_PEPPER"] = "test-pepper"
# The fake tutor fixtures publish profile links on this host; the outbound
# guardrail's allowlist is built from it.
os.environ["DCC_WEBSITE_PUBLIC_BASE_URL"] = "https://nxtutors.example"

from demo_command_center.bootstrap import build_local_stack, reset_singletons  # noqa: E402
from demo_command_center.config.settings import Settings, get_settings  # noqa: E402
from demo_command_center.contracts.common import (  # noqa: E402
    DemoMode,
    Language,
    Requirement,
)
from demo_command_center.domain.demo import DemoRequest  # noqa: E402
from demo_command_center.orchestration.context import Dependencies  # noqa: E402
from demo_command_center.orchestration.orchestrator import (  # noqa: E402
    DemoCommandCenterOrchestrator,
)
from demo_command_center.shared.clock import FrozenClock  # noqa: E402
from demo_command_center.shared.ids import prefixed  # noqa: E402

#: A Tuesday, mid-morning IST. Fixed so "tomorrow at 6pm" is a weekday evening
#: and the slot fixtures land inside the booking window every run.
FIXED_NOW = datetime(2026, 3, 10, 4, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_singletons() -> Iterator[None]:
    reset_singletons()
    get_settings.cache_clear()
    yield
    reset_singletons()
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(FIXED_NOW)


@pytest.fixture
def stack(
    settings: Settings, clock: FrozenClock
) -> tuple[DemoCommandCenterOrchestrator, Dependencies, dict[str, Any]]:
    """The real orchestrator wired to fakes. What every E2E test drives."""
    return build_local_stack(settings, clock=clock)


@pytest.fixture
def orchestrator(
    stack: tuple[DemoCommandCenterOrchestrator, Dependencies, dict[str, Any]],
) -> DemoCommandCenterOrchestrator:
    return stack[0]


@pytest.fixture
def deps(
    stack: tuple[DemoCommandCenterOrchestrator, Dependencies, dict[str, Any]],
) -> Dependencies:
    return stack[1]


@pytest.fixture
def doubles(
    stack: tuple[DemoCommandCenterOrchestrator, Dependencies, dict[str, Any]],
) -> dict[str, Any]:
    return stack[2]


@pytest.fixture
def conversation_ref() -> str:
    return "cv_test_0001"


@pytest.fixture
async def seeded_request(
    deps: Dependencies, clock: FrozenClock, conversation_ref: str
) -> DemoRequest:
    """A complete requirement, so matching is reachable without a chat."""
    request = DemoRequest(
        request_id=prefixed("req", now=clock.now()),
        conversation_ref=conversation_ref,
        student_ref="stu_test_0001",
        requirement=Requirement(
            service="board_exam_prep",
            board="CBSE",
            student_class="10",
            subject="Mathematics",
            mode=DemoMode.ONLINE,
            timezone="Asia/Kolkata",
        ),
        language=Language.EN,
        region="north",
        created_at=clock.now(),
    )
    await deps.demos.save_request(request)
    await deps.conversations.touch_inbound(conversation_ref, at=clock.now())
    return request
