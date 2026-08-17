# Production control evidence matrix

**Service:** `tutor-match-meta` (NXTutors TutorMatch Meta Agent)
**Schema revision:** 0003 · **Event contract:** v1
**Suite at time of writing:** 491 collected — 472 passed, 18 skipped (integration, no DSN), 1 xfail (upstream)

---

## How to read this

Every control below carries an **objective**, the **risk** it exists to
prevent, where it **lives**, its **configuration**, the **automated test** that
proves it, the **metric** it emits, the **alarm** that fires, an **owner**, the
**rollback trigger** it participates in, and a **verification status**.

Statuses mean exactly one thing each:

| Status | Meaning |
| --- | --- |
| **VERIFIED** | An automated test executes the control and asserts it *refuses*, in this repository, on every CI run. |
| **VERIFIED (STATIC)** | Proven by structural analysis of the source rather than by executing it — used where the runtime needs infrastructure CI does not have. |
| **NOT EXECUTED** | Implemented, but the proving test needs a dependency that was unavailable. Never counted as passing. |
| **PARTIAL** | The control exists and is tested, but a stated part of it is unproven. The gap is named. |

A claim without a named test file is not in this table.

**Owners** are roles, not people: `platform` (service engineering), `oncall`
(whoever holds the pager), `data` (analytics/BI), `product` (matching quality
and business policy).

---

## Summary

| # | Control | Status |
| --- | --- | --- |
| 1 | Constraints | VERIFIED |
| 2 | Goal alignment | VERIFIED |
| 3 | Deterministic state control | VERIFIED |
| 4 | Modularity | VERIFIED (STATIC) |
| 5 | Context optimisation | VERIFIED |
| 6 | Tool-call boundaries | VERIFIED (STATIC) |
| 7 | Security, trust & compliance | PARTIAL |
| 8 | Data privacy / PII masking | VERIFIED |
| 9 | Input guardrails | VERIFIED |
| 10 | Output guardrails | VERIFIED |
| 11 | Meta/WhatsApp compliance | PARTIAL |
| 12 | Human-in-the-loop | PARTIAL |
| 13 | Cost & performance | VERIFIED |
| 14 | Token budgeting & circuit breakers | VERIFIED |
| 15 | API latency | NOT EXECUTED |
| 16 | State management | VERIFIED |
| 17 | Error handling & retries | VERIFIED |
| 18 | Fallback mechanisms | VERIFIED |
| 19 | Observability & lifecycle | PARTIAL |
| 20 | Execution tracing | PARTIAL |
| 21 | Usage analytics | VERIFIED |
| 22 | Performance drift evaluation | NOT EXECUTED |
| 23 | Version control | VERIFIED |
| 24 | Rollback triggers | PARTIAL |

Four controls are **NOT EXECUTED or PARTIAL for reasons that block release**;
they are listed in `docs/release-gate-report.md` §Blockers and repeated in the
Go/No-Go of `docs/production-readiness-final.md`.

---

## 1. Constraints

| Field | Value |
| --- | --- |
| **Objective** | The agent operates only inside its declared remit: match a family to real NXTutors tutors. It does not invent tutors, does not answer outside the tutoring domain, and does not take actions it was not asked for. |
| **Risk** | An agent that quietly widens its own scope. The concrete failure is a shortlist entry describing a tutor who does not exist, or an availability slot nobody confirmed. |
| **Implementation** | `orchestration/evidence_guard.py` (claims must name their backing `SkillScore`), `orchestration/output_guard.py` (the finished message), `domain/identity.py` (a link that cannot be resolved is dropped, not guessed), `orchestration/orchestrator.py::_build_entries`. |
| **Configuration** | `config/policies/*.yaml` — `explanation.citable_dimensions`, `thresholds.min_sample_size`. No weight or threshold appears in Python source. |
| **Automated test** | `tests/security/test_invariants.py` (anti-fabrication), `tests/security/test_output_guard.py::TestOutputGuard` (19 cases), `tests/e2e/test_conversations.py`. |
| **Metric** | `FabricationViolations`, `GuardRefusals`, `OutputGuardRejected`. |
| **Alarm** | `tutor-match-meta-*-fabrication-violations` (threshold 0, 1 period), `-output-guard-rejections` (threshold 0). |
| **Owner** | product |
| **Rollback trigger** | Any `FabricationViolations > 0` → roll back the prompt version first, then the scoring-policy version. |
| **Status** | **VERIFIED** |

---

## 2. Goal alignment

