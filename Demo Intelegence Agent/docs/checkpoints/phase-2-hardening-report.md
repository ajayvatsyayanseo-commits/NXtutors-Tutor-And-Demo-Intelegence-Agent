# Phase 2 — Hardening Report

Baseline commit: `594c7585fdb7ab7b448ed3ea30beec2e18160b9f`.
**No git commit was created.** `HEAD` is unchanged.

This report describes what was built and what was verified. It does not claim
production readiness — §16 lists what still stands between this and a
production deploy.

---

## 1. Entry-audit findings

Full detail in `phase-2-entry-audit.md`. Independently re-derived, not read
from the Phase 1 report:

| Check | Result |
|---|---|
| `HEAD` | unchanged at the Phase 1 baseline; no commit was made |
| Protected checksums | 199 files, **0 drift** |
| Demo tests | 260 passed, 82.19% coverage — matches the report |
| TODO/stub scan | 5 hits, 4 legitimate |
| Dependencies | 7 runtime, all pure Python, **no Redis client** |
| Demo `infra/` | **empty** — Phase 1 wrote no infrastructure |
| Demo CI | **absent** — the three root workflows were all Tutor's |

**One code defect found and fixed:** `commands.py` passed a raw string to
`ObjectionAnalysisV1.has()` under a `# type: ignore[arg-type]`. It worked only
because `ObjectionCategory` is a `StrEnum`; changing it to a plain `Enum` would
have silently stopped tutor-fit routing from ever firing.

**Fourteen gaps** were catalogued and all were closed this phase.

---

## 2. Protected-path verification

```
protected files verified : 199
drift                    : 0
git status -- src config migrations infra tests : (empty)
HEAD == baseline         : yes
Tutor suite after Phase 2: 772 passed, 18 skipped, 1 xfailed
```

The three new root workflows are **additive** — `ci.yml`, `deploy.yml` and
`rollback.yml` are untouched:

```
?? .github/workflows/demo-command-center-ci.yml
?? .github/workflows/demo-command-center-deploy.yml
?? .github/workflows/demo-command-center-rollback.yml
```

### One deliberate divergence from the Tutor stack, and why

Tutor uses `backend "s3"` and `s3_bucket`/`s3_key` for Lambda code. Both are
prohibited for **new Demo** resources. Tutor's infrastructure was **not
touched**: removing its S3 bucket because Demo may not create one would be
exactly the collateral change the protection rule exists to prevent.

Demo instead uses direct ZIP upload with a build-time size gate, and leaves the
Terraform backend unconfigured with the decision escalated in
`docs/operations/terraform-state.md`.

---

## 3. Threat model and controls

`docs/security/threat-model.md` covers **34 threats**. Every one has an
implemented control and a test that fails if the control is removed.

The controls worth naming, because they are structural rather than procedural:

| Property | Enforced by |
|---|---|
| Only one module can send a WhatsApp message | AST test over every `WhatsAppPort` import |
| No domain module can open a socket | AST test over imports in five layers |
| No SQL is built by interpolation | AST test over every f-string containing SQL |
| A payment order cannot carry an unauthorised amount | `from_offer` is the only constructor, and takes `ApprovedOffer` |
| A tutor cannot be booked from an unpresented reference | facts-only read, `tutor_in_snapshot` re-asserted at hold time |
| A discount cannot breach the floor | engine check **and** a database CHECK |
| Drift cannot auto-deploy | `AUTO_APPLY_ENABLED = False`, asserted |
| No capability can write another's tables | `WRITES` map, no-shared-owner test |
| No arbitrary SQL/HTTP/shell tool exists | absent from the registry, asserted against `FORBIDDEN_TOOL_NAMES` |

### A real gap the new tests found

`tests/security/test_pii_and_secrets.py` drives the **real** logging stack and
immediately failed on four cases: **OpenAI keys, Meta tokens, Cashfree secrets
and HMAC signatures were not redacted.** The PII filter covered customer
identifiers but no provider credential — so any error path echoing a token
would have leaked an account rather than a person.

Fixed by adding seven credential patterns to `security/pii.py`, ordered so a
private-key block is redacted before later rules shred it into recognisable
fragments.

---

## 4. Serverless topology

