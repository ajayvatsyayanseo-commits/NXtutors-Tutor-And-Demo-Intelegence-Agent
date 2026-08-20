"""One command that verifies the whole agent.

The NXTutors Tutor and Demo Intelligence Agent is one product built from two
deployables. They share a `.env`, a database, an OpenAI account, a WhatsApp
number and a phone pepper — so verifying one half proves very little on its
own, and running two separate check suites made it easy to leave the second
one red for a week without noticing.

This is the single gate. It runs both halves plus the things that only exist
between them: the shared configuration, the live database, and the handoff.

    python scripts/verify_agent.py            # everything
    python scripts/verify_agent.py --fast     # skip the live-network checks

Exit code is 0 only if every check passed. A skipped check is reported as
SKIP and never counted as a pass.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "Demo Intelegence Agent"
PY = sys.executable

GREEN, RED, YELLOW, GREY, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m"


@dataclass
class Check:
    name: str
    cmd: list[str]
    cwd: Path
    #: Substring that must appear in stdout for the check to count as passed.
    #: Empty means "exit code 0 is enough".
    expect: str = ""
    #: Needs the network or the live database.
    live: bool = False


CHECKS: list[Check] = [
    # --- the Tutor half -------------------------------------------------
    Check("tutor · tests", [PY, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"], ROOT),
    Check("tutor · doctor", [PY, "-m", "tutor_match_meta.cli.doctor"], ROOT, "0 failed", live=True),
    Check(
        "tutor · lifecycle",
        [PY, "-m", "tutor_match_meta.cli.local_e2e"],
        ROOT,
        "every claim was backed",
        live=True,
    ),
    # --- the Demo half --------------------------------------------------
    Check("demo · tests", [PY, "-m", "pytest", "tests", "-q", "--no-cov", "-p", "no:cacheprovider"], DEMO),
    Check("demo · lint", [PY, "-m", "ruff", "check", "src", "tests", "scripts"], DEMO),
    Check("demo · types", [PY, "-m", "mypy", "src"], DEMO, "Success"),
    Check("demo · templates", [PY, "scripts/verify_templates.py"], DEMO, "TEMPLATES OK"),
    Check("demo · prohibited scan", [PY, "scripts/scan_prohibited.py"], DEMO, "OK:"),
    Check("demo · doctor", [PY, "-m", "demo_command_center.cli.doctor"], DEMO, "PROBLEMS: none"),
    Check("demo · lifecycle", [PY, "-m", "demo_command_center.cli.local_e2e"], DEMO, "LIFECYCLE OK"),
    # --- the seam between them ------------------------------------------
    Check(
        "combined · shared database",
        [PY, "scripts/verify_live_wiring.py"],
        DEMO,
        "WIRING OK",
        live=True,
    ),
    Check(
        "combined · both agents as one",
        [PY, "-m", "demo_command_center.cli.combined_e2e"],
        DEMO,
        "SYNC OK",
        live=True,
    ),
]


def run(check: Check, env: dict[str, str]) -> tuple[bool, str, float]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            check.cmd,
            cwd=check.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "timed out after 900s", time.perf_counter() - started
    except OSError as exc:
        return False, f"could not run: {exc}", time.perf_counter() - started

    elapsed = time.perf_counter() - started
    output = (completed.stdout or "") + (completed.stderr or "")

    if check.expect:
        # An expectation beats the exit code: `doctor` exits 0 while reporting
        # failures, and a check that only looked at the code would call that
        # healthy.
        ok = check.expect in output
        detail = "" if ok else f"expected {check.expect!r} in output"
    else:
        ok = completed.returncode == 0
        detail = "" if ok else f"exit {completed.returncode}"

    if not ok and not detail:
        detail = "failed"
    if not ok:
        tail = [line for line in output.strip().splitlines() if line.strip()][-3:]
        detail = f"{detail} | " + " / ".join(line.strip()[:90] for line in tail)
    return ok, detail, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="skip checks that need the live database or the network",
    )
    args = parser.parse_args()

    env = dict(os.environ)
    # Both packages importable from either working directory. Demo's own
    # pyproject already does this for pytest; the CLIs need it too.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(DEMO / "src"), str(ROOT / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env["PYTHONIOENCODING"] = "utf-8"

    print("=" * 72)
    print(" NXTutors Tutor and Demo Intelligence Agent — full verification")
    print("=" * 72)
    if args.fast:
        print(f" {YELLOW}--fast: live database and network checks are SKIPPED{RESET}")
    print()

    failures: list[str] = []
    skipped: list[str] = []

    for check in CHECKS:
        if args.fast and check.live:
            print(f"  {YELLOW}SKIP{RESET}  {check.name}")
            skipped.append(check.name)
            continue

        print(f"  {GREY}....{RESET}  {check.name}", end="\r", flush=True)
        ok, detail, elapsed = run(check, env)
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {check.name:<32} {GREY}{elapsed:5.1f}s{RESET}  {detail}")
        if not ok:
            failures.append(check.name)

    print()
    total = len(CHECKS) - len(skipped)
    if failures:
        print(f"{RED}FAILED{RESET} — {len(failures)} of {total} checks:")
        for name in failures:
            print(f"  - {name}")
        return 1

    print(f"{GREEN}ALL {total} CHECKS PASSED{RESET} — both halves and the seam between them.")
    if skipped:
        # Never silently: a skipped check is not evidence of health, and
        # `--fast` skipping the live database is exactly the check most likely
        # to be broken.
        print(f"{YELLOW}{len(skipped)} check(s) SKIPPED and NOT verified:{RESET}")
        for name in skipped:
            print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
