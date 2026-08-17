# Release gate — NXTutors Tutor Intelligence Agent

Verdict per component. Three states, and the third is not a soft pass:

| State | Meaning |
| --- | --- |
| **PASS** | Executed here, on this codebase, and the evidence is reproducible by the command shown. |
| **FAIL** | Executed and did not meet the bar. |
| **EXTERNAL-NOT-VERIFIED** | Correct by construction and covered by tests, but the real dependency (AWS, Meta, OpenAI, the website) was not reachable from this machine. **Not evidence of health.** |

Everything below was re-run after the changes in this pass. Nothing is marked
PASS because the code exists.

---

## 1. Architecture constraints

The mandate: Python + Lambda + API Gateway + SQS FIFO/DLQ + EventBridge + S3 +
Secrets Manager + CloudWatch + PostgreSQL/pgvector. Nothing else.

| Constraint | Verdict | Evidence |
| --- | --- | --- |
| No NAT Gateway | **PASS** | `aws_nat_gateway` absent; `nat_gateway_id` and `egress_only_gateway_id` absent from all `*.tf`. Asserted by `tests/security/test_network_boundary.py::TestForbiddenInfrastructure`. |
| No ECS / Fargate | **PASS** | `aws_ecs_cluster`, `aws_ecs_service`, `aws_ecs_task_definition` all absent. Asserted. |
| No EC2 / ASG / Kubernetes | **PASS** | `aws_instance`, `aws_launch_template`, `aws_autoscaling_group`, `aws_eks_cluster` absent. Asserted. |
| No Redis / ElastiCache | **PASS** | `aws_elasticache_*` absent; no `redis` package in `pyproject.toml` or `uv.lock`. The shared store is PostgreSQL (`cache/postgres_store.py`, `kv_entry`, `rate_bucket`, `kill_switch`). |
| No MySQL | **PASS** | `repositories/mysql_tutor.py` **deleted**; `aiomysql`/`pymysql` removed from `pyproject.toml` **and regenerated out of `uv.lock`** (89 packages resolved); `mysql_dsn`, `mysql_pool_size`, `website_direct_mysql_write_enabled` removed from `Settings` and from `.env`. Asserted by `tests/unit/test_tutor_feed.py::TestNoMySQLSurvives`. |
| No MongoDB / DynamoDB / DocumentDB | **PASS** | `aws_dynamodb_table`, `aws_docdb_cluster` absent; no driver in the lockfile. |
| No SQLite in production | **PASS** | `aiosqlite` is a `dev` extra only; `models.py` uses `JSON().with_variant(JSONB(), "postgresql")` so PostgreSQL is the production type. |
| PostgreSQL is the single application database | **PASS** | 22 tables in schema `tutor_match`, migration head `0004`, verified against the live instance. |
| pgvector | **EXTERNAL-NOT-VERIFIED** | The extension is not installed on the shared instance. The RAG path degrades to lexical retrieval as designed (`rag/pipeline.py`), and that degradation is tested — but vector retrieval itself has not run. |

### The defect this pass found and fixed

`match-worker` was **VPC-attached with no NAT Gateway, and built an OpenAI
client, a memory client and a geocoder**. None of those hosts is reachable from
a private subnet without NAT. The failure is silent: the socket never connects,
the call blocks for the full client timeout on **every invocation**, and the
turn degrades to deterministic-only extraction. In logs it looks like latency.

Fixed by splitting the turn at the network boundary:

```
ingress          [internet]  validate, dedup, rate-limit  ─► SQS enrich.fifo
enrich-worker    [internet]  OpenAI + memory, no DB grant ─► SQS match.fifo
match-worker     [VPC]       PostgreSQL/pgvector, no internet route
outbound-worker  [internet]  Meta Cloud API + memory deeds
```

Enforced three independent ways, each tested:

1. `Settings._enforce_network_boundary` refuses `network_zone=vpc` together with
   `geocoder=http`, `chitragupta_enabled` or `whatsapp_enabled`;
2. `bootstrap` builds no LLM provider in the VPC zone and no session factory in
   the internet zone;
3. `tests/security/test_network_boundary.py` asserts Terraform's `vpc_config`
   matches each function's declared `TMM_NETWORK_ZONE`.

---

## 2. Test suites

Command: `pytest -q` (plus per-marker runs).

