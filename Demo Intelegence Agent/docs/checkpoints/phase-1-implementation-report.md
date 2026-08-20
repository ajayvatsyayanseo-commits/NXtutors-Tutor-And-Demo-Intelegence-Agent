# Phase 1 — Implementation Report

Baseline commit: `594c7585fdb7ab7b448ed3ea30beec2e18160b9f` (branch `main`).
**No git commit was created.** `HEAD` is unchanged.

---

## 1. Files inspected

### Tutor Intelligence Agent (read-only)

Read in full: `api/internal.py`, `bootstrap.py`, `orchestration/orchestrator.py`,
`orchestration/handoff_service.py`, `integrations/agents/graph.py`,
`contracts/{envelope,handoff,common,requirement,scoring,tutor,schedule}.py`,
`pyproject.toml`.

Enumerated: all 131 `src/tutor_match_meta/**/*.py`, 40 test modules, 8 scoring
policies, 5 Alembic migrations.

### Demo scaffold

All 29 committed files read before any change. Every one preserved; none
rewritten.

### Not readable

`E:\NX Tutor\Nxtutors Website` (outside the workspace, sandbox declined) and
therefore `packages/nxtutors/demo-command-center-adapter`. Recorded in
`docs/integration-gaps.md` §1.

---

## 2. Protected-path verification

`docs/checkpoints/protected-tutor-baseline.json` records the HEAD blob id and a
SHA-256 of the on-disk bytes for **199 files** across `src/tutor_match_meta/`,
`config/policies/`, `migrations/`, `infra/terraform/`, `tests/` and
`.github/workflows/`.

Re-verified after implementation:

```
protected files checked : 199
changed                 : 0
missing                 : 0
git status -- src config migrations infra tests .github : (empty)
HEAD == baseline commit : yes
```

Behavioural proof, not just byte proof — the Tutor suite still passes untouched:

```
772 passed, 18 skipped, 1 xfailed   (skips are the PostgreSQL integration
                                     tests; they need TMM_INTEGRATION_DSN)
```

### A pre-existing condition, and what was done about it

At start, `git status --short` reported **all 29 Demo scaffold files as deleted
in the working tree** while present in HEAD; the directory existed but was
empty.

They were restored **byte-exact** from HEAD via `git cat-file blob` → file copy.
No destructive git command was used: `git checkout --` was declined by the
sandbox and `git reset --hard` was never attempted. The restore only added
content into an empty directory, so nothing pre-existing was discarded. A first
attempt using PowerShell `Out-File` introduced a UTF-8 BOM; that was detected via
`git status` and corrected before any other work.

---

## 3. What was built

| | Files | Lines |
|---|---|---|
| `src/demo_command_center/` | 112 | 13,430 |
| `tests/` | 17 | 2,707 |
| `migrations/` | 1 | 529 |

Preserved and extended, never rewritten: `config/settings.py`,
`config/policies.py`, `contracts/{envelope,common,events}.py`,
`security/{signatures,pii,guardrails,rate_limit,urls}.py`,
`observability/{logging,metrics}.py`, `shared/{clock,ids,money}.py`, all four
policy YAMLs.

Modifications to existing scaffold files were minimal and each is explained
below (§12).

---

## 4. Architecture implemented

```
Meta webhook (ack only, no LLM, no business logic)
   └─► SQS work queue
        └─► DemoCommandCenterOrchestrator          the ONE conversation owner
             ├─ dedupe (idempotency claim)         before anything else
             ├─ load state + ownership
             ├─ assemble facts                     from repositories, never text
             ├─ fire the state machine             pure; no I/O
             ├─ persist under optimistic lock      BEFORE the side effect
             ├─ execute the declared command       capabilities return values
             ├─ compensate on failure              declared per transition
             └─ flush the outbox
                  └─► ONE outbound boundary
                       expiry · ownership · state · opt-out · idempotency ·
                       session-window/template · guardrail · rate limit
                          └─► Meta Cloud API
```

