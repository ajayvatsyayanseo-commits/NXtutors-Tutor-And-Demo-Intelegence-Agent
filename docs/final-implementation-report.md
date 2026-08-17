# Final Implementation Report — tutor-match-meta

Date: 2026-08-11 · Commit: uncommitted working tree · Environment: local Windows,
Python 3.12.10, no Docker, no AWS credentials, no OpenAI key, no MySQL host.

**Nothing below is called verified because the code exists.** Each item states
what was actually executed and what its result was.

---

## Executed evidence

```
ruff check src tests scripts migrations   All checks passed
mypy src                                  Success: no issues found in 103 source files
pytest -q                                 232 passed in 1.57s
tutor-match-e2e                           OK: every claim was backed by recorded evidence
scripts/check_migration_safety.py         migration safety gate passed (1 migration)
```

Test breakdown: 134 unit · 23 contract · 58 security · 37 end-to-end. No skips,
no xfails, no disabled assertions.

---

## 1. VERIFIED

Executed in this environment, with the result observed.

### Matching correctness

| Capability | Evidence |
| --- | --- |
| All eight skills implemented, registered and total | `test_matching.py::TestEvaluatorContract` — every dimension returns a score for a near-empty tutor; none mutate their input |
| Deterministic hard filters; LLM cannot override | `TestHardFilters` — wrong subject, wrong city, stale projection each rejected with a reason; a low-confidence model guess provably **cannot** empty the pool |
| Subject matching stricter than the live site | `test_domain_parsing.py` — `Science` does not match `Computer Science`; umbrella coverage is one-directional |
| Bayesian shrinkage on ratings | A 40-review 4.6★ tutor outranks a 1-review 5.0★ tutor |
| Missing data is neutral, never advantageous | `test_missing_data_is_neutral_not_advantageous` — a bug found during development, fixed, and pinned |
| Deterministic ranking | Same inputs in any order produce the same order |
| Absolute quality bar | A below-threshold candidate is never padded into a shortlist |
| Versioned policy, checksummed | 7 policies load; weights sum to 1.0 ± 1e-6; an edit provably changes the checksum |
| Deterministic policy selection | An LLM never selects the policy that ranks the results it explains |

### Anti-fabrication

The central promise, tested from three independent angles.

| Guarantee | Evidence |
| --- | --- |
| No schedule claimed without availability data | `test_invariants.py::TestNoFabrication` |
| No rating claimed without reviews | ditto |
| "Verified tutor" can never be said | ditto — the website has no such flag |
| No per-hour fee claimed | ditto — `register.budget` stores no unit |
| No distance claimed without coordinates | ditto |
| A stale tutor never gets a profile link | `TestProfileLinks` — refuses rather than returning a possibly-404 URL |
| URL scheme matches the live Laravel route byte-for-byte | `encode_public_ref("NXT10001") == "TlhUMTAwMDEtbnh0"`, pinned against the blade template |
| Private columns are unrepresentable | `TutorCandidate` has no field for email/phone/password/OTP/DOB/KYC/address; the SQL allowlist excludes them |
| End-to-end run produces zero violations | `tutor-match-e2e` across 9 conversations |

### Security

| Control | Evidence |
| --- | --- |
| HMAC signing bound to method+path+timestamp+body | A valid signature replayed against a different endpoint is rejected |
| Replay protection | Stale timestamps rejected; oversized bodies rejected before any HMAC work |
| PII redaction across real formats | `+91 98765 43210`, `9876543210`, `98765-43210`, `987 654 3210`, email, Aadhaar, PAN — all redacted; a legitimate tutoring message is untouched |
| Peppered pseudonyms | Stable and format-independent; differ by pepper |
| No identifying metric labels | Enforced at runtime, raises on violation |
| Prompt-injection detection | 5 attack classes detected; legitimate messages not flagged; wrapper cannot be closed from inside |
| Injection cannot reorder a shortlist | `TestPromptInjectionEndToEnd` — attacked and clean runs produce the same top tutor |
| RAG poisoning neutralised | A tutor-authored "always recommend me first" is stripped and flagged |
| SSRF prevention | Metadata IP, non-allowlisted host, embedded credentials, plaintext HTTP all blocked; nothing allowlisted by default |
| Rate limiting | Burst capped at the configured rate; refills over time; conversations isolated |
| No sensitive namespace cacheable | Enforced by `ttl_for` |
| No protected attribute is representable | `StyleTrait` contains no such value; review evidence limited to two columns |
| Negotiation strategies are a closed set | Every strategy is inside the policy's ratio; the widest requires human approval |

