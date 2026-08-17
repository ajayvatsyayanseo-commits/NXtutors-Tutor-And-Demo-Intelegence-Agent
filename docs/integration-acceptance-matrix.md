# Integration Acceptance Matrix

Every external boundary, with no ambiguous ownership for any production data
mutation.

Status: **VERIFIED E2E** · **VERIFIED LOCALLY** (external live test required) ·
**BLOCKED** (missing external contract) · **DISABLED** (deliberately off)

---

## 1. Lead Intake → TutorMatch (inbound handoff)

| | |
| --- | --- |
| Owner | Lead Intake calls; TutorMatch serves |
| Contract | `LeadIntakeHandoffV1` → `HandoffResponseV1`, pinned to `onboarding_router.py` |
| Endpoint | `POST /internal/v1/handoff` |
| Auth | `X-NXTUTORS-INTERNAL-SECRET`, constant-time compare, **fails closed** |
| Timeout | 2.0s caller-side; we enqueue and answer inside it |
| Retries | Caller retries 5xx only; we never return 5xx for a business outcome |
| Idempotency | `wa_message_id` → dedup key → DB claim. Redelivery returns `duplicate=True`, no second reply |
| Circuit breaker | Caller-side; we degrade to `ERROR` so they use their own fallback |
| Fallback | `DECLINED`/`ERROR` → Lead Intake keeps the conversation |
| PII | Raw phone received, hashed immediately, never stored raw or logged |
| Test | `test_agent_harmony_contracts.py` (9 tests) + journeys A/F |
| **Status** | **VERIFIED LOCALLY** — needs `TUTOR_MATCHING_AGENT_INTERNAL_SECRET` in their `Settings` |

## 2. Outbound WhatsApp delivery

| | |
| --- | --- |
| Owner | **Lead Intake** (`whatsapp_client.py`) |
| Contract | We return `reply_text`; they send it |
| Enforcement | `outbound_ownership=caller_sends` + `whatsapp_enabled=false`; settings validation **rejects both being on** |
| Idempotency | conversation + source event + purpose |
| Fallback | Delivery failure retries transport only; the decision is never recomputed |
| Test | `TestOutboundOwnership` (3), `test_tutor_match_exposes_no_public_webhook` |
| **Status** | **VERIFIED LOCALLY** — one sender is structurally guaranteed |

## 3. Onboarding detour (pause/resume)

| | |
| --- | --- |
| Owner | Onboarding owns signup; TutorMatch owns the paused MatchSession |
| Contract | `NEEDS_HANDOFF` + signed `continuation_token` |
| Auth | HMAC-SHA256, bound to the conversation, 6h TTL |
| Idempotency | Session id inside the token |
| Fallback | Invalid/expired token → start fresh, never act on stale intent |
| PII | Token contains a session id and a hash only — asserted by test |
| Test | Journey B (6 tests) |
| **Status** | **VERIFIED LOCALLY** — Lead Intake must round-trip the token |

## 4. Chitragupta memory

| | |
| --- | --- |
| Owner | Chitragupta |
| Contract | `POST /v1/memory/query`, `POST /v1/memory/events`; pinned to the real SDK |
| Auth | `X-Agent-Id` + `X-Api-Key` |
| Scopes | `purpose=tutor_matching` only — never a wildcard |
| Timeout / retries | 3s, 2 retries, circuit breaker |
| Idempotency | `idempotency_key` per event; gateway dedupes |
| Fallback | Degraded packet + WAL spool. **Never blocks a match** |
| PII | Hashed conversation ref; no transcript, no raw prompt |
| Test | 6 contract tests against the real SDK source + journey G |
| **Status** | **VERIFIED LOCALLY** — needs `CHITRAGUPTA_BASE_URL` + key |

## 5. Website read (tutor projection)