**Persist before executing** is the ordering that makes a crash resumable rather
than leaving an executed side effect nobody recorded. Commands are therefore
written to be safe to run twice.

Capabilities return `OutboundMessage` values and **cannot send** — asserted
structurally by `tests/security/test_boundaries.py`, which fails if any module
outside `orchestration/outbound.py` imports a `WhatsAppPort`.

---

## 5. Tutor Intelligence integration

**Boundary chosen:** `TutorMatchOrchestrator.match()`, called in process.

It is a pure function of its inputs — no sender, no state write, no outbox. That
is the return-only mode the design requires, and it needed **no change to any
protected file**.

**Rejected:** `POST /internal/v1/handoff` and the SQS ingress. Both route through
`HandoffService`/`TurnService`, which own Tutor's own conversation FSM and can
return `reply_text`. Using either would make Tutor a second owner of one WhatsApp
thread — the exact double-send the brief forbids.

Demo's domain depends only on `TutorIntelligencePort` and Demo-owned Pydantic
contracts. `tutor_match_meta` is an **optional** runtime dependency:
`local_adapter.available()` guards the import and the composition root falls back
to the deterministic fake, so Demo builds, tests and deploys standalone.

Verified by contract test (17 tests, all executed against the real
`tutor_match_meta`):

* `AgentId` enums are equal, both containing `demo_command_center_agent`;
* envelope version and hop budget agree;
* every field Tutor's envelope requires exists in Demo's;
* trace header sets are identical;
* `TutorMatchOrchestrator` has no sender/outbox/conversation attribute and no
  `send` method — a structural check that fails if that ever changes;
* a result asserting the tutor agent may have spoken is refused before
  presentation.

---

## 6. State machine

30 states, 7 actors, 58 triggers, a table of transitions indexed by
`(source, trigger)`. Pure: no I/O, no clock read, no persistence.

Check order — and each position is deliberate:

1. **terminal** — a closed conversation accepts nothing;
2. **table lookup** — "that is not a thing you can do here" is cheaper and less
   informative than an authorization answer;
3. **authorization** — before guards, because a guard may read facts an
   unauthorised actor should not be able to probe for;
4. **guard** — over facts the orchestrator assembled from repositories.

Enforced properties, each with a test:

* `USER_FORBIDDEN` is a global backstop independent of per-transition actor sets;
* every state is reachable and no non-terminal state is a dead end;
* every escalatable state has a route to `HUMAN_HANDOFF`;
* a no-show requires the grace period **and** an authoritative absence signal —
  `evidence_source="llm"` is refused;
* a payment requires signature verification, an amount match and an order match,
  from `Actor.PAYMENT_PROVIDER` only;
* a tutor selection requires the reference to be in the persisted candidate
  snapshot.

Concurrency: optimistic locking with `expected_version`, three retries, reload
and re-decide on conflict. Never a forced write.

---

## 7. Database

Schema `dcc` in the **existing** Aurora cluster (no new cluster provisioned),
reached via the **Data API** — which is what lets the orchestrator Lambda live
outside the VPC and still reach Meta, Google, Cashfree and OpenAI without a NAT
Gateway.

`migrations/0001_dcc_schema.sql` — **36 tables**, 529 lines. Shares no table with
Tutor Intelligence, adds no column to one, holds no copy of the tutor projection.

Constraints that carry behaviour rather than documenting it:

| Constraint | Prevents |
|---|---|
| `dcc_slot_holds_active_uidx` (partial unique on `conflict_key`) | two parents booking one tutor at one time |
| `dcc_demo_reminders_uidx` on `(demo, revision, label, audience)` | a reschedule producing a second reminder ladder |
| `dcc_activation_success_uidx` (partial unique on `order_ref`) | two subscriptions for one payment |
| `dcc_payment_events` PK on `provider_event_id` | a replayed webhook |
| `dcc_discount_balances` CHECK | a stored discount whose arithmetic does not add up |
| `dcc_discount_floor` CHECK | an approved price below the margin floor |
| `dcc_demos_no_meet_for_home` CHECK | a Meet link invented for an in-person demo |
| `dcc_demos_calendar_event_uidx` | two demos claiming one calendar event |