| Suite | Verdict | Result |
| --- | --- | --- |
| Unit | **PASS** | 244 passed |
| Contract | **PASS** | 126 passed, 1 xfail (documented upstream gap in Lead Intake) |
| Security | **PASS** | 223 passed |
| E2E | **PASS** | 101 passed |
| Load / concurrency | **PASS** | 6 passed — see §4 |
| Lambda handler | **PASS** | Covered in `tests/e2e/test_split_pipeline.py` and the contract suite |
| SQS / idempotency / concurrency | **PASS** | Duplicate storm: 20 concurrent copies of one message → exactly 1 decision, 19 duplicate short-circuits |
| Fault injection | **PASS** | Every optional dependency removed (LLM, cache, memory) → 100/100 turns still answered |
| **Integration (PostgreSQL)** | **EXTERNAL-NOT-VERIFIED** | **18 skipped.** `TMM_INTEGRATION_DSN` is unset; these need a disposable PostgreSQL, and the remote RDS instance is not disposable. Skipped is not passed. |
| **Total** | | **694 passed, 18 skipped, 1 xfailed** |
| Coverage | **PASS** | 84.79% (gate: 80%) |

### Static and supply chain

| Gate | Verdict | Result |
| --- | --- | --- |
| `ruff format --check` | **PASS** | 170 files formatted |
| `ruff check` | **PASS** | All checks passed |
| `mypy --strict src` | **PASS** | No issues in 129 source files |
| `bandit -ll` | **PASS** | No issues identified |
| `pip-audit --strict` over the lockfile | **PASS** | No known vulnerabilities |
| `uv lock --check` | **PASS** | 89 packages, in sync |
| Migration safety gate | **PASS** | 4 migrations |
| `.env.example` ⇄ `Settings` | **PASS** | 91 settings, in sync |
| `terraform fmt -check` / `validate` | **PASS** | Configuration is valid |
| Semgrep / Checkov / gitleaks | **EXTERNAL-NOT-VERIFIED** | CI-only actions; not run locally |

---

## 3. The eight composed agents

All eight registered, weighted, reachable, and producing grounded evidence.
Asserted by `tests/contract/test_composed_agent_coverage.py` (45 tests, no skips).

| # | Agent | Registered | Weighted | Yields evidence | Reports MISSING with no data |
| --- | --- | --- | --- | --- | --- |
| 013 | Tutor Availability | PASS | PASS | PASS | PASS |
| 014 | Tutor Subject Expertise | PASS | PASS | PASS | PASS |
| 015 | Tutor Past Performance Score | PASS | PASS | PASS | PASS |
| 016 | Tutor Personality Compatibility | PASS | PASS | PASS | PASS |
| 017 | Tutor Academic Compatibility | PASS | PASS | PASS | PASS |
| 019 | Tutor Proximity | PASS | PASS | PASS | PASS |
| 021 | Tutor Negotiation Profile | PASS | PASS | PASS | PASS |
| 022 | Tutor Replacement Risk | PASS | PASS | PASS | PASS |

All 40 catalogue skills are declared by their evaluator and checked against the
docstring, so a silently dropped capability fails the build.

**Improved this pass:** agent 013 previously had no source of schedules at all
(the old MySQL adapter hard-coded `availability=None`). The HTTPS feed now
carries availability, `sync/projection.py` persists it into `tutor_availability`
with delete-then-insert so a withdrawn window disappears, and the projection
checksum includes it so reconciliation repairs drift.

---

## 4. Performance and cost

### Latency — in-process, measured

`pytest -m load -s`, 200 sequential full turns:

| Metric | Value |
| --- | --- |
| p50 | **4.1 ms** |
| p95 | **5.1 ms** |
| p99 | **15.0 ms** |
| max | 43.0 ms |

That is orchestration only — extraction, hard filters, eight evaluators,
ranking, evidence guard, output guard — with no network and no database.
Database and queue-wait latency are **EXTERNAL-NOT-VERIFIED**: they need the
locustfile against a deployed environment.

### Query plans — EXPLAIN ANALYZE, 25,000 synthetic rows

A real defect, found and fixed. `PostgresTutorRepository.search` filters on
`LOWER(city)` and `LOWER(gender)`; every shipped index was on the bare column,
and a btree on `city` cannot serve a predicate on `lower(city)`.

| | Plan | Rows removed by filter | Time |
| --- | --- | --- | --- |
| Before | **Seq Scan** | 23,810 | 15.4 ms |
| After (migration `0004`) | Index Scan | 3,205 | **3.6 ms** (4.3×) |

The mechanism is planner statistics as much as lookup: with no statistics on
`lower(city)` PostgreSQL estimated `rows=1` against a true 1,190, and that
misestimate is what made a sequential scan look cheap. A third, sort-aware
composite index was tried, never chosen by the planner, measured slower, and is
**not** included.

| Check | Verdict | Evidence |
| --- | --- | --- |
| N+1 queries | **PASS** | Candidate search is 2 round trips: one projection query, one batched `tutor_id = ANY(...)` for availability. Index Scan, 0.06 ms. |
| Index coverage of hot predicates | **PASS** | Migration `0004` applied to the live instance; head `0004`, both indexes valid, 0 invalid indexes. |
| Connection ceiling under Lambda scale | **EXTERNAL-NOT-VERIFIED** | `reserved_concurrent_executions × postgres_pool_size` is bounded in Terraform, but the RDS Proxy `DatabaseConnections` metric under load has not been observed. |
| Per-match cost | **EXTERNAL-NOT-VERIFIED** | `LlmCostMicros` is emitted and budgeted in four dimensions; no real spend has been measured. |

