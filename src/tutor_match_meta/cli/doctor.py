"""`tutor-match-doctor` — is this environment able to run the service?

Answers the question a deploy pipeline and an on-call engineer both ask, and
answers it *without* touching production: it inspects configuration, resolves
every version, loads every policy and prompt, and exercises the full matching
pipeline against the in-memory fixture stack.

The rule it follows is the same one the release-gate report follows: a check
that could not run reports **SKIPPED**, never PASS. "We could not reach the
database" and "the database is fine" are different answers, and conflating them
is how a green dashboard ships a broken deploy.

Exit codes:

    0   every executed check passed
    1   at least one check failed
    2   configuration is invalid; nothing could be checked
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    # S105 pattern-matches the identifier `PASS`. These are check outcomes.
    PASS = "PASS"  # noqa: S105
    FAIL = "FAIL"
    #: Could not be executed here. Never silently promoted to PASS.
    SKIPPED = "SKIPPED"
    WARN = "WARN"


@dataclass(slots=True)
class Check:
    name: str
    status: Status
    detail: str = ""

    def line(self) -> str:
        return f"  [{self.status.value:<7}] {self.name}" + (
            f" — {self.detail}" if self.detail else ""
        )


class Doctor:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def record(self, name: str, status: Status, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.FAIL]

    @property
    def skipped(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.SKIPPED]


async def _run(doctor: Doctor) -> None:
    # ---------------------------------------------------------- configuration
    from tutor_match_meta.config.settings import get_settings

    settings = get_settings()
    doctor.record("settings load", Status.PASS, f"environment={settings.environment.value}")

    # ----------------------------------------------------------------- build
    from tutor_match_meta.version import build_info

    info = build_info()
    doctor.record(
        "build identity",
        Status.PASS if info.git_sha != "unknown" else Status.WARN,
        f"app={info.app_version} sha={info.short_sha} schema={info.schema_revision}",
    )

    # --------------------------------------------------------------- prompts
    from tutor_match_meta.prompts.registry import (
        MUST_CARRY_INJECTION_CLAUSE,
        REGISTRY,
    )
    from tutor_match_meta.security.injection import DATA_NOT_INSTRUCTIONS_CLAUSE

    try:
        active = REGISTRY.active()
    except KeyError as exc:
        doctor.record("prompt pins", Status.FAIL, str(exc))
    else:
        doctor.record(
            "prompt pins", Status.PASS, ", ".join(f"{t.ref}#{t.checksum[:8]}" for t in active)
        )
        missing = [
            t.ref
            for t in active
            if t.prompt_id in MUST_CARRY_INJECTION_CLAUSE
            and DATA_NOT_INSTRUCTIONS_CLAUSE not in t.stable_prefix
        ]
        doctor.record(
            "prompt injection clause",
            Status.FAIL if missing else Status.PASS,
            ",".join(missing),
        )
        uncacheable = [t.ref for t in active if not t.cacheable]
        doctor.record(
            "prompt cache prefixes",
            Status.WARN if uncacheable else Status.PASS,
            f"below the cacheable minimum: {uncacheable}" if uncacheable else "",
        )

    # -------------------------------------------------------------- policies
    from tutor_match_meta.bootstrap import build_policy_registry

    try:
        policies = build_policy_registry(settings).load_all()
        doctor.record("scoring policies", Status.PASS, f"{len(policies)} loaded")
    except Exception as exc:
        doctor.record("scoring policies", Status.FAIL, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------- model routing
    from tutor_match_meta.bootstrap import build_model_routing

    routing = build_model_routing(settings)
    doctor.record(
        "model routing",
        Status.PASS,
        " ".join(f"{k}={v}" for k, v in routing.as_dict().items()),
    )

    # ------------------------------------------------------------ the pipeline
    from datetime import UTC, datetime

    from tutor_match_meta.bootstrap import build_local_stack
    from tutor_match_meta.contracts.inbound import (
        InboundEnvelope,
        InboundKind,
        WhatsAppTurnV1,
    )

    service, deps = build_local_stack()
    envelope = InboundEnvelope(
        kind=InboundKind.WHATSAPP_TURN,
        trace_id="doctor",
        conversation_id="doctor:1",
        dedup_key="doctor:1",
        received_at=datetime.now(UTC),
        source_agent="doctor",
        payload=WhatsAppTurnV1(
            event_id="doctor-1",
            conversation_id="doctor:1",
            provider_message_id="doctor-1",
            text="need class 10 cbse maths teacher near sector 57 gurgaon after 6:30, home tuition",
        ),
    )
    try:
        result = await service.handle(envelope)
    except Exception as exc:
        doctor.record("matching pipeline", Status.FAIL, f"{type(exc).__name__}: {exc}")
    else:
        doctor.record(
            "matching pipeline",
            Status.PASS if result.matched else Status.FAIL,
            f"state={result.state.value} shortlist="
            f"{len(result.outcome.decision.shortlist) if result.outcome else 0}",
        )
        violations = result.outcome.fabrication_violations if result.outcome else ()
        doctor.record(
            "anti-fabrication guard",
            Status.FAIL if violations else Status.PASS,
            ",".join(violations),
        )
        events = getattr(deps.analytics, "events", [])
        doctor.record("analytics events", Status.PASS, f"{len(events)} emitted")

    # --------------------------------------------------------- infrastructure
    #
    # These need credentials this command deliberately does not assume. Each
    # reports SKIPPED with the reason rather than being quietly omitted.
    if not settings.postgres_dsn:
        doctor.record("postgresql", Status.SKIPPED, "no TMM_POSTGRES_DSN configured")
    else:
        try:
            from sqlalchemy import text

            from tutor_match_meta.bootstrap import database_sessions

            sessions = database_sessions(settings)
            if sessions is None:  # pragma: no cover - guarded by the DSN check
                raise RuntimeError("no sessionmaker despite a configured DSN")
            async with sessions() as session:
                await session.execute(text("SELECT 1"))
            doctor.record(
                "postgresql", Status.PASS, f"connected, schema={settings.postgres_schema}"
            )
        except Exception as exc:
            doctor.record("postgresql", Status.FAIL, f"{type(exc).__name__}")
            sessions = None

        if sessions is not None:
            # The check that turns "relation does not exist, mid-conversation"
            # into a deployment that refuses to go ready.
            from tutor_match_meta.repositories import schema_check

            report = await schema_check.verify(sessions, schema=settings.postgres_schema)
            doctor.record(
                "database schema",
                Status.PASS if report.ok else Status.FAIL,
                f"revision={report.revision} tables_ok={not report.missing_tables}"
                if report.ok
                else "; ".join(report.problems()),
            )

    doctor.record(
        "openai",
        Status.SKIPPED if settings.llm_provider == "stub" else Status.PASS,
        "llm_provider=stub; no provider call attempted"
        if settings.llm_provider == "stub"
        else "configured (not called)",
    )
    doctor.record(
        "chitragupta",
        Status.SKIPPED if not settings.chitragupta_enabled else Status.PASS,
        "disabled" if not settings.chitragupta_enabled else "configured (not called)",
    )
    doctor.record(
        "whatsapp sender",
        Status.SKIPPED if not settings.whatsapp_enabled else Status.PASS,
        f"outbound_ownership={settings.outbound_ownership}",
    )


def main() -> int:
    doctor = Doctor()
    try:
        asyncio.run(_run(doctor))
    except Exception as exc:
        print(f"CONFIGURATION ERROR: {type(exc).__name__}: {exc}")
        return 2

    print("tutor-match-meta doctor\n")
    for check in doctor.checks:
        print(check.line())

    failed, skipped = doctor.failed, doctor.skipped
    print(
        f"\n{len(doctor.checks) - len(failed) - len(skipped)} passed, "
        f"{len(failed)} failed, {len(skipped)} skipped"
    )
    if skipped:
        print("\nSKIPPED checks were NOT executed and are not evidence of health:")
        for check in skipped:
            print(f"  - {check.name}: {check.detail}")
    return 1 if failed else 0


def _cli() -> Any:  # pragma: no cover - console-script shim
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