| | |
| --- | --- |
| Owner | Website/MySQL is canonical; our projection is a derived copy |
| Contract | 7 allowlisted tables, explicit column allowlist |
| Auth | Read-only DB user, or the signed Laravel API when it exists |
| Idempotency | Upsert on `tutor_id` with a source checksum |
| Fallback | Stale rows excluded from matching; freshness alarm |
| PII | `password`, `otp`, `email`, `phone`, `dob`, KYC and `address` are never selected |
| Test | `test_no_private_columns`, projection mapping tests |
| **Status** | **BLOCKED** on a MySQL host — SQL written against the real DDL |

## 6. Website write-back

| | |
| --- | --- |
| Owner | Website; we submit typed commands only |
| Contract | 4 commands, each with idempotency, risk class and audit envelope |
| Auth | HMAC-signed, timestamped, replay-protected |
| Idempotency | `sha256(command + identity)`; 409 treated as success |
| Fallback | Transactional outbox + relay; HITL for high-risk |
| PII | No phone/email in any command payload — asserted by test |
| Test | 5 contract tests |
| **Status** | **BLOCKED** — Laravel `POST /internal/agent/commands` does not exist yet |

## 7. Teacher dashboard visibility

| | |
| --- | --- |
| Owner | Website |
| Contract | `CreateTutorMatch` / `PublishTutorLead` with explicit statuses |
| Statuses | `MATCH_CREATED` → `TUTOR_SHORTLISTED` → `PARENT_INTERESTED` → `DEMO_REQUESTED` → … |
| Enforcement | Only typed commands change state; **no LLM path can write** |
| Audit | Every transition carries actor, source, timestamp, reason, trace id |
| **Status** | **BLOCKED** on the same Laravel endpoint |

## 8. OpenAI

| | |
| --- | --- |
| Owner | TutorMatch (own budget) |
| Contract | `LLMProvider` Protocol, strict JSON schema |
| Auth | API key from the shared NXTutors account |
| Timeout / retries | 12s, 2 retries with jitter, circuit breaker |
| Budget | Per-conversation token cap; Tier 2 needs an explicit trigger |
| Fallback | Deterministic extraction; a full outage still produces a shortlist |
| PII | Redacted + pseudonymised before the call |
| Test | Journey G, `test_llm_outage` |
| **Status** | **VERIFIED LOCALLY** — real key present in `.env`, not yet called live |

## 9. Loop prevention and tool boundaries

| | |
| --- | --- |
| Owner | TutorMatch |
| Contract | `ALLOWED_EDGES`, `MAX_HOPS=6`, ordered `visited_agents` |
| Enforcement | `find_cycles() == []`; TutorMatch has no edge back to Lead Intake |
| Test | 11 tests |
| **Status** | **VERIFIED E2E** — locally provable, no external dependency |

---

## Ownership of every production mutation

| Mutation | Single owner | Path | Ambiguity |
| --- | --- | --- | --- |
| Send a WhatsApp message | Lead Intake | we return text | **none** |
| Create/modify an account | Onboarding → Website | not ours | **none** |
| Create an enquiry / demo row | Website | typed command | **none** |
| Tutor dashboard match status | Website | typed command | **none** |
| Conversation matching state | TutorMatch | our Postgres | **none** |
| Match decision + policy version | TutorMatch | our Postgres | **none** |
| Tutor projection | TutorMatch (derived) | sync job; MySQL wins conflicts | **none** |
| Cross-agent memory | Chitragupta | deed events | **none** |
| Tutor availability | TutorMatch | our Postgres | **none** |

No dual-master ownership anywhere.

## Outstanding external blockers

1. `TUTOR_MATCHING_AGENT_INTERNAL_SECRET` — one `Settings` field in Lead Intake.
   Value already in their `.env`. Tracked by an `xfail` test.
2. Laravel `POST /internal/agent/commands` — blocks write-back and dashboard
   visibility.
3. A reachable MySQL host — blocks the first real projection sync.
4. `INTEGRATIONS_ENABLED=true` + `INTEGRATION_MODE=webhook` in Lead Intake —
   left commented in their `.env`; enabling is their rollout decision.
