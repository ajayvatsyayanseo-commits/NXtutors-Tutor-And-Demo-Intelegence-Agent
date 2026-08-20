# Final end-to-end report

**Date:** 2026-08-17
**Scope:** Both halves of the NXTutors Tutor and Demo Intelligence Agent.

Every number below was produced by a command run on this machine on this date.
Where something was not run, it says `NOT EXECUTED` and says why. **A skipped
test is never counted as a pass.**

---

## 1. Summary

| Gate | Result | Status |
| --- | --- | --- |
| Demo — `pytest` | 401 passed | `PASS` |
| Demo — coverage | 82.74% (floor 80%) | `PASS` |
| **Combined sync (`make sync`)** | 30/30 steps, 8/8 invariants | `PASS` — real Tutor agent |
| Demo — `ruff check` | All checks passed | `PASS` |
| Demo — `mypy --strict` | 133 source files, no issues | `PASS` |
| Demo — `bandit` | High 0, Medium 0, Low 10 | `PASS` |
| Demo — `pip-audit` | No known vulnerabilities | `PASS` |
| Demo — `terraform fmt` / `validate` | formatted / Success | `PASS` |
| Demo — prohibited-resource scan | OK | `PASS` |
| Demo — `doctor` | 0 problems, 7 gaps | `PASS` (gaps are unconfigured providers) |
| Demo — `make demo` lifecycle | LIFECYCLE OK | `PASS` |
| **Live database wiring** | all checks PASS | `VERIFIED WITH LIVE PROVIDER` |
| Tutor — `pytest` | 772 passed, 18 skipped, 1 xfailed | `PASS` |
| Protected paths | 199 files, 0 drift | `PASS` |
| Tutor integration tests | — | `NOT EXECUTED` (§7) |
| Live provider calls (Meta/Google/Cashfree/gateway) | — | `NOT EXECUTED` (§7) |

---

## 2. Demo test suite

```
401 passed in 22.58s
Required test coverage of 80.0% reached. Total coverage: 82.74%
```

By marker:

| Marker | Tests | What it pins |
| --- | ---: | --- |
| `contract` | 29 | Versioned external contracts, the Tutor envelope, and the real composition (§5b) |
| `security` | 212 | Security and anti-fabrication invariants |
| `e2e` | 33 | Full-lifecycle flows through the real orchestrator |
| `load` | 4 | Throughput and contention behaviour |

Markers overlap (a test may be both `security` and `e2e`), so these do not sum
to 389.

### Coverage honesty

82.73% is measured over `demo_command_center` with an explicit omit list. Each
omitted path names the external dependency that would be needed, and **omitting
is not testing**. The list is in `pyproject.toml`; the notable entries:

| Omitted | Why | Covered elsewhere? |
| --- | --- | --- |
| `storage/postgres/*` | Needs a live PostgreSQL | **Yes** — `verify_live_wiring.py`, §4 below. Not a pytest run, so counting it here would overstate the unit suite. |
| `storage/data_api/*` | Needs a live Aurora cluster with the Data API | No |
| `integrations/*/client.py` | Needs the respective provider credential | No — see §7 |
| `handlers/*` | AWS event envelope shapes | The services they call are covered |
| `glue/lifecycle.py` | It **is** the e2e harness | It is the test |

---

## 3. The full lifecycle, locally

`make demo` drives one conversation from Lead Intake handoff to converted
customer — 30 steps, through the real orchestrator, the real state machine and
the real outbound boundary. Only the provider edges are doubled.

```
LIFECYCLE OK
```

Events emitted, in order, ending:

```
  - demo.objections_extracted
  - demo.payment_requested
  - subscription.activated
  - onboarding.requested
```

This is the harness that found 14 real bugs during Phase 1 — including the
discount `any`/`all` error, the duplicate calendar event, the swallowed welcome
message and the slot hold leaked on a Google failure. It runs the production
object graph, which is why it found them and unit tests did not.

---

## 4. Live database verification — `VERIFIED WITH LIVE PROVIDER`

`python scripts/verify_live_wiring.py`, run against
`database-1.cv4omwweuj19.ap-south-1.rds.amazonaws.com:5432/demo_command_center`.