```
                    Meta Function URL          Cashfree Function URL
                    (signature = auth)         (signature = auth)
                           │                          │
                    ┌──────▼──────┐            ┌──────▼──────────┐
                    │  ingress    │            │ cashfree-webhook│
                    │ verify·dedup│            │ verify·reconcile│
                    │ enqueue·200 │            │                 │
                    └──────┬──────┘            └──────┬──────────┘
                           │                          │
              ┌────────────▼──────────┐    ┌──────────▼──────────┐
              │ scheduling.fifo       │    │ payment.fifo        │
              │ group = conversation  │    │ group = conversation│
              └────────────┬──────────┘    └──────────┬──────────┘
                           │                          │
                    ┌──────▼───────┐           ┌──────▼────────┐
                    │ orchestrator │           │ payment-worker│
                    │ THE owner    │           │ concurrency 5 │
                    └──────┬───────┘           └──────┬────────┘
                           │ fan out                  │
        ┌──────────────────┼──────────────┬───────────┘
        │                  │              │
   ┌────▼─────┐      ┌─────▼──────┐  ┌────▼──────┐
   │reminders │      │ analytics  │  │ outbound  │
   │ (std)    │      │ (std)      │  │ (std)     │
   └────┬─────┘      └─────┬──────┘  └────┬──────┘
        │                  │              │
  reminder-worker   forecast/objection/  outbound-worker
                    conversion/discount/  ONLY sender
                    monitoring workers         │
                                          Meta Cloud API

  EventBridge Scheduler ──► reminder-worker (one-shot, replace-by-name)
  EventBridge Rules ──────► sweeps: reminders, outbox relay, drift, rollups

  Persistence: Aurora Data API — a public AWS endpoint.
  NO VPC config on any function. NO NAT Gateway. NO S3. NO Redis.
```

**Five lanes, five queues, five concurrency pools.** A reminder storm or an
analytics backlog physically cannot delay a payment webhook, because they are
different queues with different reserved concurrency — not a comment about
priority.

FIFO only where ordering matters (scheduling, payment), with
`deduplication_scope = messageGroup` and `fifo_throughput_limit =
perMessageGroupId` so ordering holds *within* a conversation while unrelated
conversations run wide. Analytics is deliberately standard and grouped by event
type — two forecasts for different conversations have no ordering relationship.

---

## 5. Lambda and queue mapping

| Function | Handler | Queue | Timeout | Mem | Concurrency |
|---|---|---|---|---|---|
| `ingress` | `webhooks.meta_webhook` | → scheduling | 10s | 512 | 50 |
| `cashfree-webhook` | `webhooks.cashfree_webhook` | → payment | 15s | 512 | 50 |
| `orchestrator` | `workers.work_queue` | scheduling.fifo | 60s | 1024 | 40 |
| `scheduling-worker` | `capabilities.demo_scheduling_worker` | scheduling.fifo | 90s | 1024 | 20 |
| `reminder-worker` | `workers.reminder_sweep` | reminders | 60s | 512 | 10 |
| `outbound-worker` | `workers.outbox_relay` | outbound | 30s | 512 | 20 |
| `payment-worker` | `capabilities.demo_paid_transition_worker` | payment.fifo | 60s | 1024 | **5** |
| `forecast-worker` | `capabilities.demo_forecast_worker` | analytics | 120s | 512 | 30 |
| `objection-worker` | `capabilities.demo_objection_worker` | analytics | 120s | 1024 | 30 |
| `conversion-worker` | `capabilities.demo_conversion_worker` | analytics | 120s | 512 | 30 |
| `discount-worker` | `capabilities.demo_discount_worker` | analytics | 120s | 512 | 30 |
| `monitoring-worker` | `capabilities.demo_monitoring_worker` | analytics | 120s | 1024 | 30 |
| `ops-api` | `workers.internal_handoff` | — | 15s | 512 | 10 |

Payment has the **smallest** pool and the **highest** priority: it is low
volume and high consequence, and a wide pool only widens the window in which
two workers race one order.

Every queue's visibility timeout is 6× its consumer's timeout. Every mapping
reports `ReportBatchItemFailures`. Decision lanes use `batch_size = 1` so
unrelated conversations do not serialise; delivery and analytics lanes batch.

---

## 6. IAM matrix

Nine roles, one per function class. **No role uses `Action: "*"` with
`Resource: "*"`.** `AWSLambdaVPCAccessExecutionRole` is attached to nothing —
no Demo function is VPC-attached.