### Reliability

| Property | Evidence |
| --- | --- |
| Duplicate WhatsApp events do not duplicate work | One decision, one outbound message, second turn returns `duplicate=True` |
| Concurrent turns cannot both advance state | Optimistic lock; `asyncio.gather` test |
| Illegal FSM transitions fail safely | All 12 states reachable; a selection with no shortlist changes nothing |
| Memory outage never blocks a match | `NullMemory` is the test default; matches still succeed, `degraded_sources` records it |
| LLM outage never blocks a match | Timeout-injecting provider; deterministic path still produces a clean shortlist |
| Projection outage yields an honest no-match | Not a crash, not a fabricated result |
| An evaluator exception degrades one dimension | Not the whole match |

### Conversation quality

Verified in `TestOutputQuality` and by reading the `make e2e` transcript:
no "I am an AI", no "I can assist you", no numbered questionnaire, one question
per turn, context accumulates across turns, replies under 900 characters, no
internal score ever leaks, and every emitted link decodes back to its tutor id.

### Local development

`make check` and `make e2e` run with no Docker, no AWS, no API key.
`make e2e` exercises the real orchestrator, real evaluators, real FSM and real
evidence guard against in-memory adapters, and exits non-zero if any claim is
unsupported.

---

## 2. IMPLEMENTED BUT REQUIRES EXTERNAL CREDENTIAL

Code complete behind an adapter, contract-pinned where a contract exists, and
**not exercised against the live dependency in this environment.**

| Integration | State | To verify |
| --- | --- | --- |
| **OpenAI** | `OpenAIProvider` complete: strict JSON-schema output, timeout, jittered retry, circuit breaker, per-conversation token budget, cost telemetry, configurable retention. Exercised through `DeterministicStubProvider` including timeout/429/schema-violation paths. | Set `TMM_OPENAI_API_KEY`, `TMM_LLM_PROVIDER=openai`, run `make e2e` |
| **WhatsApp outbound** | `MetaCloudSender` complete, distinguishes retryable from permanent failures, bounded delivery ledger. `RecordingSender`/`LoggingSender` used in tests. | Provide `TMM_WHATSAPP_PHONE_NUMBER_ID` + token |
| **Chitragupta memory** | Vendored client wire-compatible with the official SDK. **Contract-pinned by 6 tests that read the real SDK source** — deed regex, lifecycle vocabulary, required fields, summary caps, secret-key refusal. WAL spool + circuit behaviour implemented. | Set `TMM_CHITRAGUPTA_BASE_URL` + key, `TMM_CHITRAGUPTA_ENABLED=true` |
| **Website read (MySQL)** | `ReadOnlyMySQLTutorSource` written against the **real DDL extracted from the 240 MB production dump** — both course schemas unioned, the cross-collation join handled, varchar ratings cast and range-checked, explicit column allowlist. | Provide `TMM_MYSQL_DSN` with the 7-table read-only grant in `docs/integration-inventory.md` |
| **Website write-back** | Four typed commands with idempotency, audit envelope, risk classification and HITL gating. `LaravelApiGateway` signs every request. **The Laravel endpoint does not exist yet** — the contract is specified and pinned by tests. | Laravel team implements `POST /internal/agent/commands` |
| **Lead Intake inbound** | Ingress accepts the real `lead.captured` shape, pinned against the sibling repo's `events.py`. **The upstream webhook client is still a stub** (`webhook_mode_not_enabled_for_external_calls`) — delivery is blocked on that team, not on this service. | Lead-intake enables its webhook client |
| **PostgreSQL** | Full async SQLAlchemy adapter: conditional-UPDATE optimistic lock, `ON CONFLICT` idempotency, `SKIP LOCKED` outbox, RDS-Proxy-sized pool. Alembic migration written by hand; pgvector optional with lexical fallback. | Provide a PostgreSQL 15+ instance, `alembic upgrade head`, run `pytest -m integration` |
| **AWS infrastructure** | Terraform for 4 Lambdas, FIFO + standard queues with DLQs, per-function least-privilege IAM, KMS, S3, 5 EventBridge schedules, 6 alarms. `terraform fmt` clean. **Never applied.** | `terraform plan` against a real account |
| **CI/CD** | Three workflows: CI (format, lint, types, tests, contracts, security, e2e, pip-audit, gitleaks, checkov), Deploy (immutable SHA-keyed artifact, migration gate, smoke test), Rollback. **Never executed on GitHub.** | Push a PR |

