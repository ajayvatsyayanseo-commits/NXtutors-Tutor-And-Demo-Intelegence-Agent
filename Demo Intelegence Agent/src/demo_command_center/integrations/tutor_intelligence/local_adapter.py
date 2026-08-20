"""In-process adapter for the Tutor Intelligence Agent.

This is the production boundary when both services are deployed in the same
Lambda package, and it is the one the discovery phase selected. It calls
`TutorMatchOrchestrator.match()` — which is a pure function of its inputs: it
performs no send, writes no Tutor state and touches no outbox. That is what
makes "Tutor Intelligence operates in return-only mode" verifiable rather than
promised.

What this module deliberately does **not** do:

* It does not call `/internal/v1/handoff`, `HandoffService` or `TurnService`.
  Those own Tutor's own conversation FSM and can produce a `reply_text`; going
  through them would make Tutor a second owner of one WhatsApp thread.
* It does not import anything from `tutor_match_meta` at module scope. The
  import is inside `_build`, so this file — and everything that imports it —
  loads fine when Tutor Intelligence is not installed. `available()` is how a
  caller checks, and `bootstrap` falls back to the fake when it is False.

Translation between Tutor's contracts and Demo's is one-way and total: a Tutor
type never escapes this module.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from demo_command_center.contracts.ports import ProviderRejected, ProviderUnavailable
from demo_command_center.contracts.tutor_match import (
    DataQuality,
    DimensionScore,
    Freshness,
    MatchRejection,
    ScoreEvidence,
    TutorCandidateV1,
    TutorMatchRequestV1,
    TutorMatchResultV1,
)
from demo_command_center.observability.logging import get_logger

logger = get_logger("integration.tutor_intelligence")

PROVIDER = "tutor_intelligence"


def available() -> bool:
    """Whether `tutor_match_meta` is importable in this runtime."""
    try:
        import tutor_match_meta  # noqa: F401
    except ImportError:
        return False
    return True


class LocalTutorIntelligenceAdapter:
    """Calls the Tutor orchestrator in-process. Never triggers a Tutor send."""

    def __init__(
        self,
        orchestrator: Any | None = None,
        *,
        public_base_url: str = "",
        tutors: Any | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._public_base_url = public_base_url
        #: Tutor source override. Left as None in production, where
        #: `_tutor_repository` picks the live projection. Passed explicitly by
        #: `dcc-sync`, which tests *composition* and must not change its verdict
        #: because a nightly feed is late — that is a data alarm, not a
        #: regression in either agent's code.
        self._tutors = tutors

    async def match_tutors(self, request: TutorMatchRequestV1) -> TutorMatchResultV1:
        if not request.return_only:  # pragma: no cover - the model forbids it
            raise ProviderRejected(PROVIDER, "return_only is mandatory")

        orchestrator = self._orchestrator or self._build()
        requirement = self._to_requirement(request)

        try:
            outcome = await orchestrator.match(requirement, trace_id=request.trace_id)
        except Exception as exc:
            logger.warning("tutor match failed", extra={"dcc_error": type(exc).__name__})
            raise ProviderUnavailable(PROVIDER, type(exc).__name__) from exc

        return self._to_result(outcome, request)

    # ------------------------------------------------------------ building
    def _build(self) -> Any:
        """Construct a standalone Tutor orchestrator.

        Built directly rather than through `tutor_match_meta.bootstrap`, because
        the bootstrap path also wires a turn service, an outbox and a sender —
        objects that would give Tutor Intelligence the ability to send. The
        orchestrator alone cannot.
        """
        try:
            from tutor_match_meta.config.settings import get_settings
            from tutor_match_meta.orchestration.orchestrator import TutorMatchOrchestrator
            from tutor_match_meta.scoring.policy import PolicyRegistry
        except ImportError as exc:
            raise ProviderUnavailable(PROVIDER, "tutor_match_meta is not installed") from exc

        _export_shared_env()
        settings = get_settings()
        self._orchestrator = TutorMatchOrchestrator(
            tutors=self._tutors if self._tutors is not None else _tutor_repository(settings),
            # NOT `bootstrap.build_policy_registry`. That resolves a relative
            # `policy_dir` against `Path.cwd()` first, and when Demo is the
            # caller the cwd is `Demo Intelegence Agent/`, which has a
            # `config/policies/` of its own holding discount, forecast,
            # monitoring and reminder policies. Tutor would then load Demo's
            # directory and fail with `policy 'board_exam_prep.v1' not found`.
            #
            # Two agents in one process cannot share a cwd-relative config
            # path. `_tutor_policy_dir` anchors on the package instead.
            policies=PolicyRegistry(_tutor_policy_dir(settings), settings.default_policy),
            public_base_url=self._public_base_url or settings.website_public_base_url,
        )
        return self._orchestrator

    # --------------------------------------------------------- translation
    @staticmethod
    def _to_requirement(request: TutorMatchRequestV1) -> Any:
        """Demo request → `MatchRequirementV1`.

        Every field is marked `Provenance.DETERMINISTIC`: Demo only sends a
        requirement once it is confirmed, so Tutor's hard filters are entitled
        to act on it. Sending LLM-provenance values would silently downgrade
        them to scoring-only signals and widen the shortlist.
        """
        from tutor_match_meta.contracts.common import Provenance, Tracked, TuitionMode
        from tutor_match_meta.contracts.requirement import MatchRequirementV1

        def tracked(value: str | None) -> Any:
            if not value:
                return None
            return Tracked(value=value, confidence=1.0, provenance=Provenance.DETERMINISTIC)

        mode_map = {"online": TuitionMode.ONLINE, "home": TuitionMode.HOME}
        return MatchRequirementV1(
            conversation_id=request.conversation_ref,
            student_ref=request.student_ref,
            subject=tracked(request.subject),
            board=tracked(request.board),
            student_class=tracked(request.student_class),
            learning_goal=tracked(request.learning_goal),
            mode=Tracked(
                value=mode_map.get(request.mode.value, TuitionMode.ONLINE),
                confidence=1.0,
                provenance=Provenance.DETERMINISTIC,
            ),
            timezone=request.timezone,
            demo_requested=True,
            captured_at=datetime.now(UTC),
        )

    def _to_result(self, outcome: Any, request: TutorMatchRequestV1) -> TutorMatchResultV1:
        """`MatchOutcome` → Demo's result. Drops internal-only dimensions."""
        decision = outcome.decision
        scored_by_id = {c.tutor_id: c for c in decision.scored}
        excluded = set(request.exclude_tutor_refs)

        candidates: list[TutorCandidateV1] = []
        for entry in decision.shortlist:
            if entry.tutor_id in excluded:
                continue
            scored = scored_by_id.get(entry.tutor_id)
            candidates.append(
                TutorCandidateV1(
                    rank=len(candidates) + 1,
                    tutor_ref=entry.tutor_id,
                    name=entry.name,
                    profile_url=entry.profile_url,
                    reasons=tuple(entry.reasons),
                    mode_label=entry.mode_label,
                    locality_label=entry.locality_label,
                    availability_label=entry.availability_label,
                    fee_label=entry.fee_label,
                    scores=self._to_scores(scored),
                    final_score=float(scored.final_score) if scored else 0.0,
                    weight_coverage=float(scored.weight_coverage) if scored else 0.0,
                    freshness=_freshness(scored),
                )
            )
            if len(candidates) >= request.limit:
                break

        return TutorMatchResultV1(
            trace_id=request.trace_id,
            correlation_id=request.correlation_id,
            conversation_ref=request.conversation_ref,
            match_session_id=decision.match_session_id,
            policy_ref=f"{decision.policy_id}@{decision.policy_version}",
            candidates=tuple(candidates),
            rejections=tuple(
                MatchRejection(tutor_ref=r.tutor_id, rule=r.rule, detail=r.detail)
                for r in decision.rejections[:20]
            ),
            no_match_reason=decision.no_match_reason,
            requires_human_review=bool(decision.requires_human_review),
            degraded_sources=tuple(decision.degraded_sources),
            generated_at=decision.generated_at,
            # The orchestrator has no sender. Asserted structurally by
            # tests/contract/test_tutor_return_only.py.
            sender_was_silent=True,
        )

    @staticmethod
    def _to_scores(scored: Any) -> tuple[DimensionScore, ...]:
        """Drop `INTERNAL_ONLY_DIMENSIONS`. `replacement_risk` is a workforce
        signal and must never reach a customer-facing surface."""
        if scored is None:
            return ()
        from tutor_match_meta.contracts.common import INTERNAL_ONLY_DIMENSIONS

        out: list[DimensionScore] = []
        for dimension, score in scored.scores.items():
            if dimension in INTERNAL_ONLY_DIMENSIONS:
                continue
            out.append(
                DimensionScore(
                    dimension=dimension.value,
                    score=float(score.score),
                    confidence=float(score.confidence),
                    data_quality=DataQuality(score.data_quality.value),
                    evidence=tuple(
                        ScoreEvidence(
                            source=e.source,
                            field_name=e.field,
                            value=e.value,
                            observed_at=e.observed_at,
                        )
                        for e in score.evidence[:4]
                    ),
                )
            )
        return tuple(out)


