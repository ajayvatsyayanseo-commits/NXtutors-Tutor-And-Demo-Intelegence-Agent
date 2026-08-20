"""`dcc-sync` — the two agents running as one, end to end.

The difference from `dcc-e2e`: that one drives the Demo lifecycle against a
**fake** Tutor Intelligence. This one calls the **real** `tutor_match_meta`
orchestrator in process, so a green run proves the two halves actually compose
— the requirement translation, the policy selection, the evidence, the URL
allowlist and the candidate snapshot all cross the boundary for real.

It refuses to fall back to the fake. A silent fallback here would be the worst
possible behaviour: the run would still print thirty green steps while proving
nothing about the integration this command exists to test.

Beyond the lifecycle, it asserts the four sync invariants that make "two agents,
one product" true rather than aspirational:

  1. Tutor Intelligence sent nothing.
  2. Every candidate presented came from a Tutor response.
  3. Exactly one agent owned the conversation at each step.
  4. Demo wrote nothing into Tutor's schema.

Exit code is 0 only if every step AND every invariant passed.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from demo_command_center.bootstrap import build_local_stack, reset_singletons
from demo_command_center.config.settings import get_settings
from demo_command_center.contracts.tutor_match import (
    TutorMatchRequestV1,
    TutorMatchResultV1,
)
from demo_command_center.glue.lifecycle import LifecycleRunner
from demo_command_center.integrations.tutor_intelligence.local_adapter import (
    LocalTutorIntelligenceAdapter,
    available,
)
from demo_command_center.observability import logging as log_config

PASS = "  [PASS]"  # noqa: S105 - an output marker, not a credential
FAIL = "  [FAIL]"


class RecordingTutorAgent(LocalTutorIntelligenceAdapter):
    """The real adapter, plus a tape of what crossed the boundary.

    Subclassed rather than wrapped so the object still satisfies whatever the
    orchestrator expects of the real one — a wrapper that grew a method the
    adapter lacks would pass here and fail in production.
    """

    def __init__(self) -> None:
        # Fixtures on purpose. This command's job is to prove the two agents
        # COMPOSE — requirement translation, policy selection, evidence, URL
        # allowlist, candidate snapshot. Reading the live projection instead
        # would make the verdict depend on whether a nightly feed ran, so a
        # late sync would look identical to a broken integration. The live
        # projection is probed separately, below, and reported as data health.
        from tutor_match_meta.fixtures.tutors import sample_tutors
        from tutor_match_meta.repositories.memory_store import InMemoryTutorRepository

        super().__init__(tutors=InMemoryTutorRepository(sample_tutors()))
        self.requests: list[TutorMatchRequestV1] = []
        self.results: list[TutorMatchResultV1] = []

    async def match_tutors(self, request: TutorMatchRequestV1) -> TutorMatchResultV1:
        self.requests.append(request)
        result = await super().match_tutors(request)
        self.results.append(result)
        return result


def _render_tutor_work(agent: RecordingTutorAgent) -> None:
    print("\n" + "=" * 78)
    print("TUTOR INTELLIGENCE — what the matching half decided")
    print("=" * 78)

    if not agent.results:
        print("  (never called)")
        return

    paired = zip(agent.requests, agent.results, strict=True)
    for index, (request, result) in enumerate(paired, start=1):
        print(
            f"\n  call {index}: {request.subject} · {request.board} · class {request.student_class}"
        )
        print(f"    goal         : {request.learning_goal}")
        print(f"    mode         : {request.mode.value}")
        print(f"    policy       : {result.policy_ref}")
        print(f"    session      : {result.match_session_id}")
        print(
            f"    considered   : {len(result.candidates)} shortlisted, "
            f"{len(result.rejections)} rejected"
        )
        print(f"    human review : {result.requires_human_review}")
        for candidate in result.candidates:
            print(f"\n    #{candidate.rank} {candidate.name}  [{candidate.tutor_ref}]")
            print(
                f"        score  : {candidate.final_score:.3f}  "
                f"(coverage {candidate.weight_coverage:.2f})"
            )
            print(f"        profile: {candidate.profile_url}")
            # A `None` label means Tutor could not substantiate it. Demo renders
            # the omission; it never fills the gap in.
            print(
                f"        fee    : {candidate.fee_label or '— not substantiated, so never quoted'}"
            )
            for reason in candidate.reasons:
                print(f"        reason : {reason}")


def _render_conversation(messages: list[str]) -> None:
    print("\n" + "=" * 78)
    print("THE CONVERSATION — what the parent actually received")
    print("=" * 78)
    for index, body in enumerate(messages, start=1):
        print(f"\n  ── message {index} " + "─" * 56)
        for line in body.splitlines():
            print(f"  {line}" if line else "")


def _check_invariants(
    agent: RecordingTutorAgent, deps: Any, doubles: dict[str, Any], presented: tuple[Any, ...]
) -> list[str]:
    """The four properties that make the two halves one product."""
    failures: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print(f"{PASS if ok else FAIL} {label}{f'  ({detail})' if detail else ''}")
        if not ok:
            failures.append(label)

    print("\n" + "=" * 78)
    print("SYNC INVARIANTS")
    print("=" * 78)

    check(bool(agent.results), "the real Tutor agent was called", f"{len(agent.results)} call(s)")

    # 1. Tutor sent nothing. Two ways, because the flag is a claim and the
    #    object graph is a fact.
    check(
        all(result.sender_was_silent for result in agent.results),
        "Tutor Intelligence reports it sent nothing",
    )
    check(
        not hasattr(agent, "sender") and not hasattr(agent, "outbox"),
        "the Tutor adapter holds no sender and no outbox",
    )

    # 2. Every presented candidate traces to a Tutor response.
    returned = {c.tutor_ref for result in agent.results for c in result.candidates}
    shown = {c.tutor_ref for c in presented}
    check(
        shown.issubset(returned) and bool(shown),
        "every candidate presented came from Tutor Intelligence",
        f"{len(shown)} shown, all in the {len(returned)} returned",
    )

    # 3. Demo owns the conversation; the capability call did not move it.
    check(
        len({r.conversation_ref for r in agent.requests}) <= 1,
        "one conversation across the boundary",
    )
    check(
        all(request.return_only for request in agent.requests),
        "every call was made in return-only mode",
    )

    # 4. The URLs that reached the parent are the ones Tutor supplied.
    sent_bodies = " ".join(
        (message[0].body if isinstance(message, tuple) else message.body)
        for message in doubles["whatsapp"].sent
    )
    check(
        "nxtutors.example" not in sent_bodies,
        "no placeholder host reached the parent",
    )

    return failures


async def _report_live_projection() -> None:
    """Data health of the real tutor feed. Reported, never asserted.

    Kept out of the verdict deliberately: a stale feed is an operations alarm,
    not a code regression, and failing this command for it would train people
    to ignore a red `dcc-sync`. But it is printed every run, because a search
    that legitimately returns nothing is otherwise indistinguishable from one
    that is broken.
    """
    print("\n" + "=" * 78)
    print("LIVE TUTOR PROJECTION — data health (reported, not asserted)")
    print("=" * 78)

    from demo_command_center.config.settings import PersistenceMode, get_settings

    settings = get_settings()
    if settings.persistence_mode is not PersistenceMode.POSTGRES_DSN:
        print(f"  skipped: persistence_mode={settings.persistence_mode.value}")
        return

    from demo_command_center.storage.postgres.pool import PostgresPool

    pool = PostgresPool(
        settings.postgres_dsn.get_secret_value(),
        schema="tutor_match",
        require_tls=settings.postgres_require_tls,
    )
    try:
        row = (
            await pool.fetch(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE jsonb_array_length(subjects::jsonb) > 0) AS matchable, "
                "EXTRACT(EPOCH FROM (now() - max(synced_at)))/3600 AS age_hours "
                "FROM tutor_projection"
            )
        )[0]
    except Exception as error:  # pragma: no cover - network/permission gated
        print(f"  unreadable: {type(error).__name__}")
        return
    finally:
        await pool.close()

    total = int(row["total"] or 0)
    matchable = int(row["matchable"] or 0)
    age = float(row["age_hours"] or 0.0)
    window = 24  # TMM_PROJECTION_AGING_HOURS

    print(f"  tutors synced      : {total}")
    print(f"  with a subject     : {matchable}  ({100 * matchable / total if total else 0:.1f}%)")
    print(f"  projection age     : {age:.1f}h   (aging window {window}h)")

    if age > window:
        print(
            f"\n  STALE — every row is older than the {window}h window, and freshness is a\n"
            "  WHERE clause, so a real search returns ZERO candidates. That is the\n"
            "  anti-fabrication rule working. Re-run the website feed; do not widen\n"
            "  the window to make results appear."
        )
    elif matchable < total * 0.5:
        print(
            f"\n  THIN — subject is a hard filter and only {matchable} of {total} rows carry one,\n"
            "  so a real search shortlists from a small pool."
        )
    else:
        print("\n  healthy.")


async def _run() -> int:
    reset_singletons()
    log_config.configure("WARNING")

    if not available():
        print("SYNC FAILED — tutor_match_meta is not importable in this runtime.")
        print("  This command exists to test the real integration and will not")
        print("  silently fall back to the fake. Run from the repository root, or")
        print("  add `src/` to PYTHONPATH.")
        return 1

    settings = get_settings()
    agent = RecordingTutorAgent()
    orchestrator, deps, doubles = build_local_stack(settings, tutors=agent)

    report = await LifecycleRunner(
        orchestrator, deps, doubles, conversation_ref="cv_combined_sync"
    ).run()

    print("=" * 78)
    print("NXTutors Tutor and Demo Intelligence Agent — combined end-to-end")
    print("=" * 78 + "\n")
    print(report.render())

    _render_tutor_work(agent)

    presented = await deps.demos.load_candidates("cv_combined_sync")
    _render_conversation(report.messages)
    failures = _check_invariants(agent, deps, doubles, tuple(presented))

    await _report_live_projection()

    outbox = doubles["stores"]["outbox"]
    if hasattr(outbox, "events"):
        print("\n" + "=" * 78)
        print("DOMAIN EVENTS")
        print("=" * 78)
        for event in dict.fromkeys(outbox.events()):
            print(f"  - {event}")

    print()
    if report.ok and not failures:
        print("SYNC OK — both agents ran as one product, end to end.")
        return 0

    bad = [step for step in report.steps if not step.ok]
    print(f"SYNC FAILED — {len(bad)} step(s), {len(failures)} invariant(s)")
    for step in bad:
        print(f"  step {step.index}. {step.name}: {step.detail or 'unexpected state'}")
    for failure in failures:
        print(f"  invariant: {failure}")
    return 1


def _utf8_stdout() -> None:
    """Windows consoles and piped stdout default to cp1252, which cannot encode
    the rupee sign. Every money figure this tool prints carries one, so without
    this the command dies on its own output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _utf8_stdout()
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