| Field | Value |
| --- | --- |
| **Objective** | Optimise for match quality, not for conversion. A materially important mismatch is disclosed even when disclosing it loses the booking. |
| **Risk** | Silent optimisation for the metric that is easiest to measure. §38 of the brief names this directly. |
| **Implementation** | `matching/hard_filters.py` (constraints are hard, not weighted), `scoring/policy.py` (multi-objective weights, versioned), `orchestration/missing_info.py` (ask rather than assume), `orchestration/output_guard.py::_UNSUPPORTED_SUPERLATIVES`. |
| **Configuration** | `config/policies/*.yaml`, seven named policies selected by `scoring/selector.py`. |
| **Automated test** | `tests/unit/test_matching.py`, `tests/security/test_output_guard.py::test_an_unsupported_superlative_is_refused`, `tests/security/test_invariants.py` (protected attributes never scored). |
| **Metric** | `NoMatch`, `MatchAccepted`, `DemoRequested`, `WeightCoverage`, `ShortlistSize`. |
| **Alarm** | `-no-match-rate` (a *sudden drop* is as suspicious as a spike; see the runbook). |
| **Owner** | product |
| **Rollback trigger** | Top-3 acceptance regression, or no-match rate moving more than 30% against the previous policy version. |
| **Status** | **VERIFIED** — for the anti-fabrication and non-discrimination properties. Conversion-vs-quality balance is a *product* judgement measured offline (control 22), not something a test can assert. |

---

## 3. Deterministic state control

| Field | Value |
| --- | --- |
| **Objective** | One conversation has exactly one coherent state, whatever order messages arrive in and however many workers see them. |
| **Risk** | Two workers advancing the same conversation, or a stale worker rolling state backwards. §25. |
| **Implementation** | `state/machine.py` (explicit FSM, invalid transitions refused), `repositories/postgres.py::PostgresConversationStore.save` (the optimistic lock is a conditional `UPDATE … WHERE lock_version = :expected`, never a read-then-write), SQS FIFO `MessageGroupId = conversation_id`. |
| **Configuration** | `infra/terraform/main.tf` — `deduplication_scope = "messageGroup"`, `fifo_throughput_limit = "perMessageGroupId"`, `batch_size = 1`. |
| **Automated test** | `tests/e2e/test_resilience.py::TestConversationConcurrency` (the exact §25 scenario: "Class 10" → "CBSE" → "Maths after 7"), `::test_a_stale_worker_cannot_overwrite_newer_state`, `::test_distinct_conversations_do_not_interfere`. |
| **Metric** | `OptimisticLockConflict`, `InvalidTransition`. |
| **Alarm** | `-optimistic-lock-conflicts` (>20 per 5 min, 2 periods) — almost always means FIFO grouping is not being honoured upstream. |
| **Owner** | platform |
| **Rollback trigger** | Sustained lock conflicts after a deploy that changed the FSM or the queue configuration. |
| **Status** | **VERIFIED** |

---

## 4. Modularity

| Field | Value |
| --- | --- |
| **Objective** | Each of the eight matching skills is bounded, side-effect-free and independently testable; the orchestrator owns sequencing and nothing else. |
| **Risk** | A monolithic scorer nobody can reason about, where a change to proximity silently alters subject matching. |
| **Implementation** | `matching/*/evaluator.py` (eight modules, each returning a `SkillScore`), `matching/base.py` (`SkillEvaluator` protocol), `repositories/ports.py` (every dependency is a `Protocol`). |
| **Configuration** | `matching/__init__.py::default_evaluators()`. |
| **Automated test** | `tests/unit/test_matching.py` (each evaluator in isolation), `tests/e2e/test_resilience.py::test_a_misbehaving_evaluator_does_not_fail_the_match` (one scorer raising must not fail the turn). |
| **Metric** | — (structural property). |
| **Alarm** | — |
| **Owner** | platform |
| **Rollback trigger** | — |
| **Status** | **VERIFIED (STATIC)** — enforced by the Protocol boundary and by the fact that the whole pipeline runs against in-memory adapters with no infrastructure. |

---

## 5. Context optimisation

| Field | Value |
| --- | --- |
| **Objective** | A model call carries the smallest sufficient context: a compact stable prefix, the current requirement, the relevant recent turns, bounded memory, bounded retrieval. Never the whole conversation history. |
| **Risk** | Cost that grows with conversation length, and an injection surface that grows with it too. |
| **Implementation** | `orchestration/model_context.py` (positive-projection `ModelContext`), `prompts/registry.py` (stable prefix / variable suffix split, so provider prefix caching can hit), `rag/pipeline.py` (`top_k` and a token budget), `orchestration/turn_service.py` (structured state is reloaded, history is not replayed). |
| **Configuration** | `TMM_RAG_TOP_K` (5), `TMM_RAG_TOKEN_BUDGET` (1200), `TMM_LLM_MAX_OUTPUT_TOKENS` (800). |
| **Automated test** | `tests/security/test_model_payload.py` (15 cases, including `test_a_new_projection_field_cannot_leak_by_default`), `tests/unit/test_rag.py`. |
| **Metric** | `LlmTokens`, `PromptCacheHitRate`. |
| **Alarm** | `-llm-spend-rate`. |
| **Owner** | platform |
| **Rollback trigger** | Prompt-cache hit rate collapsing after a prompt change — usually means per-call content leaked into the stable prefix. |
| **Status** | **VERIFIED** |

---

## 6. Tool-call boundaries

