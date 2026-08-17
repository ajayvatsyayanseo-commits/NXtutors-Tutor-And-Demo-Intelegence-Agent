# Multi-Agent Harmony Report

Date: 2026-08-11 · Phase: integration and distributed-systems correctness
Environment: local Windows, Python 3.12.10, no AWS, no live external service.

**Harmony is not claimed because services compile.** Every classification below
names what was executed and what was observed.

---

## Executed evidence

```
ruff check src tests scripts migrations   All checks passed
mypy src                                  Success: no issues found in 114 source files
pytest -q                                 300 passed, 1 xfailed
tutor-match-e2e                           OK: every claim was backed by recorded evidence
```

New in this phase: 31 harmony journeys (A–H), 37 cross-agent contract tests.

---

## The primary rule, and how it is enforced

> One message → one authoritative route, one state owner, one outbound decision.

| Failure mode | Enforcement | Test |
| --- | --- | --- |
| Two agents answer the same message | `route()` returns `DECLINED`; Lead Intake keeps the conversation | journeys A, B |
| Duplicated WhatsApp messages | 5-layer idempotency chain; redelivery returns `duplicate=True` with no reply | journey F |
| Two senders | `Settings` **rejects** `caller_sends` + `whatsapp_enabled` together | `TestOutboundOwnership` |
| A→B→A loops | TutorMatch has **no graph edge** back to Lead Intake; `find_cycles() == []` | 11 graph tests |
| Unbounded handoffs | `hop_count` + ordered `visited_agents` in the envelope, `MAX_HOPS=6` | 5 envelope tests |
| Conflicting memory | Provenance ranking; memory never overwrites what the parent said | `Tracked.beats` tests |
| Silent overwrites | Optimistic lock is a conditional UPDATE | concurrency tests |
| Inconsistent tutor IDs | `register.user_id` everywhere; URL token pinned to the blade template | contract test |
| Mixed scoring versions | `policy_id` + `version` + `checksum` stamped on every decision | contract test |
| Competing Meta webhooks | We expose none; asserted by scanning our own `api/` for webhook markers | contract test |

---

## VERIFIED END TO END

Provable locally with no external dependency.

| Capability | Evidence |
| --- | --- |
| **Handoff graph is acyclic** | `find_cycles() == []` over a DFS of `ALLOWED_EDGES` |
| **TutorMatch cannot call its caller** | `LEAD_INTAKE ∉ reachable_from(TUTOR_MATCH)`; `next_hop` raises `LoopDetected` |
| **Hop budget enforced** | Envelope validation rejects `hop_count > 6`; revisiting an agent raises |
| **Self-handoff impossible** | Model validation rejects `source == destination` |
| **Single outbound owner** | Both misconfigurations raise at settings load |
| **Continuation tokens are safe** | Forged → rejected; cross-conversation replay → rejected; expired → start fresh; contents contain no PII |
| **Idempotency across redelivery** | Same `wa_message_id` twice → one decision, one reply |
| **Concurrent redelivery** | `asyncio.gather` of the same message → at most one reply |
| **Shadow mode computes without replying** | Decision persisted, `DECLINED` returned, `shadow_mode` flagged |
| **Bucketing is stable and proportional** | Same conversation always same bucket; 25% → 400–600 of 2000 |
| **Human request short-circuits** | Wins from any state, including mid-match |
| **Private data never returned** | No email, no 10-digit mobile, no schema words in any reply |
| **Envelope carries no PII** | Serialised envelope contains no phone/email fragment |
| **Trace propagation** | `trace_id`/`correlation_id` survive hops; `causation_id` names the parent event |
| **Event versioning** | All 13 types versioned; additive fields parse; unknown type raises |

## VERIFIED LOCALLY / EXTERNAL LIVE TEST REQUIRED

Complete and contract-pinned; not yet exercised against the live counterpart.

| Integration | Pinned against | Remaining |
| --- | --- | --- |
| **Lead Intake handoff** | Their `onboarding_router.py` — payload keys, auth header, `{status, reply_text}`, `{200,202}`, 2s timeout | Point `TUTOR_MATCHING_AGENT_WEBHOOK_URL` at us and enable integrations |
| **Signup intent sync** | Their `SIGNUP_INTENT_RE` markers | Live overlap traffic |
| **Chitragupta** | The real SDK's `events.py` — deed regex, lifecycle, required keys, secret refusal | Base URL + key |
| **OpenAI** | Strict schema, budget, circuit breaker; stub exercises timeout/429/schema paths | Real key is in `.env`; not yet called |
| **PostgreSQL** | Conditional-UPDATE lock, `ON CONFLICT` idempotency, `SKIP LOCKED` outbox | `CREATE DATABASE tutormatch;` then `alembic upgrade head` |
| **Website read** | SQL written against the production dump DDL | A reachable MySQL host |

## BLOCKED BY MISSING EXTERNAL CONTRACT

| Blocker | Impact | Owner |
| --- | --- | --- |
| `TUTOR_MATCHING_AGENT_INTERNAL_SECRET` absent from Lead Intake `Settings` | Handoff auth cannot be read; value already in their `.env` | Lead Intake team |
| Laravel `POST /internal/agent/commands` does not exist | No write-back, no teacher-dashboard visibility | Laravel team |
| No reachable MySQL host | Projection sync unrun; runtime uses synthetic tutors | Infrastructure |
| Lead Intake webhook client is a stub | Their integration router returns `webhook_mode_not_enabled_for_external_calls` | Lead Intake team |