| Role | May | May NOT |
|---|---|---|
| **ingress** | send to scheduling + payment queues; read the webhook secret; KMS | read the database · call any provider · activate anything |
| **orchestrator** | Data API; send to all four downstream lanes; secrets; KMS | call Meta · call Cashfree · call Google |
| **scheduling** | Data API; Google; gateway; create/update/delete schedules; PassRole to the scheduler role only | verify Cashfree · activate subscriptions · send WhatsApp |
| **reminder** | Data API; send to outbound | send WhatsApp directly · touch payment tables |
| **outbound** | receive from outbound; read secrets; KMS decrypt | **no `rds-data` at all** · no scheduler · no payment |
| **payment** | payment tables via Data API; Cashfree; queue a welcome message | send WhatsApp · read arbitrary tutor data |
| **analytics** | Data API; send to outbound | payment · Google · scheduler |
| **monitoring** | Data API; SNS publish to the alarm topics | any queue send · any provider |
| **ops-api** | Data API; secrets | invoke Lambdas · send to queues · any provider |

Two details worth noting:

* **The outbound worker holds no database grant.** It works from the message it
  was handed, which makes "a compromised sender cannot read conversation
  history" a property of IAM rather than of code.
* **The scheduler role's assume policy carries an `aws:SourceAccount`
  condition.** Without it, any account could assume the role via the scheduler
  service principal — the confused-deputy guard is not optional.

Region scoping for the ops API is enforced **in the application**, not by IAM:
IAM cannot express "only this operator's regions", and filtering in the console
would not be access control at all.

---

## 7. Storage model

Unchanged from Phase 1 and deliberately so: schema `dcc` in the **existing**
Aurora cluster, reached over the Data API. 36 tables. No new cluster —
`aurora_cluster_arn` is a variable validated to be an existing RDS ARN, and
`scan_prohibited.py` fails on `aws_rds_cluster`.

The Data API choice is what makes the whole topology work: it is a public AWS
endpoint, so no function needs to be in the VPC, so no NAT Gateway is needed to
reach Meta, Google, Cashfree and OpenAI.

The `lambda_proxy` fallback remains in settings for a region or engine version
where the Data API is unavailable. It is not wired in Terraform because the
target environment has the Data API; wiring it would be speculative
infrastructure.

---

## 8. Glue, memory and cache

### Glue (`glue/routing.py`, `glue/saga.py`)

* **Capability routing** — eleven capabilities, five lanes, typed
  `CapabilityEvent` with the full correlation chain.
* **Write ownership** — `WRITES` declares which tables each capability may
  write; `assert_write_allowed` refuses anything else. A test asserts no table
  has two writers.
* **Sagas** — `BOOKING` and `PAYMENT`, declarative. Compensations run in
  **reverse** order of completion (cancel the event before releasing the hold,
  or the slot frees while an event still sits on it).
* **`customer_was_told()`** — the question that decides whether a failure can
  be silently retried or needs an apology. An invariant test asserts the
  customer-visible step is always last and has no compensation, because a sent
  message cannot be un-sent.

### Memory (`memory/context.py`)

Structured business state, **not** a growing transcript. The rule enforced:
*no authoritative fact is stored only as prose.* `authoritative_refs` names
where each mirrored value actually lives, and `summarise_into()` structurally
cannot write `offer_percent` or `payment_state` — so a bad summary costs
fluency, never correctness.

`ContextBuilder` fills a token budget in priority order and stops; stage
scoping is applied *before* the budget so a tight budget spends tokens on what
matters here. Core state is never dropped.

### Cache (`cache/layers.py`)

L1 bounded LRU per warm container; optional L2 in Demo-owned Postgres. No
Redis.

`NEVER_CACHE` names six values that would be wrong to cache, each with the
consequence — `assert_cacheable()` refuses them, so adding "slot availability"
requires deleting a line that says not to. Per-kind TTL **ceilings** mean a
caller cannot request a longer TTL than the kind permits; authorization is
capped at 60 seconds.

---

## 9. Resilience

* **Per-provider circuit breakers** — eight providers, eight breakers.
  A test asserts one provider's failure does not open another's circuit.
* **Declared degradation per provider**, each of which is "degrade honestly":
  Google down ⇒ never claim a confirmed meeting; Cashfree down ⇒ invent no
  payment state; Tutor Intelligence down ⇒ offer a human, never a fabricated
  tutor; OpenAI down ⇒ deterministic parsing continues.
* **`lifecycle_blocked_by()`** distinguishes "degraded" from "stopped", because
  paging the same way for OpenAI and Cashfree trains people to mute the channel.
* **Error taxonomy** — thirteen classes, one place. A malformed request, a bad
  credential, an authorization failure and an amount mismatch are **never**
  retryable. `Disposition` is closed: success, retry, DLQ, HITL or terminal.
  There is no "swallowed".
* **Full-jitter backoff** — uniform in `[0, ceiling]`. Equal backoff across a
  fleet re-synchronises every retry into one spike against a provider that is
  already struggling.
