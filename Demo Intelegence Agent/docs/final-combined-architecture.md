# Final combined architecture

**NXTutors Tutor and Demo Intelligence Agent** — how two deployables behave as
one product.

**Date:** 2026-08-17

---

## 1. The shape

```
                        WhatsApp (Meta Cloud API)
                                  │
                                  ▼
                        ┌──────────────────┐
                        │   Lead Intake    │   (separate agent, upstream)
                        └────────┬─────────┘
                                 │ signed handoff — OWNERSHIP TRANSFERS
                                 ▼
   ┌─────────────────────────────────────────────────────────────┐
   │              DEMO COMMAND CENTER AGENT                      │
   │              owns the conversation from here on             │
   │                                                             │
   │   ingress → orchestrator → capability lanes → outbound      │
   │        (the ONE state machine, the ONE send path)           │
   └───────────────┬─────────────────────────────────────────────┘
                   │ in-process call — OWNERSHIP DOES NOT MOVE
                   ▼
   ┌─────────────────────────────────────────────────────────────┐
   │              TUTOR INTELLIGENCE AGENT                       │
   │              return-only. Sends nothing. Owns nothing.      │
   │                                                             │
   │   hard filters → 8 evaluators → policy rank → evidence      │
   └───────────────┬─────────────────────────────────────────────┘
                   │ ranked candidates + evidence
                   ▼
   ┌─────────────────────────────────────────────────────────────┐
   │              DEMO COMMAND CENTER AGENT (resumes)            │
   │   schedule → remind → demo → objections → forecast →        │
   │   offer → discount → payment → activation                   │
   └───────────────┬─────────────────────────────────────────────┘
                   │ handoff — OWNERSHIP TRANSFERS, exactly once
                   ▼
                        ┌──────────────────┐
                        │    Onboarding    │   (separate agent, downstream)
                        └──────────────────┘
```

## 2. Why the Tutor half is a dependency and not a peer

The obvious design — hand the conversation to Tutor Intelligence for the
matching step and take it back afterwards — was rejected. The reason is
specific, not stylistic.

Tutor Intelligence has a complete agent surface: its own FSM, its own outbox,
its own WhatsApp sender. An integration point that hands it a conversation
(`/internal/v1/handoff`) returns a `reply_text` *and* can send that text itself.
Two agents would then each believe they were responsible for the reply, and the
parent would receive the tutor shortlist twice. That is not a hypothetical —
duplicate sends are the single most reported failure in multi-agent WhatsApp
systems, and they are invisible in testing because each agent's own logs look
correct.

So Demo calls exactly one function:

```python
TutorMatchOrchestrator.match(request) -> MatchResult
```

That object is constructed with no sender, no outbox and no conversation store.
It cannot send a message because it has nothing to send one with. The
"return-only" property is therefore structural — it survives a careless edit in
a way a code comment or a feature flag would not.

`tests/security/` asserts it from the other direction too: that the Demo
adapter's dependency graph contains no object satisfying `WhatsAppPort`.

## 3. Ownership

Exactly one agent owns a conversation at any instant. Ownership is a row, not a
convention:

| Movement | Mechanism | Ownership |
| --- | --- | --- |
| Lead Intake → Demo | Signed handoff envelope, HMAC over raw bytes | **Transfers** |
| Demo → Tutor Intelligence | In-process function call | **Does not move** |
| Demo → Onboarding | Signed event, idempotent, exactly once | **Transfers** |

A capability call is not an ownership transfer. This distinction is what makes
the eight Demo capabilities safe to run concurrently in separate Lambdas: they
compute and return, and only the orchestrator writes conversation state.

## 4. The deterministic core

| Element | Count |
| --- | ---: |
| States | 30 |
| Triggers | 49 |
| Actors | 7 |
| Declared transitions | 45 |

The transition table is pure data. `StateMachine.fire()` performs no I/O, makes
no provider call and reads no clock it was not handed — so the same
`(state, trigger, actor)` always produces the same next state, and the whole
lifecycle is testable without a database.

Persistence is separate and optimistic: every write carries the version the
caller read, and a mismatch raises `ConcurrencyConflict` rather than
last-write-wins. Verified live against the real database, not just in memory —
see `final-e2e-report.md` §4.

## 5. The one send path

Every outbound message in the Demo half goes through a single boundary. Nothing
else may call the WhatsApp sender. That boundary applies, in order:

1. **Output guard** — no fabricated tutor fact, no invented price, no link to a
   host outside the allowlist.
2. **PII rules** — what may leave, and to whom.
3. **Opt-out check** — a recipient who opted out receives nothing, ever.
4. **Template rules** — outside the 24-hour session window, only an approved
   template, and only one whose name is confirmed.
5. **Idempotency** — the same logical message is never sent twice, including
   across a Lambda retry.

Because there is one boundary, adding a rule is one edit, and forgetting to
apply a rule to a new message type is impossible.

## 6. The transactional outbox

A message is never sent inside the transaction that decided to send it. The
decision writes a row; a separate relay sends it and marks it sent. This means:

- A crash between "decided" and "sent" retries the send, not the decision.
- A crash between "sent" and "marked" resends — which the idempotency key
  absorbs.
- The database is never held open across a provider call.

