"""Post-deploy smoke test. Referenced by deploy.yml and rollback.yml.

Answers one question: **is the thing that just deployed actually serving?**

Deliberately narrow. It does not exercise matching — that is what the test
suite is for, and a smoke test that sends a real message would create a real
conversation and a real database row on every deploy. It checks the surfaces
that prove the deployment landed and is wired:

    /internal/v1/health    the process is up and reports its posture
    /internal/v1/ready     policies load, prompt pins resolve, secrets are set
    /internal/v1/version   the deployed SHA matches what we just shipped
    ingress auth           an unsigned request is refused (401)

Exit codes:

    0   every check passed
    1   a check failed — the deploy should be rolled back
    2   could not run (missing configuration)

Usage:

    python scripts/smoke_test.py --environment production
    python scripts/smoke_test.py --environment production --expect-sha "$GITHUB_SHA"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 15


class Failure(Exception):
    """A check failed. The message is what an operator reads."""


def _get(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310 - https, from config
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except (ValueError, OSError):
            return exc.code, {}


def _post_unsigned(url: str) -> int:
    """An unsigned POST must be refused. Proves auth is actually on."""
    request = urllib.request.Request(  # noqa: S310 - https, from config
        url,
        data=b'{"event_id":"smoke","conversation_id":"smoke","provider_message_id":"smoke","text":"smoke"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def check_health(base: str) -> str:
    status, body = _get(f"{base}/internal/v1/health")
    if status != 200:
        raise Failure(f"/health returned {status}")
    if body.get("status") != "ok":
        raise Failure(f"/health says {body.get('status')!r}")
    return f"environment={body.get('environment')} ownership={body.get('outbound_ownership')}"


def check_ready(base: str) -> str:
    status, body = _get(f"{base}/internal/v1/ready")
    if status != 200:
        raise Failure(f"/ready returned {status}: {body.get('problems')}")
    return "policies and prompt pins resolve"


def check_version(base: str, secret: str, expect_sha: str | None) -> str:
    status, body = _get(
        f"{base}/internal/v1/version", headers={"X-NXTUTORS-INTERNAL-SECRET": secret}
    )
    if status == 401:
        raise Failure("/version rejected the internal secret — is it rotated?")
    if status != 200:
        raise Failure(f"/version returned {status}")

    deployed = str(body.get("git_sha", "unknown"))
    if expect_sha and not deployed.startswith(expect_sha[:12]):
        # The most valuable check here: a deploy that reported success but did
        # not actually replace the running code.
        raise Failure(f"deployed sha is {deployed[:12]}, expected {expect_sha[:12]}")
    return (
        f"app={body.get('app_version')} sha={deployed[:12]} "
        f"schema={body.get('schema_revision')} prompts={body.get('prompt_versions')}"
    )


def check_ingress_refuses_unsigned(ingress_url: str) -> str:
    status = _post_unsigned(ingress_url)
    if status != 401:
        raise Failure(
            f"unsigned ingress POST returned {status}, expected 401. "
            "Signature verification may be disabled."
        )
    return "unsigned request refused"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--expect-sha", default=os.getenv("GITHUB_SHA", ""))
    args = parser.parse_args()

    internal = (os.getenv("TMM_SMOKE_INTERNAL_URL") or "").rstrip("/")
    ingress = (os.getenv("TMM_SMOKE_INGRESS_URL") or "").rstrip("/")
    secret = os.getenv("TMM_INTERNAL_SECRET") or ""

    if not internal or not secret:
        print(
            "CONFIGURATION: set TMM_SMOKE_INTERNAL_URL and TMM_INTERNAL_SECRET "
            "(terraform outputs internal_api_url). Cannot smoke test."
        )
        return 2

    checks: list[tuple[str, object]] = [
        ("health", lambda: check_health(internal)),
        ("ready", lambda: check_ready(internal)),
        ("version", lambda: check_version(internal, secret, args.expect_sha or None)),
    ]
    if ingress:
        checks.append(("ingress auth", lambda: check_ingress_refuses_unsigned(ingress)))
    else:
        print("NOTE: TMM_SMOKE_INGRESS_URL unset; skipping the ingress auth check.")

    print(f"smoke test — {args.environment}\n")
    failed = 0
    for name, run in checks:
        try:
            detail = run()  # type: ignore[operator]
        except Failure as exc:
            failed += 1
            print(f"  [FAIL] {name} — {exc}")
        except Exception as exc:  # network, DNS, TLS
            failed += 1
            print(f"  [FAIL] {name} — {type(exc).__name__}: {exc}")
        else:
            print(f"  [PASS] {name} — {detail}")

    print(f"\n{len(checks) - failed} passed, {failed} failed")
    if failed:
        print("\nRoll back: docs/runbooks/bad-deployment.md")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