## INTENTIONALLY DISABLED

| Disabled | Why | Re-enable |
| --- | --- | --- |
| TutorMatch WhatsApp sender | Lead Intake owns delivery | `outbound_ownership=tutor_match_sends` + `whatsapp_enabled=true` (together, or settings reject it) |
| Website write-back | Endpoint does not exist | `TMM_WEBSITE_WRITE_ENABLED=true` |
| Direct MySQL writer | Bypasses Laravel business rules | Two feature flags; not implemented on purpose |
| Chitragupta | No credential | `TMM_CHITRAGUPTA_ENABLED=true` |
| RAG / memory / auto-demo flags | Staged rollout | Individual flags |
| Live traffic | Ships dark | `TMM_FLAG_SHADOW_MODE=false`, then raise the percentage |

---

## Bugs found and fixed in this phase

Each has a regression test:

1. **Conversation lane renamed itself mid-conversation.** `effective_conversation_id()`
   preferred `lead_id`, which appears only after intake resolves the lead — so
   turn 1 was `wa:+91…` and turn 3 was `lead:L-1`. That split one parent across
   two states, two idempotency namespaces and two continuation scopes, and broke
   resume in exactly the case it exists for. Phone now takes precedence.
2. **A follow-up with no keyword was declined.** "Actually class 9 now" carries no
   matching intent, so we handed it back and Lead Intake would have answered a
   question *we* asked. Ownership now also consults our own FSM state, not just
   the continuation token.
3. **"create an account" was claimed as a tutoring request.** The subject hint
   `accounts?` matched the singular "account". Accountancy is a real subject
   students call "accounts", so the fix is plural-only — signup goes to
   onboarding, "class 12 accounts tutor" still reaches us.

---

## Known risks

**R1 — The upstream secret field is a one-line change we cannot make.**
Until Lead Intake adds `TUTOR_MATCHING_AGENT_INTERNAL_SECRET` to `Settings`,
the handoff cannot authenticate. The value is already in their `.env`. Tracked
by an `xfail` test that flips green when they add it.

**R2 — Contract tests read source, not a running service.**
They catch a rename or a removed key, not a behavioural change that keeps the
same shape. First live traffic remains the real test.

**R3 — Two intent classifiers exist.**
Lead Intake classifies, then we classify again. We honour their label when they
send one, but they do not send one today. Divergence shows up as a declined
message, which is safe (they keep it) but loses us the turn.

**R4 — Shadow mode doubles matching cost with no parent benefit.**
That is the point, and it is temporary. Watch `LlmCalls` during shadow and keep
`TMM_FLAG_LLM_ENABLED` off if the cost is unwelcome — deterministic extraction
still produces comparison data.

**R5 — `/tmp` WAL is not durable across Lambda recycles.**
Accepted: memory events are audit, not decision inputs.

**R6 — The handoff endpoint runs the full match inline.**
It fits inside the 2s budget locally against in-memory adapters. With real
PostgreSQL and a Tier-1 LLM call it may not. If p99 approaches 2s, switch to
`ACCEPTED` + the outbound queue — the code path already exists.

---

## Rollout sequence

1. Lead Intake adds the `Settings` field (R1).
2. `CREATE DATABASE tutormatch;` then `alembic upgrade head`.
3. Point `TUTOR_MATCHING_AGENT_WEBHOOK_URL` at the deployed URL.
4. Uncomment `INTEGRATIONS_ENABLED` / `INTEGRATION_MODE` in their `.env`.
5. Run in shadow (current default) and compare decisions for a week.
6. `TMM_FLAG_SHADOW_MODE=false`, `TMM_FLAG_PERCENTAGE_ROLLOUT=5`.
7. Watch `FabricationViolations` (must stay 0), `NoMatch`, `ShortlistLatencyMs`.
8. Raise to 25 → 50 → 100.

Rollback at any point: set `TMM_FLAG_ENABLED=false`. Every conversation
immediately returns `DECLINED` and Lead Intake resumes answering, with no
deploy and no data loss.

---

## Completion gate

| Requirement | Status |
| --- | --- |
| One unambiguous WhatsApp routing model | ✅ Lead Intake owns it; we expose no webhook |
| No duplicate outbound ownership | ✅ Structurally impossible via settings validation |
| TutorMatch accepts a Lead Intake handoff | ✅ Exact payload shape, pinned |
| Onboarding can pause/resume a MatchSession | ✅ Signed, bound, expiring tokens |
| Chitragupta works with minimal scopes | ✅ `purpose=tutor_matching`, no wildcards |
| Website/MySQL ownership explicit | ✅ Ownership matrix, no dual-master |
| Dashboard write-back uses a safe contract | ✅ Typed commands; blocked on their endpoint |
| Important events versioned | ✅ 13 types, all `.v1` |
| Every write idempotent | ✅ 5-layer chain |
| Retries cannot duplicate | ✅ Journey F |
| Loops bounded | ✅ Acyclic graph + hop budget |
| Agent contracts tested | ✅ 37 contract tests against real sibling source |
| Failure isolation works | ✅ Journey G |
| HITL works | ✅ Journey E |
| Trace propagation works | ✅ Envelope tests |
| Rollout flags work | ✅ 6 rollout tests |
| Compatibility docs current | ✅ 8 documents |