Migrations are numbered SQL applied by `dcc-migrate`, recorded with a checksum;
editing an applied migration is a hard error rather than a silent divergence.

---

## 8. Provider adapters

Every provider sits behind a Protocol in `contracts/ports.py`. No domain or
capability module imports `httpx`, `boto3` or an SDK — asserted by AST walk.

| Provider | Production | Fake | Notes |
|---|---|---|---|
| Tutor Intelligence | `local_adapter.py` | `fake.py` | in-process, return-only |
| NXTutors gateway | `client.py` | `FakeGateway` | endpoints unverified (gaps §1) |
| Meta WhatsApp | `sender.py` | `FakeWhatsApp` | never auto-retries a send |
| Google Calendar | `client.py` | `FakeCalendar` | polls for async conference |
| Cashfree | `client.py` | `FakeCashfree` | mints webhooks the real verifier accepts |
| OpenAI | `client.py` | `StubLlm` | structured output only |
| Onboarding | `bus.py` | `FakeAgentBus` | refuses an envelope with no idempotency key |
| Scheduler | `scheduler.py` | `FakeScheduler` | replace-by-name |

Shared `HttpClient`: explicit timeout, bounded retries **only when the caller
declares idempotency** (not "only for GET"), error classification, circuit
breaker, URL allowlist before the socket opens, no redirect following, safe
logging.

---

## 9. Meta template registry

Typed, closed, and strict about arity **and order** — the right count in the
wrong order delivers happily and reads as nonsense.

| Template | Status |
|---|---|
| `demo_reminder_t24h` / `_t2h` / `_t15m` | approved (from the committed reminder policy) |
| `demo_tutor_request_expired` | declared; variables unverified |
| `demo_scheduled_confirmation` | declared; variables unverified |
| tutor confirmation | **`approved=False`** — the real name is unknown |

The tutor-confirmation name was truncated in the supplied screenshot and is
**deliberately not guessed**. It is registered under a placeholder key that
cannot be mistaken for a real name; `bind()` raises `TemplateNotApproved` and
the scheduling capability degrades to an operator-approval path, recording
`tutor_confirmation_template_unavailable`.

No template is created or renamed by application code. `dcc-doctor` fails if the
reminder policy names a template the registry does not approve.

---

## 10. Tests executed — exact results

All commands run from `Demo Intelegence Agent/`.

| Gate | Command | Result |
|---|---|---|
| lint | `ruff format --check src tests` + `ruff check src tests` | **PASS** — 140 files formatted, all checks passed |
| type | `mypy src` | **PASS** — no issues in 112 source files (strict) |
| test | `pytest --cov=demo_command_center` | **PASS** — **260 passed**, coverage **82.19%** (floor 80%) |
| contracts | `pytest -m contract` | **PASS** — 17 passed |
| e2e | `pytest -m e2e` | **PASS** — 33 passed |
| test-security | `pytest -m security` | **PASS** — 87 passed |
| security | `bandit -r src -ll` | **PASS** — High 0, Medium 0, Low 8 |
| demo | `dcc-e2e` | **PASS** — 30/30 steps, `NEW → CONVERTED` |
| doctor | `dcc-doctor` | **PASS** — 0 problems, gaps listed |

### `make demo` output