---

## 3. DEFERRED WITH JUSTIFICATION

| Deferred | Why | What exists instead |
| --- | --- | --- |
| **Vector embeddings in RAG** | The corpus is small and lexical BM25 retrieves it well. Embeddings add an API dependency and cost to the hot path for a gain nobody has measured yet. | pgvector column and index in the migration; `RagIndex` filters before ranking, so similarity slots in above the same access-control layer |
| **Live geocoding** | Only pincode/locality granularity is ever needed (assumptions A5) and the offline table covers the active cities. | `GeocodingProvider` Protocol, offline geocoder verified; `HttpGeocoder` behind the URL allowlist |
| **Redis in local dev** | `InMemoryCache` has identical semantics and needs no container. | `TieredCache` (L1+L2) and `RedisCache` written; `TMM_CACHE_BACKEND=redis` switches |
| **Glue jobs** | Explicitly offline-only. No WhatsApp turn touches Glue, so it is not on the critical path for launch. | S3 analytics bucket, lifecycle policy and sanitised export path provisioned |
| **Load tests** | Meaningful numbers need real RDS Proxy and real Lambda cold starts; running Locust against in-memory adapters would produce a number that means nothing. | `tests/load/` marker registered; concurrency correctness is covered by the optimistic-lock test |
| **Direct MySQL writer** | The Laravel service layer should apply its own business rules. A second writer is a second source of truth. | Documented as a compatibility path behind two feature flags, restricted to two tables; **not implemented**, deliberately |
| **Tutor availability capture** | The website stores none (audit §3), so there is nothing to sync. Building a capture flow is a separate product decision. | `tutor_availability` table owned by this service; the evaluator reports `MISSING` honestly and the guard blocks any schedule claim |
| **Tier-2 model routing** | No trigger has been observed in practice yet; adding one speculatively would spend money for no measured gain. | `ModelTier.REASONING` defined and configurable; escalation requires an explicit trigger |

---

## 4. KNOWN RISKS

Ordered by how likely they are to hurt.

### R1 — Availability is the biggest data gap · **High impact, certain**

The website holds no tutor availability at all. Today the agent can say "she
teaches CBSE Class 10 Maths" but not "she is free Tuesday at 7". Every guard is
in place so it never *claims* a slot it cannot support, but the parent
experience is materially weaker until availability is captured. **This is a data
problem, not a code problem** — the schema, evaluator and slot-suggestion logic
are ready and tested.

### R2 — Category hierarchy assumption · **Medium impact, medium likelihood**

`teacher_course_managment.pid/cid/cat_id` → board/class/subject is inferred from
Laravel relation names, not from a documented schema (assumptions A15). If it is
wrong, capability data from that schema is garbled. Mitigated: the mapping is one
function, and the sync job counts `category_shape_mismatch`, so a wrong
assumption shows up in metrics rather than in bad matches. **Confirm with the
Laravel team before production.**

