# Phase 2 — Entry Audit

Independent verification of the Phase 1 state. Every claim below was re-derived
from the working tree, not read from the Phase 1 report.

---

## 1. Git state

```
HEAD    : 594c7585fdb7ab7b448ed3ea30beec2e18160b9f
branch  : main
protected-path status (src config migrations infra tests .github) : clean
Demo folder changes : 29 entries (9 modified, 20 untracked trees)
```

`HEAD` still equals the Phase 1 baseline commit. **No commit was created in
Phase 1**, as reported.

## 2. Protected-path checksums

Re-hashed all 199 files in `protected-tutor-baseline.json`:

```
files verified : 199
drift          : 0
missing        : 0
```

**Phase 1's protected-path claim is confirmed independently.**

## 3. Demo test run

```
260 passed
coverage 82.19% (floor 80%)
```

Matches the report exactly.

## 4. TODO / FIXME / stub scan

Five hits across 112 source files. Four are legitimate:

| Location | Verdict |
|---|---|
| `config/settings.py:51` | `_PLACEHOLDER_PREFIXES` — a detector for placeholder secrets, not a placeholder |
| `security/urls.py:74` | `pass` in an `except ValueError` — intentional control flow (host is not an IP) |
| `storage/data_api/client.py:55` | same pattern |
| `resilience/circuit.py:49` | `# type: ignore[operator]` on the injected monotonic clock — narrow and correct |

**One finding worth fixing** (carried into this phase):

`orchestration/commands.py:492` —
`extraction.analysis.has("tutor_fit_concern")  # type: ignore[arg-type]`

`ObjectionAnalysisV1.has()` is typed to take an `ObjectionCategory`. A raw
string works at runtime only because `ObjectionCategory` is a `StrEnum`, so
membership happens to compare equal. The `type: ignore` hides a genuine type
error, and a future change from `StrEnum` to `Enum` would silently make the
tutor-fit routing stop firing. **Fixed in this phase.**

No `NotImplementedError`, no `raise NotImplemented`, no mock/stub left on a
production path.

## 5. Dependency scan

Runtime dependencies are seven, all pure-Python and Lambda-friendly:

`httpx`, `openai`, `pydantic`, `pydantic-settings`, `python-dateutil`, `pyyaml`,
`tzdata`. `boto3` is dev/`aws` extra only (it is present in the Lambda runtime).

No web framework, no ORM, no database driver, **no Redis client**. Nothing to
remove.

`pip-audit` was not executed in Phase 1 and is still pending — it needs
`uv export` against a project-local environment, and this checkout shares the
parent venv. Carried into §28.

## 6. Infrastructure scan

```
Demo Intelegence Agent/infra/ : 0 files
root .github/workflows/       : ci.yml, deploy.yml, rollback.yml  (all Tutor's)
```

**Phase 1 wrote no infrastructure and no Demo CI.** Both are the core of this
phase.

### What the Tutor IaC does, and why Demo must differ

Read `infra/terraform/{versions,main}.tf` read-only. Two of its choices are
prohibited for *new Demo* resources:

| Tutor does | Demo must not | Demo does instead |
|---|---|---|
| `backend "s3"` for Terraform state | no new S3 | backend left unconfigured; the operator supplies an already-approved non-S3 backend at `init`, or accepts local state. Documented, not silently created. |
| `s3_bucket`/`s3_key` for Lambda code | no S3 artifacts | `filename` + `source_code_hash` — direct ZIP upload, with a build-time size gate |
| VPC + RDS Proxy + `rds-db:connect` | — | Data API from outside the VPC; **no VPC config, no NAT, no security group** |
| An S3 analytics bucket | no new S3 | rollups live in `dcc` tables |

**Tutor's infrastructure is not touched.** The prohibition applies to new Demo
resources; removing Tutor's S3 bucket because Demo may not create one would be
exactly the kind of collateral change the protection rule forbids.

Naming convention followed: Tutor uses `tutor-match-meta-${var.environment}`;
Demo uses `demo-command-center-${var.environment}`.

## 7. Gaps this phase must close

From Phase 1's own "remaining work", plus what this audit adds:

| # | Gap | Source |
|---|---|---|
| 1 | No Demo infrastructure at all | this audit §6 |
| 2 | No Demo CI/CD | this audit §6 |
| 3 | One Lambda module for all workers; no per-capability functions | code read |
| 4 | `glue/` holds only the E2E harness — no routing, envelopes or sagas | code read |
| 5 | `memory/` and `cache/` do not exist | code read |
| 6 | No tool registry; capability dispatch is a dict in `commands.py` | code read |
| 7 | No HITL packet builder; `human_handoff/` is empty | code read |
| 8 | Circuit breaker exists but is not wired into any provider | code read |
| 9 | No error classification taxonomy or jittered backoff | code read |
| 10 | No LLM cost ceilings enforced (settings exist, nothing reads them) | code read |
| 11 | No drift evaluation | code read |
| 12 | Seven of ten aggregates still in-memory under `data_api` | Phase 1 §14 |
| 13 | `type: ignore` hiding a real type error | this audit §4 |
| 14 | `pip-audit` never executed | this audit §5 |

Proceeding to implement.
