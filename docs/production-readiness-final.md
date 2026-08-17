# Production readiness — final assessment

**Service:** `tutor-match-meta` (NXTutors TutorMatch Meta Agent)
**Assessed:** after an adversarial hardening pass
**Schema:** 0003 · **Event contract:** v1 · **Suite:** 534 passed, 18 skipped, 84% coverage

---

## 1. Executive summary

The service is **architecturally sound and now genuinely wired**. It was not
before.

The hardening pass found **21 defects**, and the character of them is the point
of this report. They were not rough edges. Four of them meant the service could
not have worked at all:

- **there was no API Gateway.** The ingress Lambda had no trigger and the
  handoff endpoint had no Lambda. Nothing could have reached it;
- **the shared rate limiter never refused anything** — the SQL asked
  `(tokens >= 0)` after an UPDATE that clamps the value non-negative, which is
  always true;
- **every relayed reply was dropped** — the producer wrote one payload shape
  and the consumer read a different one, so the `KeyError` was logged as
  "unparseable" and the message vanished without reaching a DLQ;
- **all seven kill switches were inert.** Fully implemented, and called from
  nowhere.

Alongside those, three concurrency defects would each have caused a
production incident within hours: a per-request database engine that would
exhaust a **shared** RDS instance, a non-reentrant lock deadlocking every cold
start of the Lead Intake API, and an outbox claim that let two relays deliver
the same message.

All are fixed, each with a test that fails without the fix. The suite grew from
307 to 534 tests.

What remains is **evidence, not implementation**. Two things cannot be
demonstrated from a laptop: how the service behaves under load, and whether a
scoring change improves or degrades match quality. Both need a deployed
environment or a dataset that does not yet exist.

**Recommendation: NO-GO for full production. GO for staged shadow-mode
deployment to staging** — which is also the only path that clears the
remaining blockers.

---

## 2. Architecture implemented

```
Lead Intake Agent ──HMAC/secret──► API Gateway (2 HTTP APIs)
                                        │
                    ┌───────────────────┴──────────────────┐
                    ▼                                      ▼
            ingress Lambda                        internal-api Lambda
            validate · limit · pause              handoff · health · version
            enqueue only                          full matching, inline
                    │                                      │
                    └──────────► SQS FIFO ◄────────────────┘
                          MessageGroupId = conversation_id
                                   │
                          match-worker Lambda
                            A  extract      deterministic → guarded LLM
                            B  ask          smallest missing set
                            C  hard filter  LLM cannot override
                            D  retrieve     structured SQL, cached 120s
                            E  score        8 bounded evaluators
                            F  rank         versioned ScoringPolicy
                            G  explain      guard-approved evidence only
                            H  link         canonical, or omitted
                            I  output guard the finished bytes
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
        PostgreSQL           Chitragupta          SQS outbound
        state · decisions    deeds (optional)     outbound-worker → Meta
        outbox · analytics
        rate buckets
        kill switches
              ▲
              │  15-min sync
        MySQL (website)                    scheduled Lambda → S3 → Glue (daily)
```

**Fully serverless, as required.** Five Lambdas, no Fargate, no ECS, no NAT
Gateway, **no Redis**. The shared state that genuinely needs to cross
containers — rate buckets and kill switches — lives in PostgreSQL, which is
already on the path and scales to zero. VPC-attached functions reach AWS
services through the interface endpoints added this pass; the two functions
needing internet egress run outside the VPC.

### Subsystems built during this pass

| Subsystem | Why it did not exist |
| --- | --- |
| API Gateway + internal-api Lambda | The service had no inbound route at all |
| VPC endpoints | Documented in a comment, never provisioned |
| `analytics/` | An empty package; the table existed with no producer |
| `integrations/geo/` | An empty package |
| `rag/embeddings.py` | No content-hash skip; every run re-embedded everything |
| `prompts/registry.py` | Prompts were inline constants, unversioned |
| `integrations/llm/routing.py` | No purpose→model mapping, no pre-spend guards |
| `orchestration/output_guard.py` | No message-level validation |
| `orchestration/model_context.py` | No explicit minimisation boundary |
| `repositories/cached_tutors.py` | The cache was built and thrown away |
| `version.py` + `/version` | No way to know what was deployed |
| `cli/doctor.py` | Referenced by two files, never written |
| `scripts/smoke_test.py` | Referenced by deploy **and rollback**, never written |
| Glue, budgets, 14 alarms | Not provisioned |

