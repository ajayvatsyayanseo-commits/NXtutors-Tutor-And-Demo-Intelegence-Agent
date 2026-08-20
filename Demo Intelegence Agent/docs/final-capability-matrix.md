# Final capability matrix

Sixteen agents from the NXTutors register, fused into one product. This maps
each to the code that implements it and the test that proves it.

**Date:** 2026-08-17

Status values used below:

| Status | Meaning |
| --- | --- |
| `VERIFIED LOCALLY` | Exercised end to end through the real orchestrator, with provider edges doubled |
| `VERIFIED WITH LIVE PROVIDER` | Exercised against the real external service |
| `IMPLEMENTED — LIVE CREDENTIAL REQUIRED` | Code complete and unit-tested; the external call has never run |
| `BLOCKED BY BUSINESS CONFIGURATION` | Cannot proceed until a human supplies a value |

---

## Part A — Tutor Intelligence: the eight matching skills

Package `tutor_match_meta`, schema `tutor_match`. **Protected and unmodified**
throughout this work — see `checkpoints/final-protected-path-verification.md`.

| # | Agent | Dimension | Module | Status |
| --- | --- | --- | --- | --- |
| 013 | Tutor Availability | `AVAILABILITY` | `matching/availability.py` | `VERIFIED LOCALLY` |
| 014 | Tutor Subject Expertise | `SUBJECT_EXPERTISE` | `matching/subject_expertise.py` | `VERIFIED LOCALLY` |
| 015 | Tutor Past Performance Score | `PERFORMANCE` | `matching/performance.py` | `VERIFIED LOCALLY` |
| 016 | Tutor Personality Compatibility | `PERSONALITY` | `matching/personality.py` | `VERIFIED LOCALLY` |
| 017 | Tutor Academic Compatibility | `ACADEMIC` | `matching/academic.py` | `VERIFIED LOCALLY` |
| 019 | Tutor Proximity | `PROXIMITY` | `matching/proximity.py` | `IMPLEMENTED — LIVE CREDENTIAL REQUIRED` (geocoding) |
| 021 | Tutor Negotiation Profile | `NEGOTIATION` | `matching/negotiation.py` | `VERIFIED LOCALLY` |
| 022 | Tutor Replacement Risk | `REPLACEMENT_RISK` | `matching/replacement_risk.py` | `VERIFIED LOCALLY` |

All eight are registered in `matching/__init__.py::default_evaluators()`, which
is the single place the orchestrator learns which skills exist. Each is pure,
total and independently testable against the `SkillEvaluator` contract in
`matching/base.py`.

Constructed fresh per call rather than as a module singleton — deliberately, so
that a future cache added to a warm Lambda cannot leak one family's data into
the next conversation.

### What the Tutor half will not do

- Send a message. It has no sender, no outbox and no conversation store.
- Own a conversation. A match request is a question, not a handoff.
- Invent a tutor fact. Every dimension carries `evidence` and a `data_quality`
  flag; the explanation layer cannot cite a dimension whose data is missing,
  stale, or below the policy's minimum sample size.

---

## Part B — Demo Command Center: the eight lifecycle capabilities

Package `demo_command_center`, schema `demo_agent`. One Lambda function each.

| # | Agent | Module | Lambda handler | Status |
| --- | --- | --- | --- | --- |
| 025 | Demo Scheduling | `capabilities/scheduling/` | `demo_scheduling_worker` | `IMPLEMENTED — LIVE CREDENTIAL REQUIRED` (Google) |
| 026 | Demo Reminder | `capabilities/reminders/` | `demo_reminder_worker` | `IMPLEMENTED — LIVE CREDENTIAL REQUIRED` (Scheduler + Meta) |
| 031 | Demo Objection Extraction | `capabilities/objection_extraction/` | `demo_objection_worker` | `VERIFIED LOCALLY` (heuristic path) |
| 032 | Post-Demo Conversion | `capabilities/conversion/` | `demo_conversion_worker` | `VERIFIED LOCALLY` |
| 034 | Discount Suggestion | `capabilities/discounts/` | `demo_discount_worker` | `VERIFIED LOCALLY` |
| 036 | Demo-to-Paid Transition | `capabilities/paid_transition/` | `demo_paid_transition_worker` | `IMPLEMENTED — LIVE CREDENTIAL REQUIRED` (Cashfree) |
| 018 | Demo Success Forecast | `capabilities/forecasting/` | `demo_forecast_worker` | `VERIFIED LOCALLY` |
| 129 | Demo Monitoring Regional | `capabilities/monitoring/` | `demo_monitoring_worker` | `VERIFIED LOCALLY` |

Each worker is `_batch(Capability.X, _handler)` — a partial-batch-failure
wrapper returning `batchItemFailures`, so one poisoned SQS message does not
re-drive the other nine in its batch.

### 025 — Demo Scheduling

Negotiates a time, holds the slot, creates one calendar event with a Meet link,
confirms, and handles reschedules.

- **Slot exclusion is a database constraint**, not application logic: a partial
  unique index on `(tutor, minute)`. Fifty concurrent attempts on one slot yield
  exactly one winner and 49 clean `SlotConflict` refusals — asserted by
  `tests/load/test_profiles.py::test_slot_contention_degrades_rather_than_collapses`
  and again live against the real database by `scripts/verify_live_wiring.py`.
- **A reschedule patches the existing event.** It does not create a second one.
  This was a real bug: the tutor's calendar accumulated a duplicate event per
  reschedule, and the parent received the stale link.
- Compensation on failure releases the hold. Also a real bug: a Google failure
  after a successful hold left the slot locked forever with no demo attached.

### 026 — Demo Reminder

