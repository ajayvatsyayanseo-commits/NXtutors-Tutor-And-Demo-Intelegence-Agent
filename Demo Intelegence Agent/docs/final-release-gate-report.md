# Final combined release gate report

# NXTutors Tutor and Demo Intelligence Agent

**Date:** 2026-08-17
**Repository HEAD:** `594c7585fdb7ab7b448ed3ea30beec2e18160b9f` (unchanged — no commit was made)

---

# VERDICT

## `READY FOR STAGING — LIVE PROVIDER VERIFICATION REQUIRED`

Every gate that can be run without an external credential passes. Eight gaps
block production, and **every one is an external credential, an external
contract, or a decision that belongs to an operator** — none is unfinished code.

This verdict is not "ready for production". It should not be read as one.

---

## 1. Scope

The combined agent: 16 register agents in two halves that behave as one product.

| Half | Package | Schema | Source files |
| --- | --- | --- | ---: |
| Tutor Intelligence (013, 014, 015, 016, 017, 019, 021, 022) | `tutor_match_meta` | `tutor_match` | 131 (protected) |
| Demo Command Center (129, 018, 025, 026, 031, 032, 034, 036) | `demo_command_center` | `demo_agent` | 133 |

## 2. Gate summary

| # | Gate | Result | Status |
| ---: | --- | --- | --- |
| 1 | Demo test suite | 401 passed | `PASS` |
| 2 | Demo coverage | 82.74% (floor 80%) | `PASS` |
| 2b | **Combined sync — real Tutor agent** | 30/30 steps, 8/8 invariants | `PASS` |
| 3 | Demo lint (`ruff`) | All checks passed | `PASS` |
| 4 | Demo types (`mypy --strict`) | 133 files, no issues | `PASS` |
| 5 | Demo security (`bandit`) | High 0, Medium 0, Low 10 | `PASS` |
| 6 | Dependency audit (`pip-audit`) | No known vulnerabilities | `PASS` |
| 7 | Prohibited-resource scan | OK | `PASS` |
| 8 | Terraform `fmt` + `validate` | formatted / Success | `PASS` |
| 9 | `doctor` coherence | 0 problems, 7 gaps | `PASS` |
| 10 | Full lifecycle (`make demo`) | LIFECYCLE OK | `PASS` |
| 11 | Load profiles | 4 passed | `PASS` |
| 12 | **Live database wiring** | all checks PASS | `VERIFIED WITH LIVE PROVIDER` |
| 13 | Tutor regression | 772 passed, 18 skipped, 1 xfailed | `PASS` |
| 14 | Protected-path integrity | 199 files, 0 drift | `PASS` |
| 15 | Terraform `plan` / `apply` | — | `NOT EXECUTED` |
| 16 | Live provider calls | — | `NOT EXECUTED` |
| 17 | Tutor integration tests | — | `NOT EXECUTED` |
| 18 | Deployed load test | — | `NOT EXECUTED` |

**No gate failed. Four were not executed and are named as such.**

## 3. Protected-path integrity

| Path | Files | Drift |
| --- | ---: | ---: |
| `src/tutor_match_meta/` | 131 | 0 |
| `config/policies/` | 8 | 0 |
| `migrations/` | 7 | 0 |
| `infra/terraform/` | 10 | 0 |
| `tests/` | 40 | 0 |
| `.github/workflows/{ci,deploy,rollback}.yml` | 3 | 0 |
| **Total** | **199** | **0** |

Tree hash of `src/tutor_match_meta`: `9637b9514ca3533b28168601c2c54335de444980`.
Full evidence: `checkpoints/final-protected-path-verification.md`.

## 4. The two agents as one product

Verified live, not asserted:

| Property | Evidence |
| --- | --- |
| One `.env` serves both | `DCC_POSTGRES_DSN` blank; the DSN came from `TMM_POSTGRES_DSN` |
| One database, two schemas | `demo_agent` (37 tables) and `tutor_match` (22) in one `pg_namespace` query |
| Demo does not touch Tutor | Tutor table count identical before and after Demo writes |
| Tutor cannot send | No object in its graph satisfies `WhatsAppPort` |
| Envelope compatibility | Contract test, pinned both directions |
| **They actually compose** | `make sync` — the real `tutor_match_meta` orchestrator drives the real Demo lifecycle, 30/30 steps, 8/8 sync invariants |
| No Tutor regression | 772 passed with Demo present and sharing the `.env` |

`make sync` is the strongest of these. The envelope tests pin the *shapes*; this
runs both agents in one process and proves the *composition*, refusing to fall
back to the fake so a green run cannot be a false positive. It found a real
integration bug — a `config/policies` collision between the two agents — which
is documented in `final-e2e-report.md` §5b and pinned by 12 tests.

## 5. Ownership model

Exactly one owner at a time. A capability call is not a transfer.

| Movement | Ownership |
| --- | --- |
| Lead Intake → Demo | **Transfers** (signed handoff) |
| Demo → Tutor Intelligence | **Does not move** (in-process call, return-only) |
| Demo → Onboarding | **Transfers** (signed, idempotent, exactly once) |