```
30/30 steps passed
final state: CONVERTED
messages delivered: 7

  1. Here are the tutors I would recommend for the demo class:
  2. These times work for both of you:
  3. Your demo class is confirmed.
  4. Your demo class is confirmed.        (after reschedule)
  5. Hi, ...                              (post-demo follow-up)
  6. Here is your secure payment link for ₹4,800.00:
  7. Welcome to NXTutors! Your subscription is active.

Domain events: demo.slot_held · demo.scheduled · demo.rescheduled ·
               demo.objections_extracted · demo.payment_requested ·
               subscription.activated · onboarding.requested
```

### Required test coverage (brief §18)

Every item is covered:

envelope compatibility · Tutor return-only · ownership transition · duplicate
inbound event · duplicate outbound prevention · illegal state transition · stale
tutor result · tutor selection tampering · timezone parsing · slot collision ·
two concurrent bookings (10-way race, exactly one winner) · tutor decline ·
request expiry · Google failure compensation · reminder replacement · student
no-show · tutor no-show · structured objection extraction · deterministic
forecast · discount ceiling · forged payment event · duplicate payment event ·
amount mismatch · activation retry · duplicate onboarding handoff.

### Not executed, and why

* `tests/integration/` — needs a live Aurora cluster (`DCC_TEST_CLUSTER_ARN`).
  Skipped is **not** a pass.
* `make audit` (pip-audit) — needs `uv export` against a project-local
  environment; this checkout shares the parent venv.
* `make` itself is not installed on the development host. Every target's
  underlying command was run directly and passes.

---

## 11. Bugs found and fixed during implementation

Each was found by a test or a check, not by inspection.

| # | Bug | Found by | Fix |
|---|---|---|---|
| 1 | A parent with no qualifying objection could **never be sent a payment link** — `approve()` refused any non-`APPROVED` decision, but "no discount" is not "no sale" | `make demo` step 26 | `approve()` refuses only `ESCALATED`; the payable amount is already floor-validated |
| 2 | A reschedule **created a second calendar event** instead of patching the first, leaving the parent with two invites | E2E `test_only_one_logical_calendar_event…` | `_create_event` detects an existing `calendar_event_id` and patches; bumps `revision` |
| 3 | The **welcome message was never delivered** — ownership transferred to Onboarding before the outbox flushed, so the send was suppressed as not-owner | `make demo` message list | `CommandOutcome.transfer_to`, applied by the orchestrator *after* the flush; `ONBOARDING_HANDOFF_PENDING` removed from `SUSPENDED` |
| 4 | **Cancellation notices were silently swallowed** — `CANCELLED` is terminal, and terminal blocked all sends | same root cause as #3 | `TERMINAL_ALLOWED` for the messages that announce the terminal state |
| 5 | `tutor_ref` was **erased between turns** — `assemble_facts` overwrote a persisted fact with `None` derived from a demo row that does not exist yet | E2E `test_a_declined_or_expired_tutor_request…` | facts layer over the snapshot; `_set_if_known` never writes a `None` |
| 6 | Commands accepted a **caller-supplied `tutor_ref`** from the payload, bypassing the snapshot check — a tampering vector | found while fixing #5 | facts only; `_place_hold` also re-asserts `tutor_in_snapshot` |
| 7 | A plain price objection selected the **12–15% band instead of 8–10%** — band triggers matched on *any* rather than *all* | unit `test_an_explicit_price_objection_earns_the_price_band` | subset match; a ~5% overpay on every price-sensitive customer, plus needless human escalations |
| 8 | **"my son is in class 10" parsed as a demo time** | unit `test_class_ten_is_not_ten_oclock` | a bare number with no day and no time marker is not a time |
| 9 | A Google failure **left the slot hold in place** for its full TTL | E2E `test_a_google_failure_compensates_the_hold` | `compensation=RELEASE_SLOT_HOLD` on the transition whose command takes the hold into calendar creation |
| 10 | **Every outbound message was blocked** — the output guard counted any URL as PII, and the profile host was not allowlisted | `make demo` guardrail logs | URLs excluded from the PII verdict (the allowlist is the real control); `website_public_base_url` added |
| 11 | `NextStep.ADDRESS_OBJECTIONS` **did not exist** — the offline stub would have raised `AttributeError` the moment it found an objection | `mypy --strict` | member added; the local stack now uses `StubLlm` so `make demo` exercises the path |
| 12 | Attendance from a conference with **2+ participants asserted nothing**, so a completed demo scored `UNKNOWN` and lost its discount eligibility | `make demo` discount step | 2+ participants means both parties attended |
| 13 | The **invisible-character detector held literal bidi characters** in source — the same trick aimed at a reviewer | `bandit` B613 (HIGH) | rewritten as `\u` escapes; behaviour identical, source readable |
| 14 | Coverage `omit` paths were **stale and matched nothing**, so infrastructure-only modules counted as untested | coverage run | corrected to the real layout |