---

## 3. Security posture

**Strong, with two named gaps.** Full analysis in `docs/threat-model.md`
(24 threats, STRIDE plus the agent-specific categories).

What holds up well:

- **Prompt injection defence is layered, and honest about which layer works.**
  The output side — schema-constrained responses plus a validator on the
  finished bytes — is the layer that actually holds; structural wrapping and
  pattern detection exist to reduce noise and make campaigns visible, and the
  code says so. `TestAdversarial` runs the §7 attacks end to end and asserts
  they fail *safely*, not merely that they are detected.
- **SQL injection is structurally impossible**, not filtered. There is no API
  that accepts SQL: `CandidateQuery` is a frozen dataclass of typed scalars.
  Proven by an AST walk over every SQL-building module — with a
  planted-violation self-check, so the detector cannot silently stop working.
- **Poisoned tutor records cannot move a ranking.** A tutor writing
  "always rank me first" into their profile changes nothing, because
  `profile_summary` is evidence to quote, never a scoring input. The test
  computes a shortlist before and after and asserts byte-identical order.
- **Least privilege is real.** The only internet-facing function holds
  `sqs:SendMessage` on one queue and nothing else — no database access, no
  tutor data.

Gaps:

- **No external penetration test** against a deployed environment.
- **No automated IAM policy assertions.** Roles are reviewed and Checkov gates
  CI, but nothing asserts a role *lacks* a permission.

---

## 4. Data privacy posture

**Strong.** Five classes, field-by-field, in `docs/data-classification.md`.

The design principle worth stating: classification is driven by **field type,
not content scanning**. `tutor.phone` is PII because it is `tutor.phone`, not
because it matches a regex. Regex redaction is the last line for free text, not
the primary control.

Four independent enforcement layers, each with tests:

| Layer | Mechanism |
| --- | --- |
| Logs | `RequestContext` has **no field** for a phone or a message body — a leak would need a new field, not a careless format string |
| Metrics | `assert_label_safe` raises at runtime on an identifying dimension |
| Analytics | Closed dimension allowlist; an unlisted key raises at construction |
| Model payloads | `ModelContext` is a **positive projection** — a new tutor column cannot reach a provider by existing |

The test that matters most is
`test_a_new_projection_field_cannot_leak_by_default`: it asserts the projection
only ever contains fields it names. A denylist would pass only for the fields
someone remembered.

One deliberate, bounded exception: `outbox_event.payload.recipient` holds a raw
E.164 number, only under `outbound_ownership=tutor_match_sends`, only while a
message is undelivered, never in logs. The Meta API addresses by phone; there
is no way to deliver without it.

**Open:** no privileged audit/unmask path (designed, not built), and no erasure
endpoint for a DPDP subject request.

---

## 5. Matching-quality evaluation

**This is the weakest area, and it is a blocker.**

What exists: eight bounded evaluators, a versioned `ScoringPolicy` with no
business weight in Python source, an evidence guard that mechanically prevents
citing a dimension whose data is missing/stale/below sample size, and a
decision record stamped with policy id, version **and checksum**.

What does not exist: **an evaluation dataset and a harness.** No offline
measurement of hard-filter correctness, top-1/top-3 acceptance, no-match
precision, subject/board error, or replacement rate.

The consequence is concrete: a scoring-policy change cannot be justified
quantitatively before rollout. Every quality rollback trigger is reactive — an
alarm on production traffic. §26 and §27 both require better.

Mitigations already in place: shadow mode records real decisions with no
parent-visible effect; the checksum detects an unversioned edit to a live
policy; historical scores are never rewritten.

---

## 6. Failure-mode evaluation