Ownership transfer is applied **after** the outbox flush. That ordering is not
cosmetic: applying it before caused a real bug where the welcome message was
composed by an agent that no longer owned the conversation and was silently
dropped.

## 7. Sagas

Two flows cross a boundary that cannot be rolled back by the database, so both
are sagas with explicit compensation in reverse order:

| Saga | Steps | Compensates |
| --- | ---: | --- |
| `BOOKING` | 5 | Release slot hold, cancel calendar event, notify |
| `PAYMENT` | 5 | Void order, restore prior state, escalate to a human |

`RELEASE_SLOT_HOLD` on the tutor-acceptance step exists because of a found bug:
a Google Calendar failure after a successful hold left the slot locked with no
demo attached, quietly making the tutor unbookable at that time forever.

## 8. Persistence

One database, two schemas, one connection string.

| | Tutor half | Demo half |
| --- | --- | --- |
| Schema | `tutor_match` | `demo_agent` |
| Tables | 22 | 36 (+1 migration ledger = 37 live) |
| Access | SQLAlchemy + asyncpg | asyncpg direct, or Aurora Data API |
| Migrations | Alembic | numbered SQL, `scripts/apply_migrations_dsn.py` |

Demo supports four persistence modes so the same code runs locally, in tests, in
a Lambda with the Data API, and against a plain PostgreSQL instance:

| Mode | Used by |
| --- | --- |
| `memory` | tests, `make demo` |
| `postgres_dsn` | local development and the current shared database |
| `data_api` | Lambda against an Aurora cluster with the Data API enabled |
| `lambda_proxy` | a function that reaches the database through another function |

The `search_path` is set **per acquisition** (`setup=`, not `init=`). asyncpg
issues `RESET ALL` when returning a connection to the pool, so an `init=`
callback works for the first query after pool creation and silently fails for
every one after it — with `UndefinedTableError`, which is a confusing symptom
because the schema, the grant and the table are all correct.

## 9. Network boundary

The Demo half creates no VPC resources and needs no NAT Gateway. Its
dependencies — SQS, Secrets Manager, EventBridge, the Aurora Data API, Meta,
Google, Cashfree, OpenAI — are all reachable over public endpoints, and a Lambda
outside a VPC has internet egress by default.

That is the whole reason a NAT Gateway (roughly $32/month plus per-GB, before
any traffic) does not appear in the Demo cost model.

The Tutor half **does** use a VPC and S3, legitimately. Those are prohibited for
**new Demo** resources only. `scripts/scan_prohibited.py` therefore scans
`Demo Intelegence Agent/` and not the repository root.

Caveat, stated rather than hidden: in `postgres_dsn` mode the database is
reached by direct connection, and a Lambda outside a VPC has no stable source
address for a security group to admit. See `final-integration-gaps.md` §2.

## 10. Compute topology

| Resource | Count |
| --- | ---: |
| Lambda functions | 13 (11 workers + ingress + Cashfree webhook) |
| SQS queues | 10 (5 lanes + 5 dead-letter queues) |
| CloudWatch alarms | 16 |
| IAM roles | 2 role resources, 8 distinct policy documents |
| S3 buckets | 0 |
| NAT Gateways | 0 |
| Always-running workers | 0 |

Five lanes, priority ordered: `payment` (smallest pool, highest consequence),
`scheduling`, `outbound`, `reminders`, `analytics` (largest pool, safe to
starve).

One package deploys to all 13 functions. Separate packages would triple build
time and guarantee version skew between workers that exchange typed events.

## 11. Where authority lives

No language model has authority anywhere in either half. The model reads text
and proposes; a deterministic mechanism decides:

| Decision | Decided by |
| --- | --- |
| Which tutors are eligible | Hard filters. A model cannot override one. |
| The ranking | A versioned `ScoringPolicy`, checksummed onto the decision |
| Which tutor the parent picked | Ordinal lookup in the persisted candidate snapshot |
| Whether a slot is free | A partial unique index on `(tutor, minute)` |
| The conversation's state | A 45-row transition table with actor authorization |
| The discount | A deterministic band engine with a price floor |
| Whether payment succeeded | HMAC over raw bytes + exact amount reconciliation |
| Whether someone attended | Calendar RSVP or conference participation, never inference |

The tool registry makes this concrete: of 12 registered tools, **5 are
model-facing**. The financial and booking tools are not exposed to the model at
all, and `FORBIDDEN_TOOL_NAMES` prevents one being added by name.

## 12. Failure handling

Errors are classified before they are retried. `ErrorClass` × `Disposition`
decides retry, backoff, dead-letter or escalate — so a 400 is never retried 5
times and a 429 is never dead-lettered on the first attempt.

Per-provider circuit breakers carry a declared `Degradation`. When a provider is
open, the system says what it cannot currently do. It never fabricates the
answer the provider would have given — no invented Meet link, no assumed
payment, no guessed availability.

## 13. What the two halves share, exactly

| Shared | Not shared |
| --- | --- |
| One `.env` file | Any table |
| One database | Any schema |
| One correlation id per conversation | Any FSM |
| The envelope contract (pinned by a contract test) | Any outbox or sender |
| One Python virtualenv locally | Any deployment package |

Demo never queries a Tutor table. Tutor never writes Demo state. The only
coupling is one function call and one envelope shape.