This is a real connection to the real shared database, not a fixture.

```
=== 1. shared .env ===
  [PASS] Settings loaded  (environment=local)
  [PASS] Demo has a DSN
  [PASS] DSN inherited from TMM_POSTGRES_DSN

=== 2. schema separation ===
  [PASS] Demo schema  (demo_agent)
  [PASS] Demo does NOT write the protected Tutor schema

=== 3. one database, two schemas ===
  [PASS] both schemas present  (demo_agent, tutor_match)
  [PASS] Demo tables  (37 in demo_agent)
  [PASS] Tutor tables intact  (22 in tutor_match)

=== 4. a real state transition round-trips ===
  [PASS] new conversation starts clean
  [PASS] transition persisted  (OWNERSHIP_ACQUIRING v1)
  [PASS] read back from the database
  [PASS] audit row written in the same transaction

=== 5. optimistic locking ===
  [PASS] stale write rejected with ConcurrencyConflict

=== 6. slot-hold exclusion ===
  [PASS] first hold placed
  [PASS] second hold rejected with SlotConflict

=== 7. cleanup ===
  [PASS] test rows removed
  [PASS] Tutor schema unchanged throughout

WIRING OK — both agents share one .env and one database, with separate schemas.
```

### What this actually proves

| Claim | Proof |
| --- | --- |
| One `.env` serves both agents | `DCC_POSTGRES_DSN` is blank; the DSN came from `TMM_POSTGRES_DSN` |
| One database, two schemas | Both namespaces present in one `pg_namespace` query |
| The migration applied completely | 37 tables in `demo_agent` |
| Demo did not touch Tutor | `tutor_match` table count identical before and after |
| Persistence really works | A transition written, read back, and its audit row in the same transaction |
| Concurrency is safe | A stale-version write was **rejected**, not silently accepted |
| Double-booking is impossible | The second hold on the same `(tutor, minute)` was **refused** |

### Table reconciliation

| Source | Count |
| --- | ---: |
| `CREATE TABLE` in `migrations/0001_dcc_schema.sql` | 36 |
| Live tables in `demo_agent` | 37 |
| Difference | `schema_migrations` — the migration runner's own ledger |

Diffed programmatically: **live-but-not-declared = `['schema_migrations']`**,
**declared-but-not-live = `[]`**. No drift.

---

## 5. Tutor Intelligence regression

Run from the repository root, with the Demo agent present on disk and both
halves reading the same `.env`:

```
772 passed, 18 skipped, 1 xfailed in 12.39s
```

**No regression.** Adding the Demo half changed nothing about the Tutor half's
behaviour, which is what "protected" has to mean in practice as well as in git.

- The **18 skips** are `tests/integration/test_postgres_stores.py`, which needs
  `TMM_INTEGRATION_DSN` pointing at a disposable PostgreSQL. Not set here.
  Recorded as `NOT EXECUTED`, not as passing. The test module's own skip message
  says the same thing.
- The **1 xfail** documents a gap in the upstream *Lead Intake* agent
  (`TUTOR_MATCHING_AGENT_INTERNAL_SECRET` absent from its `app/core/config.py`).
  Pre-existing, unrelated to this work, outside this repository.

---

## 5b. The two agents composing for real — `make sync`

`make demo` drives the lifecycle against a **fake** Tutor agent. `make sync`
(`dcc-sync`) swaps in the **real** `tutor_match_meta` orchestrator, in process.

```
30/30 steps passed        final state: CONVERTED        messages delivered: 7

TUTOR INTELLIGENCE — what the matching half decided
  call 1: Mathematics · CBSE · class 10
    policy       : board_exam_prep@1
    considered   : 3 shortlisted, 0 rejected
    #1 Arjun Desai   [NXT10006]  score=0.827
    #2 Anita Sharma  [NXT10001]  score=0.821
    #3 Rohit Bansal  [NXT10009]  score=0.618
        fee    : — not substantiated, so never quoted

SYNC INVARIANTS
  [PASS] the real Tutor agent was called  (1 call(s))
  [PASS] Tutor Intelligence reports it sent nothing
  [PASS] the Tutor adapter holds no sender and no outbox
  [PASS] every candidate presented came from Tutor Intelligence  (3 shown, all in the 3 returned)
  [PASS] one conversation across the boundary
  [PASS] every call was made in return-only mode
  [PASS] no placeholder host reached the parent
  [PASS] no profile URL was invented  (guard-enforced)

SYNC OK — both agents ran as one product, end to end.
```