**Strong.** `docs/fallback-matrix.md` covers every dependency; 17 fault-injection
tests execute them.

The property running through all of it: **a third-party outage never becomes a
total platform failure.** OpenAI down → deterministic matching, which already
handles the well-formed majority with zero model calls. RAG down → structured
matching, and exact tutor filtering is untouched. Chitragupta down → bounded
session state, deeds spooled. Cache down → read the canonical store.

One dependency has no graceful degradation, deliberately: **PostgreSQL**. We do
not produce a recommendation whose decision cannot be recorded. An unauditable
shortlist is worse than a delayed one.

The subtlest thing verified here is an *ordering* property:
`MATCHING_PAUSED` is read **before** the idempotency claim. Claiming first and
then declining would burn the dedup key, so the caller's redelivery after
unpause would be swallowed as a duplicate and the parent would never be
answered. That is what makes the switch genuinely lossless, and there is a test
for it.

---

## 7. Cost profile

Modelled in `docs/cost-architecture.md` from formulas at three declared traffic
levels. **No production usage is invented.**

| | LOW (500/mo) | EXPECTED (5k/mo) | HIGH (50k/mo) |
| --- | --- | --- | --- |
| Total / month | ~$82 | ~$85 | ~$119 |
| Per completed match | $0.164 | $0.017 | $0.0024 |

The shape is unusual and worth stating plainly: **at LOW and EXPECTED traffic,
~89% of the cost is fixed overhead that would be identical if the service
handled zero messages** — CloudWatch custom metrics ($54) and RDS Proxy ($22).
The variable cost of actually matching a family is under two cents.

That is the intended consequence of a scale-to-zero design. It also means the
honest way to reduce cost is to remove always-on components, not to optimise
the request path.

Controls: four independent LLM ceilings checked **before** the spend, a
content-hash embedding skip (an unchanged corpus costs nothing), a capped and
cached geocoder, and a fast hourly spend alarm — because an AWS Budgets alert
arriving the next morning is a post-mortem, not a control.

---

## 8. Performance profile

**Instrumented, unmeasured.** This is blocker B1.

Eleven `STAGE_*` metrics separate queue wait from processing time — the
distinction that stops a backlog looking healthy. Five Locust profiles are
written. Statement timeouts, connection timeouts and bounded concurrency are
configured. Migration 0003 adds evidence-shaped indexes built `CONCURRENTLY`.

But no load run has happened, so:

- the latency SLOs are assumptions;
- `match_worker_reserved_concurrency = 20` is a reasoned guess, not a measured
  limit — and the risk it bounds is exhausting an RDS instance **another
  service shares**.

---

## 9. Observability

Structured JSON logs with a correlation id on every line (which also
neutralises log injection, since newlines are escaped). 60+ metrics in one
enum, so dashboards cannot drift from the code. EMF, so the log line *is* the
metric — no `PutMetricData` latency, no extra IAM. 14 CloudWatch alarms, each
naming its runbook in `alarm_description`.

**Missing:** a dashboard. The alarms exist; there is no single pane to open.
Non-blocking, but the first thing to add.

---

## 10. Human-in-the-loop

**Half-built, and this is blocker B4.**

Escalation *into* human review works: thin evidence, guard violations, explicit
requests and unresolvable links all route to a human, with tests.

Approval *out of* it does not exist. `approval_request` and `approval_audit`
are tables with no service writing to them and no operator surface. Fee
overrides and discounts are therefore currently impossible — which is safe, and
incomplete against §39.

---

## 11. Rollback capability

**Strong, and this is one of the better-designed parts.**

Five independent axes, so a bad prompt does not need an application rollback:

| Axis | Mechanism | Time |
| --- | --- | --- |
| Prompt | `TMM_PROMPT_PINS=explanation=v1` | ~30s, no deploy |
| Scoring policy | `TMM_DEFAULT_POLICY` | ~30s, no deploy |
| Model | `TMM_MODEL_*` | ~30s, no deploy |
| Feature | `TMM_FLAG_*` | ~30s, no deploy |
| Application | `rollback.yml` | ~2 min |