## 6. State machine

30 states, 49 triggers, 7 actors, 45 declared transitions. Table-driven and
pure — no I/O, no provider call, no ambient clock. Persistence is optimistic:
every write carries `expected_version`; a stale write raises
`ConcurrencyConflict`. **Verified live**, not only in memory.

## 7. Capability isolation

Eight Demo capabilities, one Lambda each, wrapped in `_batch(...)` returning
`batchItemFailures` so one poisoned message does not re-drive its batch. Only
the orchestrator writes conversation state.

Full matrix: `final-capability-matrix.md`.

## 8. The outbound boundary

One path. Nothing else may call the sender. It applies, in order: output guard →
PII rules → opt-out → template rules → idempotency.

Because there is one boundary, a new rule is one edit and a forgotten message
type is impossible.

## 9. Data ownership

| | Tutor | Demo |
| --- | --- | --- |
| Schema | `tutor_match` | `demo_agent` |
| Tables | 22 | 36 declared + 1 migration ledger = 37 live |
| Shared tables | **0** | **0** |

Table reconciliation was diffed programmatically: live-but-not-declared =
`['schema_migrations']`; declared-but-not-live = `[]`. No drift.

## 10. Security posture

| Property | Status |
| --- | --- |
| Threats modelled | 34, each with a control and a test |
| Security-marked tests | 212, all passing |
| `bandit` High / Medium | **0 / 0** |
| Known vulnerable dependencies | **0** |
| `Action:"*"` + `Resource:"*"` in IAM | **Nowhere** |
| Credentials in code / IaC / CI | **None** — OIDC only |
| Long-lived AWS keys | **Not used** |

Detail, including the ten Low bandit findings individually: `final-security-report.md`.

## 11. Anti-fabrication

| Never invented | Decided by |
| --- | --- |
| A tutor fact | Ordinal lookup in the persisted candidate snapshot |
| A price or discount | Deterministic band engine with a floor |
| A Meet link | Google, or the message says it is unavailable |
| A payment | HMAC over raw bytes + exact amount reconciliation |
| Attendance | RSVP or conference participation |
| A provider's answer when it is down | A declared `Degradation`, never a guess |

## 12. Model authority

**None.** 12 registered tools, **5 model-facing**; no financial or booking tool
among them, and `FORBIDDEN_TOOL_NAMES` blocks re-adding one by name. Every
proposal passes a 9-stage authorisation pipeline.

## 13. Resilience

Errors are classified before retry (`ErrorClass` × `Disposition`), with
full-jitter exponential backoff. Per-provider circuit breakers carry a declared
degradation. Two sagas (`BOOKING`, `PAYMENT`), 5 steps each, compensating in
reverse order.

## 14. Serverless constraint compliance

| Prohibited for new Demo resources | Present |
| --- | --- |
| EC2 / ECS / Fargate / EKS | No |
| Always-running worker | No |
| NAT Gateway | No |
| Redis / ElastiCache | No |
| New S3 bucket / S3 Lambda artifacts | No |
| Direct Laravel / MySQL access | No |
| New database cluster | No |
| Long-lived AWS credentials in GitHub | No |

Enforced by `scripts/scan_prohibited.py` (16 resource types, 4 argument
patterns, 8 modules) in CI, and by `build_lambda.py`, which **fails** rather
than falling back to S3.

## 15. Infrastructure

13 Lambda functions · 5 queue lanes + 5 DLQs · 16 alarms · 8 policy documents ·
0 S3 buckets · 0 NAT Gateways · no `vpc_config`.

`persistence_mode` is now a Terraform variable **with no default**, so a deploy
cannot silently pick a backend the database does not support. In `postgres_dsn`
mode the `rds-data` grant is not issued at all.

## 16. CI/CD

Three path-filtered workflows over `Demo Intelegence Agent/**`: CI, deploy,
rollback. GitHub OIDC, production environment approval, ZIP size gate,
prohibited-resource scan, plan-destroy check.

Rollback moves a Lambda alias to a previous published version. **It does not
re-run migrations and cannot destroy payment or scheduling state** — automating
that was explicitly prohibited and is not automated.

## 17. Observability

Structured JSON logs with redaction applied at the logger. Metrics and alarms
grouped by consequence: money, correctness, customer impact, cost. Log retention
finite (30 days default, validated ≤365).

## 18. Cost

Measured per completed demo: **2 LLM calls, 1 tutor match, 1 calendar event, 7
messages** — all under their asserted ceilings.

Fixed monthly cost attributable to new Demo resources: **CloudWatch logs and
alarms only.** No NAT Gateway, no idle compute, no cache cluster, no new
database.

No cloud price is stated as fact anywhere; `docs/operations/cost-model.md` is
parameterised. Detail: `final-cost-report.md`.

## 19. Performance

In-process, simulated providers, one machine — **not production latencies**:
baseline 10/10 lifecycles, p50 7.93ms, 0 errors; 50-way slot contention → 1
winner, 49 clean refusals, 1.2ms; turn cost flat after 60 turns.

`peak`, `burst`, `stress` and `soak` are defined and **were not run**. No
throughput or user-count claim is made. Detail: `final-performance-report.md`.