---

## 5. Security

| Control | Verdict | Evidence |
| --- | --- | --- |
| SQL injection | **PASS** | No free-text query entry point exists; `CandidateQuery` is a frozen typed dataclass. Structurally asserted by AST inspection in `tests/security/test_sql_injection.py`. No module carries an `S608` exemption any more. |
| Prompt injection | **PASS** | Parent text is quoted, labelled and sanitised before any model sees it; detections counted. |
| LLM cannot execute SQL or mutate business state | **PASS** | The model returns a structured requirement only. Hard filters are deterministic and a model cannot override one. |
| No fabricated tutor facts | **PASS** | Evidence guard at claim level, output guard on the finished bytes; an unbacked fee blocks the whole message. 0 violations across the e2e run. |
| PII | **PASS** | Feed allowlist enforced by `extra="ignore"` plus an asserted disjointness with the forbidden field set; a phone number published by the website never reaches process memory. |
| Secrets | **PASS** | No credential in `.env.example` (91 settings, all secrets blank); gitleaks in CI. |
| IAM least privilege | **PASS** (by construction) | One role per function. Ingress can write **only** the enrich queue — it cannot reach the match queue, so a compromised internet-facing function cannot inject a forged `EnrichmentV1` into matching. The enrich role has **no** `rds-db:connect`. |
| Encryption | **PASS** (by construction) | KMS CMK with rotation on every queue, the S3 bucket and every Lambda environment. |
| Rate limiting | **PASS** | Layered PostgreSQL token bucket; refill-check-decrement in one atomic statement. |
| Abuse handling | **PASS** | Escalating enforcement; a shed turn releases its idempotency claim. |
| Circuit breakers / provider 429-5xx | **PASS** | Retry classification and breaker tested; a 4xx is never retried. |
| Kill switches | **PASS** | `MATCHING_PAUSED` is lossless — it takes no durable action, so the redelivery is not swallowed as a duplicate. |
| Idempotency | **PASS** | `ON CONFLICT DO NOTHING` + rowcount; 20-way concurrent duplicate storm yields exactly one decision. |
| Optimistic locking | **PASS** | Conditional UPDATE; conflict releases the claim so redelivery can retry. |
| HITL | **PARTIAL** | Schema and audit trail exist (`approval_audit`); there is **no operator UI or workflow**. Approvals cannot actually be granted today. |

---

## 6. Outstanding — not resolved

Stated plainly rather than buried.

| # | Item | Impact |
| --- | --- | --- |
| B1 | **Integration suite never executed.** Needs a disposable PostgreSQL (`TMM_INTEGRATION_DSN`). 18 tests skipped. | The real persistence path — transactions, optimistic locking, the outbox lease — is covered only by unit doubles. |
| B2 | **No evaluation dataset.** No labelled set of requirement → expected shortlist, so scoring quality drift is undetectable. | A policy change can silently make matching worse. |
| B3 | **`tutor_projection` is empty.** `TMM_WEBSITE_API_BASE_URL` is unset, so the feed is unconfigured and the sync reports `feed_not_configured`. | **The agent has no real tutors.** It answers honestly with no-matches. This is the single blocker to a real end-to-end run. |
| B4 | **HITL is schema-only.** | Escalations are recorded, never actioned. |
| B5 | **pgvector unavailable.** | RAG runs lexical-only. |
| B6 | **Rollback never rehearsed**; no CloudWatch dashboard. | Recovery time is unknown. |
| B7 | **Credentials in `.env` are live and unrotated.** | Should be rotated before any deploy. |

---

## 7. Verdict

**Conditional go — for deployment, not for live parent traffic.**

The architecture now satisfies the mandate: serverless, NAT-less, PostgreSQL-only,
with the internet/VPC boundary enforced in config, in code and in Terraform, and
asserted by tests. The matching pipeline is correct, guarded, fast, and all eight
composed agents demonstrably contribute.

It is **not** ready for real parents, for one reason above the rest: **B3 — there
are no tutors in the projection.** Point `TMM_WEBSITE_API_BASE_URL` and
`TMM_WEBSITE_API_SIGNING_KEY` at the website's `/internal/agent/tutors` feed, run
`sync_projection`, and the same flows run on real data. Until then every honest
answer is "no match".

Recommended order: B3 (feed) → B1 (integration suite against a throwaway
PostgreSQL) → B7 (rotate credentials) → B2 (evaluation set) → B4 (HITL).