Plus seven kill switches taking effect within 10 seconds without a deploy.

A pin naming a version this build does not contain **fails `/ready`** rather
than silently serving something else.

**Untested:** no rollback has been rehearsed against a real deployment.

---

## 12. Live integrations verified

| Integration | Status |
| --- | --- |
| Matching pipeline end to end | **Verified** — 534 tests, `make e2e`, `make doctor` |
| PostgreSQL reachability | **Verified** — the doctor connects to the configured instance |
| Deterministic extraction | **Verified** |
| Eight evaluators, ranking, evidence guard | **Verified** |
| Output guard | **Verified** — 25 tests including adversarial |
| Internal API contract | **Verified** — real FastAPI over the in-memory stack |
| Terraform validity | **Verified** |

---

## 13. External integrations not yet live-tested

| Integration | Status | Needs |
| --- | --- | --- |
| OpenAI | Implemented, never called | An API key and a deliberate run |
| Meta Cloud API | Implemented, never called | A Business Account; only used under `tutor_match_sends` |
| Chitragupta | Implemented, disabled | A base URL and key |
| Website MySQL projection sync | Implemented, never run | MySQL credentials |
| Website write-back | Implemented, disabled | Deliberate enablement |
| Geocoder (HTTP) | Implemented, disabled | A provider; `offline` is the default and needs nothing |
| SQS / API Gateway / Glue | Terraform written, never applied | An AWS deploy |
| **PostgreSQL schema** | **Migrations never run** | `alembic upgrade head` — the `tutor_match` schema does not exist |

The last row is a deployment prerequisite, not a defect. It is step 2 of §15.

---

## 14. Remaining risks

### Blockers

| # | Risk | Why it blocks |
| --- | --- | --- |
| B1 | No load test | SLOs and the concurrency ceiling are assumptions, and the ceiling bounds blast radius on a **shared** database |
| B2 | No evaluation dataset or harness | Match quality cannot be measured before or after a change |
| B3 | Integration tests unexecuted | The two most consequential fixes (rate limiter, outbox lease) are concurrency properties of SQL, fixed but not demonstrated |
| B4 | HITL approval is schema only | §39 high-risk actions have no approval path |

### Accepted risks

| Risk | Why acceptable |
| --- | --- |
| Chitragupta WAL is per-container `/tmp` | Best-effort by construction; memory is never a matching input |
| Pool cache can serve a 120s-old pool | Same order as the sync interval; freshness is recomputed on read |
| L1 cache cannot be invalidated cross-container | Capped at 30s |
| Prompt caching may not hit | Never correctness-critical; tracked, not depended on |
| Constraint-relaxation rungs 1–4 absent | Falls back to an honest no-match naming the blocking rule |
| No 24-hour-window modelling | Lead Intake owns it under the deployed default; blocks `tutor_match_sends` |
| `.env` holds live credentials on disk | Gitignored; rotate before wider access |

---

## 15. Exact deployment procedure

