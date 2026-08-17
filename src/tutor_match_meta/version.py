"""Build identity — what is actually deployed right now.

During an incident the first question is never "what does the code do", it is
"which code is running". Six answers matter, and they change independently:

    app version           the package version
    git SHA               the exact commit
    schema version        the Alembic head this build expects
    scoring policy        which weights produced a decision
    prompt versions       which instructions the model received
    event contract        what shape callers may send

Decoupling them is the point. A bad prompt is rolled back by changing a prompt
version, not by redeploying the application (§33 of the hardening brief), and
that is only auditable if the two are reported separately.

Everything here is read from the environment at import time and is cheap; the
deploy pipeline stamps `TMM_GIT_SHA` and `TMM_BUILD_ID` into the Lambda
environment. Nothing here is a secret, so the full block is safe to return from
an operator-authenticated health surface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

#: Alembic head this build expects. Bumped in the same commit as the migration,
#: so a version mismatch at boot means the migration did not run.
EXPECTED_SCHEMA_REVISION = "0005"

#: The inbound/outbound event contract generation. Callers pin against this.
EVENT_CONTRACT_VERSION = "v1"

UNKNOWN = "unknown"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip() or UNKNOWN


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Immutable snapshot of what this process is."""

    app_version: str
    git_sha: str
    build_id: str
    schema_revision: str
    event_contract_version: str
    #: `policy_id.version` of the default scoring policy. Per-decision policy is
    #: stamped on the decision row; this is only the deployed default.
    default_policy: str
    #: `prompt_id -> version` for every prompt this build can send.
    prompt_versions: dict[str, str]

    @property
    def short_sha(self) -> str:
        return self.git_sha[:12] if self.git_sha != UNKNOWN else UNKNOWN

    def as_dict(self) -> dict[str, object]:
        return {
            "app_version": self.app_version,
            "git_sha": self.git_sha,
            "build_id": self.build_id,
            "schema_revision": self.schema_revision,
            "event_contract_version": self.event_contract_version,
            "default_policy": self.default_policy,
            "prompt_versions": dict(self.prompt_versions),
        }


def _app_version() -> str:
    try:
        from importlib.metadata import version

        return version("tutor-match-meta")
    except Exception:  # pragma: no cover - source checkout without an install
        return _env("TMM_APP_VERSION")


@lru_cache(maxsize=1)
def build_info() -> BuildInfo:
    """Process-wide build identity. Cached: nothing here changes at runtime."""
    from tutor_match_meta.prompts.registry import REGISTRY

    default_policy = (os.getenv("TMM_DEFAULT_POLICY") or "regular_school_support.v1").strip()
    return BuildInfo(
        app_version=_app_version(),
        git_sha=_env("TMM_GIT_SHA"),
        build_id=_env("TMM_BUILD_ID"),
        schema_revision=EXPECTED_SCHEMA_REVISION,
        event_contract_version=EVENT_CONTRACT_VERSION,
        default_policy=default_policy,
        prompt_versions={p.prompt_id: p.version for p in REGISTRY.all()},
    )


__all__ = [
    "EVENT_CONTRACT_VERSION",
    "EXPECTED_SCHEMA_REVISION",
    "BuildInfo",
    "build_info",
]