* **Kill switches** — `CircuitRegistry.disable()` behaves exactly like an open
  circuit, so every caller already handles it.

---

## 10. Cost controls

Full model in `docs/operations/cost-model.md`, with every rate as a
**parameter**.

Measured per completed demo, asserted as ceilings in the load suite:

```
llm=2  tutor_match=1  calendar_events=1  messages=7
```

Implemented: four independent LLM ceilings plus a daily environment circuit;
purpose-based model routing where only objection extraction earns the expensive
tier; ten `FORBIDDEN_USES` each naming the deterministic alternative; no LLM in
ingress; no LLM on a duplicate event; bounded context; batching on delivery
lanes only; arm64; 4.84 MB package; finite log retention; `PassThrough` tracing
by default; reminder ceilings; provider retry caps; capability kill switches.

Cost alarms **notify**; they never page. A cost spike at 3am is not worth waking
someone for, and treating it as if it were is how genuine pages get muted.

---

## 11. Observability

Structured JSON, one object per line. Trace context is ambient via `ContextVar`
so every line in an invocation carries `trace_id`, `correlation_id`,
`conversation_ref`, `capability` and `handler` without each call passing them.

**Never emitted:** raw PII, provider credentials, HMAC signatures, tracebacks.
`Outcome.detail` is the exception **type name** only, because an exception
message routinely contains the request body that caused it.

Alarms are grouped by what they mean to a person on call, not by which AWS
service emitted them:

| Group | Alarms |
|---|---|
| **Money** (pages) | payment mismatch · activation failure · payment DLQ |
| **Correctness** (pages) | illegal transition spike · slot-hold conflict spike · signature failures · outbound guardrail block |
| **Customer** | scheduling queue age · four DLQs · per-function errors and throttles · reminder failures |
| **Cost** (notifies) | LLM spend · budget exhausted · circuit opened · regional underperformance |

Log retention is finite and validated (1–365 days). Tracing defaults to
sampled.

---

## 12. CI/CD

Three new root workflows, path-filtered to `Demo Intelegence Agent/**`.

**`demo-command-center-ci.yml`** — five jobs: quality (ruff · mypy · pytest with
the coverage gate · contract · e2e · security), security (bandit · ruff S ·
pip-audit · gitleaks), build (package · **size gate** · prohibited scan ·
migration validation · doctor · lifecycle smoke), infrastructure (fmt ·
validate), load-smoke.

**`demo-command-center-deploy.yml`** — **OIDC only.** No `AWS_ACCESS_KEY_ID`,
no `AWS_SECRET_ACCESS_KEY` anywhere. Requires a typed confirmation matching the
environment, re-runs the gate, builds, and uses a GitHub environment so
production is subject to manual approval. The plan is inspected and the deploy
**fails if it would destroy anything** unexpected.

**`demo-command-center-rollback.yml`** — moves the `live` alias, runs no
Terraform and builds nothing, so it is fast enough to use during an incident.
It records the before/after version table and explicitly notes what a rollback
does *not* undo: migrations, in-flight queue messages, infrastructure.

---

## 13. Load results

Simulated providers only. Nothing real was called or billed.

```
baseline (10 conversations, concurrency 2):
  10/10 completed  p50=11.69ms  p95=13.93ms  p99=13.93ms  errors=0  wall=0.19s

per completed demo:
  llm=2  tutor_match=1  calendar_events=1  messages=7

50-way slot contention: resolved in 1.2ms — exactly 1 winner, 49 clean refusals
turn latency after 60 turns: 0.1ms (no growth with history)
```

**What these do not measure:** Lambda cold starts, SQS behaviour, Data API
latency, or real provider latency. Only a deployed test can. What they *do*
measure is the orchestrator's own work per conversation and whether slot
contention degrades or collapses — and a regression in the per-demo counters
shows up in CI before it shows up on a bill.

`peak`, `burst`, `stress` and `soak` profiles are defined and parameterised;
only `baseline` runs in CI, for runtime.

---

## 14. Exact validation results

Every command run from `Demo Intelegence Agent/`.