| Field | Value |
| --- | --- |
| **Objective** | A model can return schema-valid data and nothing else. It cannot invoke a tool, reach the database, or emit free text to a parent. |
| **Risk** | The classic agent failure: a successful injection becomes an action. |
| **Implementation** | `integrations/llm/provider.py` — the `LLMProvider` protocol has exactly one method, `structured()`, and `schema` is required; there is no completion method. `repositories/ports.py::CandidateQuery` is a frozen dataclass of typed fields with no free-text entry point. |
| **Configuration** | Strict JSON-schema mode with `additionalProperties: false` and `required` on every key (`prompts/extraction.py::EXTRACTION_SCHEMA`). |
| **Automated test** | `tests/security/test_sql_injection.py::TestNoSqlSurface` (the port cannot express an injectable query), `::test_every_interpolated_sql_fragment_is_a_module_constant` (AST walk over every SQL-building module, with a planted-violation self-check), `tests/security/test_output_guard.py::TestAdversarial`. |
| **Metric** | `InjectionDetected`. |
| **Alarm** | `-injection-campaign` (>25 per 5 min). |
| **Owner** | platform |
| **Rollback trigger** | Security alarm. |
| **Status** | **VERIFIED (STATIC)** — the guarantee is the *absence* of an API, which is proven structurally. The adversarial end-to-end cases in `TestAdversarial` are executed. |

---

## 7. Security, trust & compliance

| Field | Value |
| --- | --- |
| **Objective** | Every inbound request is authenticated; every outbound destination is allowlisted; no credential is ever logged. |
| **Risk** | Webhook forgery, replay, SSRF, credential leakage. Full analysis in `docs/threat-model.md`. |
| **Implementation** | `security/signing.py` (HMAC over method+path+timestamp+body, so a signature cannot be replayed against another endpoint), `api/internal.py::_authorised` (constant-time compare, fails closed on an unset secret), `security/urls.py` (scheme + host allowlist + private-range and metadata-endpoint block, DNS resolved), `infra/terraform/main.tf` (per-function IAM roles; ingress can only `sqs:SendMessage`). |
| **Configuration** | `TMM_INGRESS_SIGNING_KEY`, `TMM_INGRESS_TIMESTAMP_TOLERANCE_SECONDS` (300), `TMM_INTERNAL_SECRET`, `TMM_INGRESS_MAX_BODY_BYTES` (65536). Secrets come from Secrets Manager; Terraform references the secret by name so no value enters state. |
| **Automated test** | `tests/security/test_invariants.py::TestRequestSigning` (including path-binding replay), `::TestUrlPolicy` (SSRF, metadata endpoint, credentials-in-URL), `tests/security/test_sql_injection.py`. CI: `pip-audit --strict`, gitleaks, `ruff --select S`, bandit, semgrep. |
| **Metric** | `RateLimited`, `InjectionDetected`. |
| **Alarm** | `-ingress-5xx`, `-injection-campaign`. |
| **Owner** | platform |
| **Rollback trigger** | Security alarm; leaked-credential incident (`docs/runbooks/leaked-key.md`). |
| **Status** | **PARTIAL** — every listed control is tested. Not covered: key rotation has a documented runbook but no automated rotation, and no external penetration test has been run against a deployed environment. Both are named in the release gate. |

---

## 8. Data privacy / PII masking

| Field | Value |
| --- | --- |
| **Objective** | Raw PII appears in no log, no metric label, no analytics export, no model payload, no exception trace and no DLQ dashboard. |
| **Risk** | The highest-consequence failure in the system. Full field-by-field classification in `docs/data-classification.md`. |
| **Implementation** | `security/pii.py` (peppered pseudonyms; `assert_label_safe` raises at runtime on an identifying metric dimension), `observability/context.py` (the request context holds `conversation_id_hash`, and has no field for a phone number or a message body), `analytics/events.py` (closed dimension allowlist, refused at construction), `orchestration/model_context.py` (positive projection). |
| **Configuration** | `TMM_HASH_PEPPER` (required outside local — settings refuse to boot without it), `TMM_ANALYTICS_INCLUDE_RAW_TEXT` (must be false outside local, enforced by a validator). |
| **Automated test** | `tests/security/test_model_payload.py` (15), `tests/security/test_analytics_privacy.py` (16), `tests/security/test_invariants.py::TestPiiRedaction`, `tests/security/test_cache_hygiene.py::test_no_key_builder_embeds_a_raw_identifier`. |
| **Metric** | — deliberately. A metric counting PII incidents would be a metric labelled by PII. |
| **Alarm** | Log-metric filter on `analytics event rejected` (WARN) — an `UnsafeDimension` in production means a producer tried to export something it should not. |
| **Owner** | platform |
| **Rollback trigger** | Any confirmed leak → immediate application rollback, `docs/runbooks/privacy-incident.md`. |
| **Status** | **VERIFIED** — with one accepted exception, documented: `outbox_event.payload.recipient` holds a raw E.164 number, but only under `outbound_ownership=tutor_match_sends`, only while a message is undelivered, and never in logs. See `contracts/outbound.py`. |

---

## 9. Input guardrails

