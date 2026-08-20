"""`dcc-e2e` — run the full demo lifecycle against in-memory adapters.

No AWS, no OpenAI, no Google, no Cashfree, no Meta. It drives the *real*
orchestrator, state machine and outbound boundary; only the edges are doubled.
That is the point: a green run here means the lifecycle logic works, and the
only untested surface is the provider wire format.

Exit code is 0 only if every step passed. `make demo` therefore fails a build
rather than printing a wall of text nobody reads.
"""

from __future__ import annotations

import asyncio
import sys

from demo_command_center.bootstrap import build_local_stack, reset_singletons
from demo_command_center.config.settings import get_settings
from demo_command_center.glue.lifecycle import LifecycleRunner
from demo_command_center.observability import logging as log_config


async def _run() -> int:
    reset_singletons()
    log_config.configure("WARNING")
    orchestrator, deps, doubles = build_local_stack(get_settings())

    report = await LifecycleRunner(orchestrator, deps, doubles).run()
    print(report.render())

    if report.messages:
        print("\nMessages the outbound boundary delivered:")
        for index, body in enumerate(report.messages, start=1):
            first = body.splitlines()[0] if body else "(template)"
            print(f"  {index}. {first[:96]}")

    outbox = doubles["stores"]["outbox"]
    if hasattr(outbox, "events"):
        print("\nDomain events published:")
        for event in dict.fromkeys(outbox.events()):
            print(f"  - {event}")

    print()
    if report.ok:
        print("LIFECYCLE OK")
        return 0
    failed = [step for step in report.steps if not step.ok]
    print(f"LIFECYCLE FAILED — {len(failed)} step(s):")
    for step in failed:
        print(f"  {step.index}. {step.name}: {step.detail or 'unexpected state'}")
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