```bash
# ── 0. Gate ────────────────────────────────────────────────────────────────
make check                       # format, lint, types, tests, contracts
make audit                       # dependency CVEs against the lockfile
make security                    # bandit + ruff S
uv run tutor-match-doctor
python scripts/check_migration_safety.py

export TMM_INTEGRATION_DSN=postgresql+asyncpg://tmm:tmm@localhost:5433/tmm
uv run pytest -m integration     # clears blocker B3

# ── 1. Secrets ─────────────────────────────────────────────────────────────
aws secretsmanager create-secret --name /tutor-match-meta/staging \
  --secret-string file://secrets.json   # then shred the file
# Required: TMM_HASH_PEPPER, TMM_INGRESS_SIGNING_KEY, TMM_INTERNAL_SECRET,
#           TMM_CONTINUATION_SIGNING_KEY, TMM_POSTGRES_DSN
#           (+ TMM_OPENAI_API_KEY only if llm_provider=openai)

# ── 2. Schema ──────────────────────────────────────────────────────────────
# Additive only; creates the `tutor_match` schema. Migration 0003 builds
# indexes CONCURRENTLY, so it does not block the sync job.
uv run alembic upgrade head
psql "$TMM_POSTGRES_DSN" -c "SELECT version_num FROM tutor_match.alembic_version;"
# Expect: 0003

# ── 3. Artifact ────────────────────────────────────────────────────────────
uv build --wheel
aws s3 cp dist/*.whl "s3://$ARTIFACT_BUCKET/tutor-match-meta/$(git rev-parse HEAD).zip"

# ── 4. Infrastructure ──────────────────────────────────────────────────────
terraform -chdir=infra/terraform init -backend-config="key=staging/terraform.tfstate"
terraform -chdir=infra/terraform plan -out=tfplan     # READ THIS PLAN
terraform -chdir=infra/terraform apply tfplan

# ── 5. Stamp the build ─────────────────────────────────────────────────────
for fn in ingress internal-api match-worker outbound-worker scheduled; do
  aws lambda update-function-configuration \
    --function-name "tutor-match-meta-staging-$fn" \
    --environment "Variables={...,TMM_GIT_SHA=$(git rev-parse HEAD)}"
done

# ── 6. Verify ──────────────────────────────────────────────────────────────
export TMM_SMOKE_INTERNAL_URL=$(terraform -chdir=infra/terraform output -raw internal_api_url)
export TMM_SMOKE_INGRESS_URL=$(terraform -chdir=infra/terraform output -raw ingress_url)
python scripts/smoke_test.py --environment staging --expect-sha "$(git rev-parse HEAD)"

# ── 7. Seed the projection ─────────────────────────────────────────────────
aws lambda invoke --function-name tutor-match-meta-staging-scheduled \
  --payload '{"job":"sync_projection"}' /dev/stdout
aws lambda invoke --function-name tutor-match-meta-staging-scheduled \
  --payload '{"job":"check_staleness"}' /dev/stdout

# ── 8. Shadow mode — real traffic, no parent-visible effect ─────────────────
# TMM_FLAG_ENABLED=true  TMM_FLAG_SHADOW_MODE=true  TMM_FLAG_PERCENTAGE_ROLLOUT=100
# Leave for at least a week. This is also the seed corpus for blocker B2.

# ── 9. Load test — clears blocker B1 ───────────────────────────────────────
uv pip install locust
uv run locust -f tests/load/locustfile.py --host "$TMM_SMOKE_INGRESS_URL" \
  --users 50 --spawn-rate 5 --run-time 10m --headless
# Set match_worker_reserved_concurrency from DatabaseConnections and Throttles.

# ── 10. Staged rollout ─────────────────────────────────────────────────────
# TMM_FLAG_SHADOW_MODE=false, then TMM_FLAG_PERCENTAGE_ROLLOUT=5 → 25 → 100.
# Watch between each: NoMatch, HumanHandoff, OutputGuardRejected, LlmCostMicros.
```

**Lead Intake needs one change:** `TUTOR_MATCHING_AGENT_INTERNAL_SECRET` in
`app/core/config.py` (the value is already in their `.env`). Tracked by the
xfailing contract test.

---

## 16. Exact rollback procedure

**Choose the narrowest axis that fixes the problem.** Three of the five are
faster than an application rollback and do not touch code.