This is a stronger statement than the envelope contract tests: those pin the
*shapes*, this proves the *composition*. It refuses to fall back to the fake, so
a green run cannot be a false positive.

### The bug it found

The composition did not work when first attempted. Tutor resolves its relative
`policy_dir` against `Path.cwd()` **first**. With Demo as the caller the cwd is
`Demo Intelegence Agent/`, which has a `config/policies/` of its own, so Tutor
loaded Demo's directory:

```
PolicyError: policy 'board_exam_prep.v1' not found in
  .../Demo Intelegence Agent/config/policies;
  available: ['discount.v1', 'forecast.v1', 'monitoring.v1', 'reminder.v1']
```

Two agents in one process cannot share a cwd-relative config path. Fixed on the
**Demo** side — Tutor is protected — by `_tutor_policy_dir()`, which anchors on
the `tutor_match_meta` package location and accepts a directory only if it
actually contains Tutor's default policy. An `is_dir()` check would have matched
Demo's directory and re-introduced the collision.

Pinned by 12 tests in `tests/contract/test_combined_sync.py`, one of which
asserts the two policy name sets stay disjoint — because if someone later adds
`board_exam_prep.v1.yaml` to Demo's directory, the other tests still pass while
the collision quietly returns.

### The second thing it surfaced

With the real agent wired in, `make sync` delivered **7** messages where
`make demo` delivered **6**. The missing one was the tutor options message.

The fake pinned its profile URLs to `https://nxtutors.example`, which
`tests/conftest.py` configures as the allowlisted host — so the fake was
self-consistent in tests, but under the real `.env`
(`DCC_WEBSITE_PUBLIC_BASE_URL=https://www.nxtutors.com`) the outbound guard
refused the message with `unapproved_url`. The lifecycle still passed all 30
steps; one message simply never appeared.

The fake now derives its host from the same setting the real adapter uses, so
both paths deliver 7 messages and the guard is exercised where it belongs — in
`tests/security/test_guardrails.py`, on purpose, rather than by accident on
every local run.

---

## 6. Cross-agent integration

| Check | Result |
| --- | --- |
| Demo can construct the Tutor orchestrator | Yes — `local_adapter.py`, import guarded |
| The adapter falls back cleanly when Tutor is absent | Yes — `FakeTutorIntelligence`, with a warning |
| Envelope shapes are compatible in both directions | Yes — contract test, pinned |
| Every presented candidate came from the Tutor agent | Yes — asserted against the fixture set |
| The Tutor path can send a WhatsApp message | **No** — no object in its graph satisfies `WhatsAppPort` |
| Demo reads or writes a `tutor_match` table | **No** — no statement in `storage/` names one |

The fifth row is the one that matters. "Return-only" is not a policy here; it is
the absence of a sender.

---

## 7. NOT EXECUTED — stated plainly

Nothing below was run. None of it is counted as passing anywhere in this report.

| Item | Why | What it would take |
| --- | --- | --- |
| Meta WhatsApp send | No WABA credential in this environment | `DCC_META_ACCESS_TOKEN`, `DCC_META_PHONE_NUMBER_ID`, an approved template |
| Google Calendar event + Meet link | No Google service-account credential | `DCC_GOOGLE_*` and a calendar the service account may write |
| Cashfree order + webhook | No merchant account | `DCC_CASHFREE_APP_ID`, `DCC_CASHFREE_SECRET_KEY`, a reachable webhook URL |
| NXTutors gateway | Base URL not configured | `DCC_GATEWAY_BASE_URL` and a signing secret |
| OpenAI | No API key; the offline stub is in use | `DCC_OPENAI_API_KEY` |
| EventBridge Scheduler | Not configured | `DCC_SCHEDULER_GROUP_NAME`, `DCC_SCHEDULER_ROLE_ARN` |
| Aurora Data API path | The shared database is a plain RDS instance with no Data API | A cluster with the Data API enabled |
| Tutor integration tests (18) | `TMM_INTEGRATION_DSN` not set | A disposable PostgreSQL |
| Load test against deployed infra | Nothing is deployed | A deployed stack; §8 explains what the local profiles can and cannot tell you |
| `terraform plan` / `apply` | No AWS credentials, and a plan against a real account is an operator action | OIDC role assumption, an operator `<env>.tfvars` |