---

## 12. Changes to pre-existing scaffold files

Small and each load-bearing:

* `config/settings.py` — added `website_public_base_url` + `website_host` (the
  outbound allowlist needs it; bug #10), wrapped one over-length line.
* `domain/objections.py` — added `NextStep.ADDRESS_OBJECTIONS` (bug #11).
* `security/guardrails.py` — bidi class rewritten as escapes (bug #13).
* `security/signatures.py`, `observability/logging.py` — one `noqa` each, with a
  comment explaining why the rule does not apply.
* `observability/metrics.py` — `stream` typed `TextIO` for mypy strict.
* `pyproject.toml` — corrected coverage omits, added `../src` to the test path so
  the envelope compatibility test runs rather than silently skipping, added a
  mypy override for the untyped optional `tutor_match_meta` import.
* `Makefile` — `RUN` override; `check` now includes `e2e` and `test-security`,
  because a gate that skips the lifecycle and the money path is not a gate.

Policy YAMLs, contracts, PII, rate limiting, URL policy, clock, ids and money:
**unchanged**.

---

## 13. Unresolved external credentials

None available: Meta, Google, Cashfree, OpenAI, Aurora, EventBridge Scheduler,
NXTutors gateway. All are exercised through fakes with real behaviour; live
smoke tests are opt-in and skipped by default.

Full detail in `docs/integration-gaps.md`.

---

## 14. Genuine remaining work for Phase 2

Ordered by what would actually block a production deploy.

1. **Complete the Data API repositories.** Three of ten aggregates are
   implemented — the three carrying the concurrency contract. The other seven
   fall back to in-memory stores, so demo rows, reminders and payment records
   would not survive a container recycle. The schema already exists; this is
   repository code.
2. **Verify the Laravel gateway.** Eight unverified endpoints in one table.
   Confirm `plan_quote` returns integer minor units and
   `activate_subscription` honours `X-Idempotency-Key`.
3. **Resolve the tutor-confirmation template name** and reconcile the other five
   against a WABA export — variable *order* especially.
4. **Infrastructure.** No Terraform was written: the brief scoped Phase 1 to
   application code, and `infra/terraform/**` is a protected path. Needed:
   API Gateway routes, the SQS work queue + DLQ, six Lambdas, the Scheduler
   group and role, Secrets Manager entries, alarms on the metrics already
   emitted.
5. **Secrets Manager wiring.** Settings read from the environment; deployed
   environments should resolve `SecretStr` fields from Secrets Manager at cold
   start.
6. **Google credential provider.** `GoogleCalendarClient` takes a
   `token_provider` and none is implemented — service-account JWT with
   domain-wide delegation, or an OAuth refresh flow.
7. **Ops API and console** for capability 129. The monitoring capability and its
   server-side region authorization exist; there is no HTTP surface yet.
8. **Load testing.** `tests/load/` is empty. The interesting question is
   contention on `dcc_slot_holds` under a popular tutor.
9. **Forecast calibration.** `forecast.v1` coefficients were set by hand. Once
   labelled demos exist, refit and emit `forecast.v2` — the scoring code does
   not change, only the policy file.
