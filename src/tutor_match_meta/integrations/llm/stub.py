"""Deterministic LLM stub for local runs and tests.

Not a mock that returns canned strings — it produces schema-valid output derived
from the input, so the full extraction path (schema validation, budget
accounting, merge-under-deterministic) is genuinely exercised without a key.

It also has explicit failure modes, because the tests that matter most are the
ones where the provider times out, rate-limits or returns garbage.
"""

from __future__ import annotations

import time
from typing import Any

from tutor_match_meta.domain import academics, modes, subjects
from tutor_match_meta.integrations.llm.provider import (
    LLMRateLimited,
    LLMRequest,
    LLMResponse,
    LLMSchemaViolation,
    LLMTimeout,
    LLMUsage,
)


class DeterministicStubProvider:
    """Reuses the domain lexicons to answer extraction requests plausibly."""

    name = "stub"

    def __init__(
        self,
        *,
        fail_with: type[Exception] | None = None,
        fail_after: int = 0,
        canned: dict[str, Any] | None = None,
    ) -> None:
        self._fail_with = fail_with
        self._fail_after = fail_after
        self._canned = canned
        self.calls = 0

    async def structured(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self._fail_with is not None and self.calls > self._fail_after:
            raise self._fail_with(f"stub failure on call {self.calls}")

        started = time.perf_counter()
        data = self._canned if self._canned is not None else self._answer(request)
        usage = LLMUsage(
            provider=self.name,
            model=f"stub-tier{int(request.tier)}",
            tier=request.tier,
            prompt_tokens=max(1, len(request.user_content) // 4),
            completion_tokens=64,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return LLMResponse(data=data, usage=usage)

    def _answer(self, request: LLMRequest) -> dict[str, Any]:
        if request.schema_name == "MatchRequirementExtraction":
            return self._extract(request.user_content)
        # An unknown schema is a programming error, not a runtime condition.
        raise LLMSchemaViolation(f"stub has no answer for schema {request.schema_name}")

    def _extract(self, text: str) -> dict[str, Any]:
        found = subjects.extract(text)
        return {
            "subject": found[0] if found else None,
            "additional_subjects": found[1:4],
            "board": academics.extract_board(text),
            "student_class": academics.extract_class(text),
            "exam": academics.extract_exam(text),
            "mode": (m.value if (m := modes.extract_mode(text)) else None),
            "learning_goal": None,
            "teaching_style": None,
            "language_preference": None,
            "topics": [],
            "weak_topics": [],
        }


def timing_out_provider() -> DeterministicStubProvider:
    return DeterministicStubProvider(fail_with=LLMTimeout)


def rate_limited_provider(*, succeed_first: int = 0) -> DeterministicStubProvider:
    return DeterministicStubProvider(fail_with=LLMRateLimited, fail_after=succeed_first)