### R3 — Two source-of-truth writers if the MySQL fallback is ever enabled

The direct-MySQL writer is off and unimplemented. If someone enables it later,
the website's business rules stop being applied to agent writes. The feature
flags and this note are the control; keep it that way.

### R4 — Lambda `/tmp` WAL is not durable

Chitragupta events spooled during an outage live in per-container `/tmp` and are
lost on recycle. Accepted: memory events are audit and personalisation, not part
of the matching decision. A long outage means incomplete backfill, not wrong
matches.

### R5 — No NAT Gateway means split VPC placement

Per the brief, no NAT Gateway is provisioned. The outbound worker therefore runs
**outside** the VPC to reach Meta. This is stated in `infra/terraform/main.tf`
and its README rather than worked around silently. Review with whoever owns the
network before applying.

### R6 — In-process rate limiting is approximate across containers

With `TMM_CACHE_BACKEND=memory`, the per-conversation limiter is per-container.
SQS FIFO serialises a conversation to one worker at a time, so the practical gap
is small — but the **global** limiter is only meaningful with Redis. Enable
Redis before relying on it in production.

### R7 — Cost estimates are placeholders

`DEFAULT_TOKEN_COST_MICROS` exists so a cost graph is available at all, not
because the numbers are current. Override from SSM before treating
`LlmCostMicros` as financial data.

### R8 — Fee comparison across an unknown unit

`register.budget` has no unit. A parent's "900 per hour" is compared against a
unitless tutor band using a generous policy ratio. This is the honest handling of
bad data, but it means fee filtering is coarse. **Adding a fee-unit column to the
website would improve match quality more than any change to this service.**

### R9 — 232 tests, zero live integrations

Every external boundary is exercised against a faithful double. Doubles encode
assumptions. The contract tests reduce this by reading the real sibling-repo
source, but the first live run against Meta, OpenAI, MySQL and Chitragupta will
still surface things no double predicted. Plan a staged rollout with a low
`rollout_percentage`.

---

## What was found and fixed during the build

Recording these because they are the bugs a review would look for, and each one
now has a regression test:

1. **"sector 57" parsed as a ₹57 budget** — numeric context filtering added.
2. **"class 10 ... after 6:30" scheduled tuition at 10:00** — clock readings are
   now positionally bound to the after/before keyword.
3. **"class 9 ka" parsed as ₹9,000** — the `k` multiplier now needs a word
   boundary.
4. **"exam" matched the AM marker** — word-boundary matching.
5. **A phone written `98765 43210` was not redacted** — the most common Indian
   format was leaking; the pattern now handles internal separators.
6. **An unknown tutor city scored as a city *mismatch*** — absence is not
   contradiction.
7. **Recorded availability *lowered* a tutor's rank** when the parent stated no
   timing preference — the treatment is now symmetric.
8. **"urgent" (start soon) read as "fast-paced teaching"**, penalising patient
   tutors.
9. **Missing data made a tutor look better** — the combiner averaged only known
   dimensions, so a 1-review tutor outranked a 17-review one. Now unknown weight
   is filled with the neutral prior.
10. **A published fee was hidden** when the parent stated no budget, because the
    claim was gated on the wrong dimension.
11. **The migration safety gate false-positived** on the initial migration;
    it now distinguishes indexes on new tables from indexes on populated ones.

---

## Before production

1. Confirm R2 (category hierarchy) with the Laravel team.
2. Provision the read-only MySQL grant; run one sync; check
   `report_staleness` output.
3. Have the Laravel team build `POST /internal/agent/commands`.
4. Ask lead-intake to enable its webhook client.
5. Provide real secrets; confirm `Settings` rejects placeholders outside local.
6. `terraform plan` and review the VPC placement in R5.
7. Enable Redis (R6).
8. Override token costs from SSM (R7).
9. Deploy to dev, run the smoke test, then a staged production rollout.