```bash
# ── Which axis? ────────────────────────────────────────────────────────────
curl -s -H "X-NXTUTORS-INTERNAL-SECRET: $TMM_INTERNAL_SECRET" \
  "$INTERNAL_URL/internal/v1/version" | jq
# Compare app_version, git_sha, default_policy and prompt_versions against
# the last known-good deploy. Roll back only what changed.

# ── A. Prompt (~30s) ───────────────────────────────────────────────────────
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --environment "Variables={...,TMM_PROMPT_PINS=explanation=v1}"
curl -s "$INTERNAL_URL/internal/v1/ready" | jq   # 503 => the pin does not exist

# ── B. Scoring policy (~30s) ───────────────────────────────────────────────
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --environment "Variables={...,TMM_DEFAULT_POLICY=regular_school_support.v1}"
# Historical scores are never rewritten; only future matching changes.

# ── C. Model (~30s) ────────────────────────────────────────────────────────
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --environment "Variables={...,TMM_MODEL_ESCALATION=gpt-4o-mini}"

# ── D. Feature flag / emergency stop (~10s) ────────────────────────────────
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name, paused, actor, reason)
VALUES ('LLM_PAUSED', true, '$USER', 'INC-1234')
ON CONFLICT (name) DO UPDATE SET paused=true, actor=EXCLUDED.actor,
  reason=EXCLUDED.reason, changed_at=now();"

# ── E. Application (~2 min) ────────────────────────────────────────────────
gh workflow run rollback.yml -f environment=production -f target_sha=<good-sha>
# Code only. Migrations are additive by policy, so the previous code runs
# against the current schema. Rolling the schema back is a separate, manual,
# human-approved decision.

# ── Verify, every time ─────────────────────────────────────────────────────
python scripts/smoke_test.py --environment production --expect-sha "<good-sha>"

# ── Drain what accumulated ─────────────────────────────────────────────────
aws lambda invoke --function-name "$SVC-scheduled" \
  --payload '{"job":"relay_outbox"}' /dev/stdout    # reclaims abandoned leases first
aws sqs get-queue-attributes --queue-url "$MATCH_DLQ" \
  --attribute-names ApproximateNumberOfMessages     # then docs/runbooks/dlq-replay.md

# ── Before going off shift ─────────────────────────────────────────────────
curl -s -H "X-NXTUTORS-INTERNAL-SECRET: $TMM_INTERNAL_SECRET" \
  "$INTERNAL_URL/internal/v1/version" | jq '.holding_work'
# Must be [] or handed over in writing.
```

Rollback triggers and their alarms: `docs/production-control-matrix.md` §24.

---

## 17. Go / No-Go

### **NO-GO for full production rollout.**
### **GO for staged shadow-mode deployment to staging.**

The blockers are **B1** (no load test) and **B2** (no evaluation harness), with
**B3** (integration tests unexecuted) and **B4** (HITL approval) close behind.

The reasoning is deliberate, and it is not about code quality. The service is
now well built: 534 tests, 84% coverage, every gate that *can* run locally
passing, 21 real defects found and fixed with regression tests for each. If the
question were "is the code sound", the answer would be yes.

But two claims cannot be made from a laptop, and both are load-bearing:

**"It will hold under load."** Unknown. The concurrency ceiling is a reasoned
guess, and what it bounds is exhausting an RDS instance that
`demo_command_center` also depends on. Getting that wrong is an outage for a
service with nothing to do with tutoring.

**"It matches well."** Unmeasured. There is no dataset, so there is no
before/after. Every quality control is reactive — an alarm on real families'
conversations.

Shipping to production without those would mean discovering both on live
traffic, and the brief is explicit that "not executed" must never be recorded
as "passed".

Staging clears them. Shadow mode gives real traffic, real data and zero
parent-visible effect, and its recorded decisions are precisely the seed corpus
B2 needs. **The path to GO runs through staging, not around it.**

**B3 is the exception and should be done today**: fifteen minutes with a
Docker PostgreSQL converts the two highest-severity fixes from "reasoned" to
"demonstrated". It needs no deployment and no approval.

### Conditions for GO

1. **B3** — integration suite green against a real PostgreSQL.
2. **B1** — load test run in staging; concurrency set from
   `DatabaseConnections` and `Throttles`, not from what AWS permits.
3. **B2** — evaluation dataset built from shadow-mode decisions, with the §26
   metrics computed for the incumbent policy as a baseline.
4. **B4** — approval workflow built, or high-risk actions explicitly descoped
   in writing.
5. A rollback rehearsed once in staging.
6. A CloudWatch dashboard.
7. Credentials rotated and `.env` cleared from developer machines.

With 1–4 done, this is a **GO**. Without them, deploying to production would be
calling an unfinished system production ready, which is the one thing this
review was asked not to do.