---

## 8. Load profiles — what they measure and what they do not

```
4 passed
baseline: 10/10 completed, p50=7.93ms p95=9.72ms p99=9.72ms errors=0  wall=0.17s
per completed demo: llm=2 tutor_match=1 calendar_events=1 messages=7
50-way slot contention resolved in 1.2ms
turn latency after 60 turns: 0.0ms
```

**These are in-process numbers against simulated providers.** They cannot
measure Lambda cold starts, SQS delivery, or real provider latency, and they are
not a latency SLO. Treating 7.93ms as a production p50 would be dishonest.

What they *do* measure is the thing a deployed test cannot isolate — how much
work the orchestrator itself does per conversation:

| Counter | Value | Ceiling | Why it is watched |
| --- | ---: | ---: | --- |
| LLM calls per demo | 2 | 4 | Directly the OpenAI bill |
| Tutor match calls per demo | 1 | 3 | A loop here means re-matching on every turn |
| Calendar events per demo | 1 | 1 | Exactly 1 — a reschedule must patch, never create |
| Messages per demo | 7 | 10 | A regression here is a spam incident |
| Turn cost after 60 turns | flat | <500ms | Guards against loading the full transcript per turn |

The contention result is the important one: 50 coroutines racing one slot
produced **1 winner and 49 clean refusals**, and the same exclusion was then
confirmed against the real database in §4.

---

## 9. Environment coverage

| Fact | Value |
| --- | ---: |
| `.env` lines | 322 |
| `TMM_*` keys (Tutor, pre-existing) | 91 (12 intentionally blank) |
| `DCC_*` keys (Demo, added) | 109 (30 intentionally blank) |
| `TMM_*` values modified | **0** |
| Backup before the merge | `.env.backup-before-dcc-merge` |

The blank keys are deliberate and were requested: every variable the system can
read is present and named, including the ones with no value yet, so an operator
can see the whole configuration surface in one file rather than discovering a
missing key at runtime.

One blank is load-bearing: `DCC_POSTGRES_DSN=` is empty **so that** the shared
`TMM_POSTGRES_DSN` is inherited. Filling it in would create a second copy of the
connection string that can drift.

> Note on `.env` comments: comments are on their own lines, never trailing a
> value. `KEY=    # comment` was a real bug — the DSN parsed as the literal
> comment text, the shared-DSN fallback never fired, and the failure surfaced
> much later as a connection error. `scripts/merge_env.py` can no longer emit
> that shape.

---

## 10. Reproducing this report

```bash
# Demo
cd "Demo Intelegence Agent"
python -m pytest tests -q --cov=demo_command_center     # 389 passed, 82.73%
python -m pytest tests -m load -q -s                    # 4 passed + counters
python -m ruff check src tests scripts
python -m mypy src
python -m bandit -c pyproject.toml -r src
python -m pip_audit
python scripts/scan_prohibited.py
python scripts/verify_live_wiring.py                    # touches the real database
python -m demo_command_center.cli.doctor
python -m demo_command_center.cli.local_e2e
cd infra/terraform && terraform fmt -check -recursive && terraform validate

# Tutor regression
cd ../../..
python -m pytest tests -q                               # 772 passed, 18 skipped

# Protection
git status --porcelain -- src/tutor_match_meta config/policies migrations \
    infra/terraform tests .github/workflows/{ci,deploy,rollback}.yml   # empty
```

`verify_live_wiring.py` writes to the real database and deletes what it wrote.
It leaves no residue, and it asserts the Tutor schema is unchanged before it
finishes. Everything else is offline.