| Field | Value |
| --- | --- |
| **Objective** | Malformed, oversized, hostile and abusive input is handled safely — **without** over-blocking ordinary questions. |
| **Risk** | Two symmetric failures: executing hostile input, and refusing a parent whose message merely contains a scary word. §5 calls out both. |
| **Implementation** | `contracts/inbound.py` (`max_length` on every field; `extra="forbid"` where the shape is ours), `security/injection.py` (sanitisation and untrusted-data wrapping), `security/rate_limit.py::AbuseDetector` (escalating, wired into `turn_service.handle`), `handlers/ingress.py` (auth before parse). |
| **Configuration** | `MAX_MESSAGE_CHARS` (4000), `MAX_UNTRUSTED_CHARS` (2000), `TMM_INGRESS_MAX_BODY_BYTES`, the six `TMM_RATE_LIMIT_*` settings. |
| **Automated test** | `tests/security/test_sql_injection.py::TestHostileTextIsData` (7 payloads × full turn), `tests/security/test_output_guard.py::test_sql_text_from_a_parent_is_data_not_a_command` (asserts the *innocent* case is not refused), `tests/security/test_invariants.py::TestPromptInjection`. |
| **Metric** | `AbuseSignal`, `AbuseEnforcement`, `ContactHarvestAttempt`, `InjectionDetected`, `RateLimited`. |
| **Alarm** | `-injection-campaign`. |
| **Owner** | platform |
| **Rollback trigger** | Security alarm. |
| **Status** | **VERIFIED** |

---

## 10. Output guardrails