def _export_shared_env() -> None:
    """Put the shared `.env`'s `TMM_*` keys into the process environment.

    Tutor's `Settings` declares `env_file=".env"`, which pydantic-settings
    resolves against the **working directory**. Demo runs from
    `Demo Intelegence Agent/`, which has no `.env` — so without this, Tutor
    silently loads its *defaults* for every setting, including
    `postgres_dsn = postgresql+asyncpg://tmm:tmm@localhost:5433/tmm`. Nothing
    raises; the projection is simply unreachable and every match falls back to
    fixtures.

    Environment variables outrank `env_file` in pydantic-settings, so exporting
    is enough and needs no change to the protected Tutor package. Existing
    values are never overwritten — a deployed Lambda sets these directly, and
    that must win over a file that should not be there at all.
    """
    from demo_command_center.config.settings import _repo_root

    path = _repo_root() / ".env"
    if not path.is_file():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("TMM_") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        key = key.strip()
        value = raw.strip().strip('"').strip("'")
        # A blank in the file means "unset", and exporting "" would shadow the
        # package default with something less useful than the default.
        if not value or key in os.environ:
            continue
        os.environ[key] = value


def _tutor_repository(settings: Any) -> Any:
    """The real tutor projection when a database is reachable, fixtures otherwise.

    Which one you get is not cosmetic — it decides whether a parent is shown a
    real tutor with a real profile URL or a fixture. So the fallback is logged
    at WARNING with the reason, never silently.

    Two things the projection path does NOT paper over, both currently live
    against the shared database and both recorded in
    `docs/final-integration-gaps.md`:

    * **Freshness is a WHERE clause**, not a post-filter — a row synced longer
      ago than `projection_aging_hours` never enters the pool at all. With the
      projection at ~44h and the window at 24h, a real search legitimately
      returns zero candidates. That is the anti-fabrication rule working, not a
      bug, and the fix is to re-run the feed rather than to widen the window.
    * **Subject is a hard filter**, and only ~1.3% of the synced rows carry a
      subject, so even a fresh projection currently shortlists from ~24 tutors.
    """
    from tutor_match_meta.fixtures.tutors import sample_tutors
    from tutor_match_meta.repositories.memory_store import InMemoryTutorRepository

    dsn = str(getattr(settings, "postgres_dsn", "") or "")
    if not dsn or "localhost" in dsn:
        logger.warning(
            "tutor projection unavailable; serving FIXTURES",
            extra={"dcc_reason": "no_database_configured"},
        )
        return InMemoryTutorRepository(sample_tutors())

    try:
        from tutor_match_meta.repositories.postgres import (
            PostgresTutorRepository,
            build_sessions,
            create_engine,
        )

        return PostgresTutorRepository(
            build_sessions(create_engine(settings)),
            fresh_hours=settings.projection_fresh_hours,
            aging_hours=settings.projection_aging_hours,
        )
    except Exception as exc:  # pragma: no cover - dependency/connection gated
        logger.warning(
            "tutor projection unreachable; serving FIXTURES",
            extra={"dcc_reason": type(exc).__name__},
        )
        return InMemoryTutorRepository(sample_tutors())


def _tutor_policy_dir(settings: Any) -> Path:
    """Tutor's OWN policy directory, anchored on the package, never the cwd.

    Walks up from `tutor_match_meta/` looking for the configured relative path,
    and accepts a directory only if it actually contains Tutor's default
    policy. That last condition is the whole point: Demo's `config/policies/`
    is a real directory, so an `is_dir()` check would match it and hand Tutor
    the wrong policy set.
    """
    configured = Path(settings.policy_dir)
    if configured.is_absolute():
        return configured

    import tutor_match_meta

    for parent in Path(tutor_match_meta.__file__).resolve().parents:
        candidate = parent / configured
        if (candidate / f"{settings.default_policy}.yaml").is_file():
            return candidate
    return configured


def _freshness(scored: Any) -> Freshness:
    if scored is None:
        return Freshness.UNKNOWN
    try:
        return Freshness(scored.freshness.value)
    except (AttributeError, ValueError):
        return Freshness.UNKNOWN