| Gate | Command | Result |
|---|---|---|
| lint | `ruff format --check` + `ruff check src tests` | **PASS** |
| type | `mypy src` | **PASS** — 130 files, strict |
| test | `pytest --cov` | **PASS** — **389 passed**, coverage **82.80%** |
| contracts | `pytest -m contract` | **PASS** — 17 |
| e2e | `pytest -m e2e` | **PASS** — 33 |
| test-security | `pytest -m security` | **PASS** — **212** |
| load | `pytest -m load` | **PASS** — 4 |
| security | `bandit -r src -ll` | **PASS** — High 0, Medium 0 |
| demo | `dcc-e2e` | **PASS** — 30/30, `NEW → CONVERTED` |
| doctor | `dcc-doctor` | **PASS** — 0 problems, 8 gaps listed |
| migrate | `dcc-migrate --dry-run` | **PASS** |
| **terraform fmt** | `terraform fmt -check -recursive` | **PASS** |
| **terraform validate** | `terraform validate` | **PASS** |
| **package build** | `scripts/build_lambda.py` | **PASS** — **4.84 MB** of 50 MB |
| **prohibited scan** | `scripts/scan_prohibited.py` | **PASS** — 0 findings |
| Tutor suite | `pytest tests` (repo root) | **PASS** — 772, unaffected |

Test count grew from **260 → 389** (+129), security tests from **87 → 212**.

### Terraform caught two real errors

`terraform validate` is installed on this host and found:

1. `iam.tf` referenced `aws_lambda_function.reminder_worker`, which does not
   exist — the workers are a `for_each` map. Fixed to reference both the
   function and its alias.
2. A duplicate `live` alias was declared for the reminder worker, which would
   have conflicted at apply time. Removed.

### Not executed, and why

* `make audit` (pip-audit) — needs `uv export` against a project-local
  environment; this checkout shares the parent venv. **Wired into CI**, where
  it will run.
* `tests/integration/` — needs a live Aurora cluster. Skipped is not a pass.
* `make` itself — not installed on this Windows host. Every target's underlying
  command was run directly.
* GitHub Actions syntax validation — no `actionlint` on this host. The
  workflows are unexecuted.

---

## 15. External credentials and account actions not executed

Nothing was deployed. No AWS resource was created, and no AWS credential was
used at any point.

Required before a first deploy, all owner actions:

| Action | Where |
|---|---|
| Create the OIDC provider and the `DCC_DEPLOY_ROLE_ARN` deploy role | AWS IAM |
| Configure GitHub environments with production approval | GitHub settings |
| Set `DCC_AWS_REGION`, `DCC_AURORA_CLUSTER_ARN`, `DCC_AURORA_SECRET_ARN` | GitHub variables |
| Create the Secrets Manager secret with provider credentials | AWS |
| **Decide the Terraform backend** | `docs/operations/terraform-state.md` |
| Confirm the tutor-confirmation template name | Meta Business Manager |
| Verify the Laravel gateway endpoints | NXTutors website repo |
| Provide Google service-account credentials | Google Workspace admin |

---

## 16. Remaining for the final audit

Ordered by what actually blocks production.

1. **Seven of ten aggregates still use in-memory repositories** under
   `persistence_mode=data_api`. The three implemented carry the concurrency
   contract; the rest would lose demo rows, reminders and payment records on a
   container recycle. **This is the single largest remaining blocker.** The
   schema exists; this is repository code.
2. **Laravel gateway unverified** — eight endpoints, contained in one
   `_ENDPOINTS` table.
3. **Tutor-confirmation template name unknown** — deliberately not guessed; the
   registry refuses it and scheduling degrades.
4. **The Google credential provider is not implemented.**
   `GoogleCalendarClient` takes a `token_provider` and none exists — service-
   account JWT with domain-wide delegation, or an OAuth refresh flow.
5. **Secrets Manager resolution is not wired.** Settings read from the
   environment; deployed environments should resolve `SecretStr` fields at cold
   start.
6. **The ops API has no HTTP surface.** The monitoring capability and its
   server-side region authorization exist; `ops-api` currently points at the
   handoff handler.
7. **`pip-audit` has never actually run.** Wired into CI, unexecuted here.
8. **The authorisation pipeline is not yet on the orchestrator's hot path.** It
   is complete and tested, but `commands.py` still dispatches through its own
   table. Wiring it is mechanical; until then the tool registry is a control
   that exists and is tested rather than one that is enforced on every turn.
9. **No deployed load test.** Cold starts, SQS behaviour and Data API latency
   are unmeasured.
10. **In-process circuit breakers** are per container, so a cold fleet
    re-discovers a dead provider. Accepted trade-off; revisit if provider
    outages become frequent.

### What this phase does not claim

The controls are implemented and tested against fakes. **No line of this has
run against a real AWS account, a real Meta webhook, a real Cashfree payment or
a real Aurora cluster.** The wire format of every provider remains the largest
untested surface, and item 8 above means the tool registry is not yet the
gate it is designed to be.