Times reminders, picks a channel, scores no-show risk, escalates on silence,
throttles.

Reminder timing is EventBridge Scheduler, not a polling loop — a polling worker
would be an always-running resource, which is prohibited. Without
`DCC_SCHEDULER_*` configured, reminders will not fire on time; `make doctor`
reports this as a gap rather than letting it pass silently.

### 031 — Demo Objection Extraction

The only capability with a genuine LLM dependency. Traces arguments, separates
explicit from implicit objections, finds root cause, summarises.

With `DCC_LLM_PROVIDER` unset it runs the offline stub and the extraction is
heuristic. That is honest degradation, reported by `make doctor` — not a silent
fallback that looks like a working model.

The model's output is a **proposal**. It never sets state, never chooses a
discount and never sends a message.

### 032 — Post-Demo Conversion

Personalises the pitch, frames urgency, summarises benefits, adds social proof,
drafts the close.

Every claim in the draft must trace to a stored fact. The output guard rejects a
message citing a tutor attribute that is not in the persisted candidate
snapshot — which is what stops a fluent model inventing a credential.

### 034 — Discount Suggestion

Analyses margin, picks a band, sets conditions, prevents abuse.

Deterministic band engine with a hard price floor. **A band requires ALL of its
triggers, not any one.** That was a real bug and an expensive one: `any` matching
granted the top band to roughly every price-sensitive customer, costing about 5%
on each.

Discount tools are **not model-facing**. The model may observe that a parent
raised price; it cannot propose a number.

### 036 — Demo-to-Paid Transition

Migrates the record, creates the onboarding flow, switches status, prepares the
payment link, drafts the welcome note.

- Money is integer paise (`Money`), never a float.
- Payment success is an HMAC over raw bytes **plus** exact amount
  reconciliation. A valid signature on the wrong amount is not a payment.
- `approve()` refuses only from `ESCALATED`. It previously refused when no
  discount band matched, which meant a full-price customer — the most valuable
  kind — could never be sent a payment link.

### 018 — Demo Success Forecast

Mines past demos, weights features, estimates conversion, proposes strategy,
scores risk.

Advisory only. A forecast never gates a booking or a price. Drift is evaluated
with PSI and `AUTO_APPLY_ENABLED = False`: a drifted model raises an alarm and
waits for a human.

### 129 — Demo Monitoring Regional

Regional demo calendar, no-show tracking, conversion and quality comparison,
underperformance alerts.

Aggregates only. Read paths are region-scoped in the application layer; that
scoping is **not** enforced by IAM, which is stated plainly in `iam.tf` rather
than implied to be stronger than it is.

---

## Part C — The eight cross-cutting mechanisms

Not register agents. These are what make the sixteen safe to run together.

| Mechanism | Module | What it prevents |
| --- | --- | --- |
| Tool registry | `orchestration/tools.py` | A model reaching a financial or booking tool. 12 tools, **5 model-facing**. |
| Authorisation pipeline | `orchestration/authorisation.py` | A valid-looking action in the wrong state, by the wrong actor. 9 stages. |
| Output guard | `guardrails/` | A fabricated tutor fact or an unapproved link leaving the system. |
| Outbox | `storage/*/outbox` | A message sent inside the transaction that decided to send it. |
| Sagas | `glue/saga.py` | A half-completed booking or payment with no compensation. |
| Circuit breakers | `resilience/` | A dead provider being retried into a bill, or its answer being fabricated. |
| Budget ceilings | `cost_control/budget.py` | An LLM loop turning into an unbounded spend. 4 ceilings. |
| Human handoff | `human_handoff/escalation.py` | An agent deciding something a human must decide. |

### The 9-stage authorisation pipeline

Every proposed action passes all nine, in order. Failing any one stops it:

1. Schema — is the proposal even well-formed?
2. Ownership — do we own this conversation right now?
3. State — is this legal from the current state?
4. Authorization — may this actor fire this trigger?
5. Policy — does the versioned policy permit it?
6. Rate limit — per conversation, per provider, per tenant.
7. Idempotency — have we already done exactly this?
8. Single-flight — is an `EXCLUSIVE` tool already running for this conversation?
9. Result validation — is what came back the shape we promised?

Stages 7 and 8 are the two that matter under retry. SQS is at-least-once, so
every handler runs more than once eventually; without both, a duplicate delivery
becomes a duplicate charge.

---

## Part D — Composition proof

The claim "sixteen agents, all reachable" is asserted mechanically, not by this
document:

| Assertion | Test |
| --- | --- |
| All 8 Tutor skills registered, weighted, reachable | `tests/contract/test_composed_agent_coverage.py` |
| All 8 Demo capabilities routed and lane-assigned | `tests/security/test_hardening.py` |
| Tutor returns candidates and sends nothing | `Demo Intelegence Agent/tests/security/` |
| Envelope compatibility across the boundary | `tests/contract/` (Demo), pinned both ways |
| No financial tool is model-facing | `tools.py::FORBIDDEN_TOOL_NAMES` + registry test |

Current results: Demo **389 passed** (contract 17, security 212, e2e 33, load 4)
at 82.73% coverage; Tutor **772 passed, 18 skipped, 1 xfailed**. Full detail and
the honest accounting of the skips is in `final-e2e-report.md`.

---

## Part E — Blocked

| Item | Status | Why |
| --- | --- | --- |
| Tutor confirmation WhatsApp template | `BLOCKED BY BUSINESS CONFIGURATION` | The approved template name was truncated in the supplied screenshot. It is deliberately **not guessed** — a wrong template name is a rejected send at the provider, in production, on the first real booking. Set `DCC_TEMPLATE_TUTOR_CONFIRMATION` once confirmed; `make doctor` lists it until then. |