## 20. Configuration

322 lines in one `.env`: 91 `TMM_*` (12 blank) + 109 `DCC_*` (30 blank). **Zero
`TMM_*` values modified.** Backup at `.env.backup-before-dcc-merge`.

Blank keys are deliberate and were requested — the whole configuration surface
is visible in one file. One blank is load-bearing: `DCC_POSTGRES_DSN=` is empty
so the shared DSN is inherited and cannot drift.

## 21. Skipped and not-executed — the honest list

**Nothing below is counted as passing anywhere in this report.**

| Item | Status | Reason |
| --- | --- | --- |
| 18 Tutor integration tests | `NOT EXECUTED` | `TMM_INTEGRATION_DSN` unset; needs a disposable PostgreSQL |
| 1 Tutor `xfail` | Expected failure | Upstream Lead Intake gap, outside this repository |
| Meta / Google / Cashfree / gateway calls | `NOT EXECUTED` | No live credentials |
| OpenAI calls | `NOT EXECUTED` | No API key; offline stub in use, spend to date zero |
| EventBridge Scheduler | `NOT EXECUTED` | Not configured |
| Aurora Data API path | `NOT EXECUTED` | The shared database has no Data API |
| `terraform plan` / `apply` | `NOT EXECUTED` | No AWS credentials; an operator action |
| 4 of 5 load profiles | `NOT EXECUTED` | Only `baseline` runs in CI |
| Deployed load test | `NOT EXECUTED` | Nothing is deployed |

## 22. Gaps that block production

| # | Gap | Class |
| ---: | --- | --- |
| 1 | Meta WhatsApp credentials | External credential |
| 2 | One truncated template name | Business configuration — **deliberately not guessed** |
| 3 | Google Calendar credentials | External credential |
| 4 | Cashfree merchant account | External credential |
| 5 | NXTutors gateway URL + response schema | External contract |
| 6 | EventBridge Scheduler configuration | External credential |
| 7 | `persistence_mode` + its network consequence | Operator decision |
| 8 | Terraform state backend | Operator decision |

All sixteen gaps, with resolutions and costs: `final-integration-gaps.md`.

## 23. Bugs found and fixed

Fourteen during Phase 1, all found by gates rather than by inspection. The five
with real customer or money consequences:

| Bug | Consequence had it shipped |
| --- | --- |
| Discount bands matched ANY trigger instead of ALL | ~5% overpaid on every price-sensitive customer |
| `approve()` refused when no band matched | A full-price customer could **never** be sent a payment link |
| Reschedule created a second calendar event | Duplicate tutor events; the parent got the stale link |
| Google failure left the slot hold | The tutor became unbookable at that time, permanently |
| Commands accepted a caller-supplied `tutor_ref` | A crafted message could name any tutor, bypassing matching entirely |

Plus, this phase: asyncpg's `RESET ALL` wiping `search_path` (fixed with
`setup=`), and `.env` inline comments parsing as values (fixed in the file and
in the generator, so it cannot recur).

## 24. Instruction compliance

| Instruction | Compliance |
| --- | --- |
| Do not commit or push | **No commit, no push.** `HEAD` unchanged |
| Tutor Intelligence must not be rewritten | **199 files, 0 drift** |
| Never `git reset --hard` or destructive checkout | Never used. Scaffold restore used `git cat-file blob` |
| Both agents share the same `.env` | One file, 322 lines, verified live |
| Add all required env vars, including empty ones | 109 `DCC_*` keys, 30 intentionally blank |
| No hardcoded credentials, prices, IDs, ARNs, model IDs | None. Scanned and asserted |
| No backend Python or credentials in Laravel `public/` | None |
| Do not create or rename templates in code | Not done |
| Do not guess a truncated template name | **Not guessed.** Reported as a gap |
| No long-lived AWS keys | OIDC only |
| Do not automate destructive rollback | Alias move only; no migration rollback |
| Do not mark a skipped test as passed | §21 lists every skip as `NOT EXECUTED` |
| No unsupported scale claims | None made anywhere |
| Update the README | Rewritten as one combined README |

---

# Final verdict

## `READY FOR STAGING — LIVE PROVIDER VERIFICATION REQUIRED`

**What is proven.** The composition is real and mechanically asserted: 16 agents
registered and reachable, one owner per conversation, one send path, one
deterministic state machine, one database with two non-overlapping schemas,
optimistic locking and slot exclusion **verified against the live shared
database**. 1,161 tests pass across both halves. No High or Medium security
finding, no known vulnerable dependency, no prohibited resource, and the
protected Tutor agent is byte-identical to where it started.

**What is not proven.** Not one message has been sent to a real parent, not one
calendar event created, not one rupee moved, and nothing has been deployed.
Every provider contract is exercised against fixtures.

**The next step is a staging deployment with real credentials**, in this order —
Meta first (its template rejection is the fastest signal), then Google
(asynchronous conference creation is the least predictable), then Cashfree in
sandbox (highest consequence, so verify it under a safety net), then the
gateway.

No production traffic until §22 is empty.