| Field | Value |
| --- | --- |
| **Objective** | No message reaches a parent unvalidated. Every tutor named exists and is eligible; every link is canonical; every fee is authorised; no guarantee, no internal score, no database error, no prompt text. |
| **Risk** | A hallucinated tutor, a 404 profile link, an invented price, a promised exam result. |
| **Implementation** | `orchestration/output_guard.py` — runs on the assembled bytes, after every template and model, with no path back to the generator. A failure substitutes `SAFE_FALLBACK` and counts the incident; it is never patched up and sent. Wired at `turn_service.py::_guard_output` on **both** the shortlist and the clarification path. |
| **Configuration** | `MAX_MESSAGE_CHARS` (1400 — well under WhatsApp's 4096, because a longer shortlist is unreadable on a phone), `prompts/registry.py::FORBIDDEN_CLAIM_MARKERS`. |
| **Automated test** | `tests/security/test_output_guard.py` — 25 cases, including `test_the_fallback_itself_passes_validation` (a fallback that failed its own guard would loop). |
| **Metric** | `OutputGuardRejected`. |
| **Alarm** | `-output-guard-rejections` (threshold 0, 1 period — this is a correctness incident, not a warning). |
| **Owner** | product |
| **Rollback trigger** | Any rejection → roll back the prompt version. |
| **Status** | **VERIFIED** |

---

## 11. Meta / WhatsApp Business compliance

| Field | Value |
| --- | --- |
| **Objective** | One sender, one webhook, no double sends; message length within Meta's limit; permanent delivery failures distinguished from transient ones. |
| **Risk** | Two agents replying to the same parent; retrying a message into a closed 24-hour window; a Business Account penalty. |
| **Implementation** | `contracts/handoff.py::OutboundOwnership` plus `config/settings.py::_enforce_single_outbound_owner` — the both-senders-enabled configuration is *unrepresentable*, rejected at boot. `integrations/whatsapp/outbound.py` (4096-char cap; `retryable` distinguishes 429/5xx from a closed window). `contracts/outbound.py` (one typed delivery contract across producer, relay and worker). |
| **Configuration** | `TMM_OUTBOUND_OWNERSHIP` (`caller_sends` default), `TMM_WHATSAPP_ENABLED`. |
| **Automated test** | `tests/contract/test_outbound_delivery.py` (8), `tests/contract/test_agent_harmony_contracts.py`, `tests/e2e/test_multi_agent_harmony.py`. |
| **Metric** | `OutboxPending`, `OutboxDead`, `OutboxReclaimed`. |
| **Alarm** | `-outbound-dlq-not-empty`, `-outbox-dead`. |
| **Owner** | platform |
| **Rollback trigger** | Duplicate-send report from a parent or from Lead Intake. |
| **Status** | **PARTIAL** — the ownership invariant, the delivery contract and the retry classification are all tested. **The 24-hour session window and message-template approval are not modelled at all**: under `caller_sends` (the deployed default) Lead Intake owns that concern, but switching to `tutor_match_sends` without adding it would risk sending outside the window. Named in the release gate. |

---

## 12. Human-in-the-loop interventions

| Field | Value |
| --- | --- |
| **Objective** | High-risk actions require a human, and every approval records who, what, why, and the before/after state. |
| **Risk** | An agent granting a discount, overriding a verification, or booking a demo it should not have. |
| **Implementation** | `orchestration/orchestrator.py::HUMAN_REVIEW_COVERAGE_FLOOR` (thin evidence escalates), `orchestration/routing.py::Intent.HUMAN_REQUESTED`, `state/machine.py` (`HUMAN_HANDOFF` trigger), `repositories/models.py::ApprovalRow` + `approval_audit` table (migration 0003), `config/kill_switches.py::AUTO_DEMO_PAUSED` → `ESCALATE_TO_HUMAN`. |
| **Configuration** | `TMM_FLAG_AUTO_DEMO_ENABLED` (false), `HUMAN_REVIEW_COVERAGE_FLOOR` (0.35). |
| **Automated test** | `tests/e2e/test_conversations.py` (escalation on thin evidence), `tests/e2e/test_multi_agent_harmony.py` (human-requested routing). |
| **Metric** | `HumanHandoff`. |
| **Alarm** | `-human-handoff-spike`. |
| **Owner** | product |
| **Rollback trigger** | HITL rate spike → roll back the scoring-policy version. |
| **Status** | **PARTIAL** — escalation *into* human review is implemented and tested. **The approval workflow out of it is not**: `approval_request` and `approval_audit` exist as schema with no service writing to them, and no operator surface to approve from. Fee overrides and discounts are therefore not currently possible at all, which is safe but incomplete. Named in the release gate. |

---

## 13. Cost & performance

| Field | Value |
| --- | --- |
| **Objective** | Cost per completed match is bounded, measured and alarmed, and no expensive call is made where deterministic code suffices. |
| **Risk** | An LLM call per tutor; a paid geocode per candidate; re-embedding an unchanged corpus. Each is a multiple-of-budget mistake. |
| **Implementation** | `integrations/llm/routing.py` (purpose→model; `Purpose` is the complete list of reasons to call a model, and date arithmetic, distance, sorting, SQL filtering, availability intersection and fee comparison are deliberately absent), `integrations/geo/provider.py` (pre-geocoded tutors, one capped lookup per turn, haversine locally), `rag/embeddings.py` (content-hash skip), `repositories/cached_tutors.py` (short-TTL pool cache), `cache/postgres_store.py` (no ElastiCache). |
| **Configuration** | `TMM_MODEL_*`, `TMM_GEOCODE_MAX_CALLS_PER_TURN` (1), `TMM_GEOCODE_CACHE_TTL_SECONDS` (86400), `TMM_CACHE_BACKEND` (`postgres`). Full model in `docs/cost-architecture.md`. |
| **Automated test** | `tests/unit/test_cost_controls.py` (25), `tests/security/test_cache_hygiene.py::TestPoolCacheCorrectness`. |
| **Metric** | `LlmCostMicros`, `LlmCalls`, `LlmTokens`, `EmbeddingsSkipped`, `EmbeddingCostMicros`, `GeocodeCalls`, `GeocodeCacheHits`, `CacheHitRatio`. |
| **Alarm** | `-llm-spend-rate`, `-llm-budget-exhausted`, AWS Budgets at 80% actual / 100% forecast. |
| **Owner** | platform |
| **Rollback trigger** | Cost per completed match rising more than 50% against the previous release. |
| **Status** | **VERIFIED** — for the controls. The *absolute* cost figures in `docs/cost-architecture.md` are modelled from formulas, not measured, and are labelled as such. |

---

## 14. Token budgeting & circuit breakers

| Field | Value |
| --- | --- |
| **Objective** | Model spend is bounded in four independent dimensions, and a provider outage sheds load instead of queueing behind it. |
| **Risk** | Tokens alone do not bound cost. The realistic runaway is *call count*: a redelivery loop making many cheap calls. |
| **Implementation** | `integrations/llm/provider.py::TokenBudget` (tokens, calls/turn, calls/conversation, escalations — each with its own ceiling), `integrations/llm/routing.py::GuardedProvider` (kill switch → budget → rate limit → **then** the provider), `integrations/llm/openai_provider.py::CircuitBreaker` (CLOSED/OPEN/HALF_OPEN with a single probe). |
| **Configuration** | `TMM_LLM_CONVERSATION_TOKEN_BUDGET` (40000), `TMM_LLM_MAX_CALLS_PER_TURN` (2), `TMM_LLM_MAX_CALLS_PER_CONVERSATION` (12), `TMM_LLM_MAX_ESCALATIONS_PER_CONVERSATION` (1), `TMM_RATE_LIMIT_LLM_PER_CONVERSATION_PER_MINUTE` (4). |
| **Automated test** | `tests/unit/test_cost_controls.py::TestTokenBudget` (5), `::TestGuardedProvider` (6 — each asserts `inner.calls == 0`, i.e. the control refuses *before* money is spent), `tests/e2e/test_resilience.py::TestDependencyFailure`. |
| **Metric** | `LlmBudgetExceeded`, `LlmPaused`, `CircuitOpen`. |
| **Alarm** | `-llm-budget-exhausted` (>10 per 15 min — usually a redelivery loop, so check `DuplicateEvents` before raising any ceiling). |
| **Owner** | platform |
| **Rollback trigger** | Spend alarm → `LLM_PAUSED` kill switch (degrades to deterministic matching, does not stop the service). |
| **Status** | **VERIFIED** |

---

## 15. API latency

| Field | Value |
| --- | --- |
| **Objective** | Stage-level latency is measured separately, so queue wait is never hidden inside a generic "latency" number. p50/p95/p99 per stage. |
| **Risk** | A backlog that looks healthy right up until parents complain, because processing time is fast and queue time is invisible. |
| **Implementation** | `observability/metrics.py` — eleven `STAGE_*` metrics (ingress validation, queue wait, state load, memory lookup, extraction, candidate SQL, skill scoring, RAG, explanation, persistence, outbound enqueue), emitted from `orchestration/turn_service.py` around each stage. Queue age comes from SQS's own `ApproximateAgeOfOldestMessage`. |
| **Configuration** | `TMM_POSTGRES_STATEMENT_TIMEOUT_MS` (5000), `TMM_LLM_TIMEOUT_SECONDS` (12), API Gateway integration timeouts below each function's own timeout. |
| **Automated test** | `tests/load/locustfile.py` — five profiles (steady, burst, hot conversation, duplicate storm, malformed). **Not executed:** requires a deployed environment. |
| **Metric** | `StageQueueWaitMs`, `StageCandidateSqlMs`, `StageExtractionMs`, …, `TurnLatencyMs`, `ShortlistLatencyMs`. |
| **Alarm** | `-match-queue-age` (>120s, 2 periods), `-internal-p95-latency` (>1800ms, below Lead Intake's 2s timeout). |
| **Owner** | platform |
| **Rollback trigger** | p95 regression >2× the previous release. |
| **Status** | **NOT EXECUTED** — the instrumentation and the profiles exist; no load run has happened, so **the SLOs and the safe concurrency ceiling are unvalidated assumptions**. Release blocker. |

---

## 16. State management

| Field | Value |
| --- | --- |
| **Objective** | Conversation state, requirements and decisions are durable, versioned and auditable; the cache is never the system of record. |
| **Risk** | A decision nobody can explain six months later; a cached answer served after the underlying tutor deactivated. |
| **Implementation** | `repositories/models.py` (`match_decision` stores the policy id, version *and checksum*, the candidate pool, every rejection and the full score vector), `repositories/cached_tutors.py` (freshness recomputed on read, never restored from the entry), `cache/base.py` (`TieredCache.L1_MAX_TTL_SECONDS = 30`, because L1 cannot receive a cross-container invalidation). |
| **Configuration** | `DEFAULT_TTLS` (`pool` 120s, `tutor` 300s, `geo` 86400s), `TMM_PROJECTION_FRESH_HOURS` (6), `TMM_PROJECTION_AGING_HOURS` (24). |
| **Automated test** | `tests/security/test_cache_hygiene.py` (17, including `test_freshness_is_recomputed_not_restored` and `test_a_full_turn_succeeds_against_a_cache_that_stores_nothing`), `tests/e2e/test_conversations.py::test_the_decision_is_fully_auditable`. |
| **Metric** | `CacheHitRatio`, `CacheErrors`, `ProjectionStalenessHours`. |
| **Alarm** | `-projection-stale` (>24h; `treat_missing_data = "breaching"`, because no data means the sync job is not running). |
| **Owner** | platform |
| **Rollback trigger** | Stale-data incident. |
| **Status** | **VERIFIED** |

---

## 17. Error handling & retries

| Field | Value |
| --- | --- |
| **Objective** | Errors are classified. Validation, authorisation, forbidden transitions and business rejections are never retried; transient failures are, with capped jittered backoff, and every retrying write is idempotent. |
| **Risk** | Infinite retries burning budget on a permanent error; or a lost message because a transient error was treated as permanent. |
| **Implementation** | `integrations/llm/openai_provider.py` (429/timeout retried with full-jitter backoff; schema/auth/4xx raised immediately), `sync/outbox_relay.py::BACKOFF_SECONDS` (30s→1h, capped, then `dead`), `handlers/lambda_entry.py` (partial batch failures; an unparseable outbound record is now *reported* rather than silently dropped), `repositories/postgres.py` (`ON CONFLICT DO NOTHING` + `rowcount` — the database decides who wins a race). |
| **Configuration** | `TMM_LLM_MAX_RETRIES` (2), `maxReceiveCount` 3 (match) / 5 (outbound), `CLAIM_LEASE_SECONDS` (900). |
| **Automated test** | `tests/e2e/test_resilience.py::TestDependencyFailure` (4 LLM error classes parametrised), `::TestDuplicateDelivery::test_a_failed_turn_releases_its_claim_for_a_genuine_retry`, `tests/integration/test_postgres_stores.py::TestOutboxLease` (NOT EXECUTED). |
| **Metric** | `DlqDepth`, `OutboxDead`, `OutboxReclaimed`, `DuplicateEvents`. |
| **Alarm** | `-match-dlq-not-empty`, `-outbound-dlq-not-empty`, `-outbox-dead`. |
| **Owner** | oncall |
| **Rollback trigger** | DLQ growth after a deploy. |
| **Status** | **VERIFIED** |

---

## 18. Fallback mechanisms

| Field | Value |
| --- | --- |
| **Objective** | Every dependency has an explicit, documented degraded behaviour. A third-party outage never becomes a total platform failure. |
| **Risk** | An outage in an optional dependency taking down the core matching path. |
| **Implementation** | `config/kill_switches.py::POLICIES` (each switch declares its safe behaviour *as data*, with a rationale and a `lossless` flag), `repositories/ports.py::DegradedSources` (recorded on every decision), `orchestration/orchestrator.py` (a dependency failure degrades the answer, never fails the turn). Full table in `docs/fallback-matrix.md`. |
| **Configuration** | Seven kill switches in the `kill_switch` table, TTL-cached for `TMM_KILL_SWITCH_TTL_SECONDS` (10s). |
| **Automated test** | `tests/e2e/test_resilience.py::TestKillSwitches` (3), `::TestDependencyFailure` (7). Notably `test_matching_paused_declines_without_consuming_the_dedup_key` — the ordering property that makes `MATCHING_PAUSED` genuinely lossless. |
| **Metric** | `DegradedTurn`, `KillSwitchActive`, `ChitraguptaFailures`, `WebsiteFailures`, `MysqlFailures`. |
| **Alarm** | `-kill-switch-active` (held down for an hour → confirm it is intentional). |
| **Owner** | oncall |
| **Rollback trigger** | — (fallbacks *prevent* rollback). |
| **Status** | **VERIFIED** |

---

## 19. Observability & lifecycle

| Field | Value |
| --- | --- |
| **Objective** | Structured JSON logs with a correlation id on every line; metrics for traffic, errors, latency, queue age, DLQ depth, cost and quality; no high-cardinality label. |
| **Risk** | An incident nobody can diagnose, or a CloudWatch bill larger than the service. |
| **Implementation** | `observability/context.py` (`JsonFormatter`; the context holds a hash, and JSON encoding also neutralises log injection by escaping newlines), `observability/metrics.py` (EMF — the log line *is* the metric, so no `PutMetricData` latency and no extra IAM), `security/pii.py::assert_label_safe` (raises at runtime on an identifying dimension). |
| **Configuration** | `TMM_LOG_LEVEL`, `log_retention_days` (30). Noisy libraries pinned to WARNING because they log full request bodies at INFO. |
| **Automated test** | `tests/security/test_invariants.py` (label safety), `tests/security/test_analytics_privacy.py`. |
| **Metric** | 60+ named in `observability/metrics.py::Metric` — one enum, so dashboards cannot drift from the code. |
| **Alarm** | 14 CloudWatch alarms across `main.tf`, `api.tf` and `cost.tf`. |
| **Owner** | oncall |
| **Rollback trigger** | — |
| **Status** | **PARTIAL** — logging, metrics and alarms are implemented and tested. **No dashboard is provisioned**: the alarms exist but there is no single pane an on-call engineer can open, and the p50/p95/p99 views control 15 calls for do not exist yet. Named in the release gate. |

---

## 20. Execution tracing

| Field | Value |
| --- | --- |
| **Objective** | One trace shows inbound → router → queue → state load → memory → requirement → SQL → scores → rank → explanation → persistence → outbound, with provider spans exposing latency, status and retry count but never secrets or PII. |
| **Risk** | Knowing a turn was slow without knowing which stage. |
| **Implementation** | `trace_id` minted at ingress and carried on the `InboundEnvelope` through SQS, so the worker inherits it rather than minting a new one; `RequestContext` propagated by `ContextVar`; X-Ray `tracing_config { mode = "Active" }` on all five functions; per-stage timings via the `STAGE_*` metrics. |
| **Configuration** | `X-Trace-Id` header, `observability/context.py::request_context`. |
| **Automated test** | `tests/e2e/test_conversations.py::test_the_decision_is_fully_auditable` (trace id survives to the decision row). |
| **Metric** | `STAGE_*` (11). |
| **Alarm** | — |
| **Owner** | platform |
| **Rollback trigger** | — |
| **Status** | **PARTIAL** — correlation is complete and tested end to end; X-Ray is enabled. **Explicit sub-segments per stage are not emitted**, so X-Ray shows one span per function rather than the stage breakdown; the stage timings are available as metrics instead. Acceptable, but it is less than §30 asks for. |

---

## 21. Usage analytics

| Field | Value |
| --- | --- |
| **Objective** | A funnel that can be exported to S3 and read by a BI tool without a second privacy review. |
| **Risk** | Raw WhatsApp messages in a data lake. |
| **Implementation** | `analytics/events.py` — a closed shape: a name from a fixed enum, a pseudonymous ref, and a dimension bag whose keys are allowlisted and whose values are bucketed. An unlisted dimension raises `UnsafeDimension` at construction. Emitted fire-and-forget from `turn_service`, so an analytics outage is not an incident. |
| **Configuration** | `TMM_ANALYTICS_ENABLED`, `ALLOWED_DIMENSIONS` (20 keys, none free-text), Glue crawler daily at 04:00. |
| **Automated test** | `tests/security/test_analytics_privacy.py` (16) — including a full real turn asserting no phone number, no street-level locality and no raw conversation id reaches the export. |
| **Metric** | — (analytics is the metric). |
| **Alarm** | — |
| **Owner** | data |
| **Rollback trigger** | Privacy violation in the export. |
| **Status** | **VERIFIED** |

---

## 22. Performance drift evaluation

| Field | Value |
| --- | --- |
| **Objective** | Track hard-filter correctness, top-1/top-3 acceptance, no-match precision, subject/board error, availability conflict, replacement rate, unsupported claims, HITL rate, cost and latency across versions. Multi-objective, never conversion alone. |
| **Risk** | Quality regressing invisibly across releases. |
| **Implementation** | Every decision is persisted with its policy id, version and checksum (`match_decision`), which is what makes a retrospective comparison possible at all. `config/flags.py::ShadowComparison` supports shadow-mode evaluation. |
| **Configuration** | `TMM_FLAG_SHADOW_MODE` (true by default — real data, no parent-visible effect). |
| **Automated test** | **None.** |
| **Metric** | `WeightCoverage`, `NoMatch`, `MatchAccepted`, `DemoRequested`, `HumanHandoff`. |
| **Alarm** | `-no-match-rate`, `-human-handoff-spike`. |
| **Owner** | product |
| **Rollback trigger** | Match-quality regression → roll back the scoring-policy version. |
| **Status** | **NOT EXECUTED** — **there is no evaluation dataset and no evaluation harness.** The plumbing to compare versions exists; the thing to compare against does not. This means a scoring-policy change cannot currently be evaluated offline before rollout, which is what §27 requires. Release blocker. |

---

## 23. Version control

| Field | Value |
| --- | --- |
| **Objective** | Every build exposes app version, git SHA, schema migration revision, scoring-policy version, prompt versions and event-contract version — separately, because they roll back independently. |
| **Risk** | Not knowing which code produced a decision; or needing an application rollback to undo a bad prompt. |
| **Implementation** | `version.py::build_info()`, surfaced at `GET /internal/v1/version` (authenticated with the same shared secret as the handoff — none of it is secret, but an unauthenticated build-identity endpoint hands an attacker a version to look up). Prompt versions come from `prompts/registry.py`, each with a checksum computed from the text, so an edit that forgets to bump the version is caught by a test. |
| **Configuration** | `TMM_GIT_SHA`, `TMM_BUILD_ID` stamped by the deploy pipeline; `TMM_PROMPT_PINS` pins a prompt to an earlier version **without a deploy**. |
| **Automated test** | `tests/security/test_invariants.py::test_every_prompt_declares_the_data_clause` (enumerates the registry), `make doctor` resolves every prompt and policy. `/ready` fails if a pin names a version this build does not contain. |
| **Metric** | — |
| **Alarm** | — |
| **Owner** | platform |
| **Rollback trigger** | — (this control *enables* rollback). |
| **Status** | **VERIFIED** |

---

## 24. Rollback triggers

| Field | Value |
| --- | --- |
| **Objective** | Explicit, alarmed triggers, each naming what to roll back — application, prompt, scoring policy, model config, feature flag or RAG corpus. |
| **Risk** | An incident where the only available action is a full application rollback, when the actual fault was one prompt. |
| **Implementation** | `infra/terraform/cost.tf::local.quality_alarms` — eight alarms, each with an `alarm_description` naming its runbook. Independent rollback axes: `TMM_PROMPT_PINS` (prompt), `TMM_DEFAULT_POLICY` (scoring), `TMM_MODEL_*` (model), `TMM_FLAG_*` (feature), `.github/workflows/rollback.yml` (application). |
| **Configuration** | See `docs/production-readiness-final.md` §16 for the exact procedures. |
| **Automated test** | `tests/e2e/test_resilience.py::TestKillSwitches` proves the emergency path; the rollback workflow itself is untested. |
| **Metric** | Every metric in the `quality_alarms` map. |
| **Alarm** | 8 quality alarms + 2 cost + 4 infrastructure. |
| **Owner** | oncall |
| **Rollback trigger** | — |
| **Status** | **PARTIAL** — triggers are defined, alarmed and routed to runbooks. **No rollback has been rehearsed**, and `.github/workflows/rollback.yml` has never been executed against a real deployment. Named in the release gate. |

---

## Controls the brief asks for that are deliberately *not* implemented

Stating these explicitly, because an unstated omission reads as an oversight.

| Item | Decision | Reason |
| --- | --- | --- |
| Redis / ElastiCache | Not used | The only always-on hourly-billed component in an otherwise scale-to-zero architecture, and it lives in a VPC that has no NAT Gateway. PostgreSQL is already on the path. `cache/postgres_store.py`. |
| NAT Gateway | Not provisioned | Explicit constraint. Functions needing internet egress (`outbound-worker`, and any OpenAI traffic) run outside the VPC; VPC-attached functions reach AWS services through the endpoints in `endpoints.tf`. |
| Fargate / ECS | Not used | Explicit constraint. Every compute path is Lambda. |
| Glue on the request path | Never | §23. Glue runs daily over the sanitised S3 export, and has no route to PostgreSQL or to the queues. |
| Reranking model in RAG | Not used | §19 says rerank only when evidence shows benefit. There is no evidence yet; `top_k=5` with metadata pre-filtering is the current design. |
| Per-tutor LLM calls | Never | §15. The pattern is SQL hard filter → bounded pool → deterministic scoring → at most one bounded explanation call. |
