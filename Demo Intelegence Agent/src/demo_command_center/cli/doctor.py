"""`dcc-doctor` — is this build coherent, and what is it missing?

Two kinds of finding, kept apart on purpose:

* **PROBLEM** — this build is wrong. A malformed transition table, a policy that
  will not load, a feature the model weights but the builder never produces.
  These are bugs and the exit code is non-zero.
* **GAP** — this build is fine but an external dependency is unconfigured. No
  Meta token, no Aurora ARN, an unapproved template. Expected locally, reported
  so nobody has to guess which integrations are live.

The distinction matters because "missing credentials" must never look like
"broken code", and vice versa.
"""

from __future__ import annotations

import asyncio
import sys

from demo_command_center.bootstrap import (
    discount_policy,
    forecast_model,
    monitoring_policy,
    reminder_policy,
    reset_singletons,
)
from demo_command_center.capabilities.forecasting.features import FEATURE_NAMES
from demo_command_center.config.settings import Settings, get_settings
from demo_command_center.integrations.meta_whatsapp.templates import registry
from demo_command_center.state.transitions import table_invariants
from demo_command_center.version import build_info


def _check_state_machine() -> list[str]:
    return [f"state machine: {problem}" for problem in table_invariants()]


def _check_policies(settings: Settings) -> list[str]:
    problems: list[str] = []
    try:
        reminder = reminder_policy(settings)
        approved = registry().approved_names()
        for offset in reminder.offsets:
            if offset.template not in approved:
                problems.append(
                    f"reminder policy references a template that is not approved: {offset.template}"
                )
    except Exception as exc:
        problems.append(f"reminder policy: {exc}")

    try:
        discount_policy(settings)
    except Exception as exc:
        problems.append(f"discount policy: {exc}")

    try:
        model = forecast_model(settings)
        declared = {feature.name for feature in model.features}
        missing = declared - set(FEATURE_NAMES)
        extra = set(FEATURE_NAMES) - declared
        if missing:
            # The dangerous direction: the model weights a feature the builder
            # never produces, so it silently falls back to its default forever.
            problems.append(f"forecast weights features the builder never emits: {sorted(missing)}")
        if extra:
            problems.append(f"feature builder emits features the model ignores: {sorted(extra)}")
    except Exception as exc:
        problems.append(f"forecast model: {exc}")

    try:
        monitoring_policy(settings)
    except Exception as exc:
        problems.append(f"monitoring policy: {exc}")
    return problems


def _check_gaps(settings: Settings) -> list[str]:
    gaps: list[str] = []
    if not settings.meta_enabled:
        gaps.append("Meta WhatsApp disabled — no message can be delivered")
    if not settings.google_enabled:
        gaps.append("Google Calendar disabled — no event or Meet link can be created")
    if not settings.cashfree_enabled:
        gaps.append("Cashfree disabled — no payment order can be created")
    if settings.llm_provider != "openai":
        gaps.append("LLM provider is the offline stub — objection extraction is heuristic")
    if not settings.gateway_base_url:
        gaps.append("NXTutors gateway not configured — identity, quotes and activation unavailable")
    if settings.persistence_mode.value == "memory":
        gaps.append("persistence is in-memory — state is lost on container recycle")
    if not settings.scheduler_role_arn:
        gaps.append("EventBridge Scheduler not configured — reminders will not fire on time")

    for template in registry().unconfirmed():
        gaps.append(f"template {template.name!r} unusable: {template.note}")

    try:
        from demo_command_center.integrations.tutor_intelligence.local_adapter import available

        if not available():
            gaps.append("tutor_match_meta not importable — the fake matcher is in use")
    except Exception as exc:
        gaps.append(f"tutor intelligence adapter: {exc}")
    return gaps


async def _run() -> int:
    reset_singletons()
    settings = get_settings()
    info = build_info()

    print(f"Demo Command Center doctor — {info.get('version', '?')} ({settings.environment.value})")
    print()

    problems = _check_state_machine() + _check_policies(settings)
    gaps = _check_gaps(settings)

    if problems:
        print(f"PROBLEMS ({len(problems)}) — this build is not correct:")
        for problem in problems:
            print(f"  ! {problem}")
    else:
        print("PROBLEMS: none. State machine, policies and feature contract are coherent.")
    print()

    if gaps:
        print(f"GAPS ({len(gaps)}) — external dependencies not configured here:")
        for gap in gaps:
            print(f"  - {gap}")
    else:
        print("GAPS: none. Every integration is configured.")

    print()
    print("Run `make demo` to exercise the full lifecycle against in-memory adapters.")
    return 1 if problems else 0


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
