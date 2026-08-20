# Integration gaps

Everything this build could not verify against a real system, and what stands in
for it. Each entry states the risk in the terms that matter: what breaks, when
you would find out, and what would close it.

Nothing here is a code defect. These are external dependencies that were not
reachable from this workspace.

---

## 1. NXTutors Laravel gateway — **every endpoint unverified**

**Status:** blocking for a production deploy. Not blocking for Phase 1.

`E:\NX Tutor\Nxtutors Website` is outside this workspace and access was declined
by the sandbox, so `packages/nxtutors/demo-command-center-adapter` could not be
read. Paths, verbs, request shapes and response field names in
`integrations/nxtutors_gateway/client.py` are **inferred from the journey**, not
from the Laravel package.

| Operation | Assumed route | Used by |
|---|---|---|
| `resolve_identity` | `POST /api/agent/v1/identity/resolve` | intake |
| `resolve_tutor_contacts` | `GET /api/agent/v1/tutors/{ref}/contacts` | calendar invite |
| `tutor_availability` | `GET /api/agent/v1/tutors/{ref}/availability` | scheduling |
| `plan_quote` | `GET /api/agent/v1/plans/quote` | discounts, payment |
| `discount_eligibility` | `GET /api/agent/v1/customers/discount-eligibility` | discounts |
| `record_demo` | `POST /api/agent/v1/demos` | scheduling |
| `activate_subscription` | `POST /api/agent/v1/subscriptions/activate` | paid transition |
| `region_authorization` | `GET /api/agent/v1/operators/{ref}/regions` | monitoring |

**Contained deliberately.** All eight live in one `_ENDPOINTS` table at the top
of the client, so correcting them is a single-place edit rather than a search.
The domain never sees a gateway shape — everything is validated into a Demo
Pydantic model at the boundary.

**When you would find out:** immediately, at the first live call. Every one
raises `ProviderRejected`/`ProviderUnavailable` rather than returning something
plausible, and the orchestrator degrades rather than inventing data.

**To close:** read the Laravel package, correct `_ENDPOINTS` and the response
field names, and run the gateway contract tests against a staging instance.

### Two things worth confirming when it becomes readable

* `plan_quote` **must** return `list_price_minor` as an integer. The client
  refuses to derive paise from a float price — `4799.99 * 100` is
  `479998.99999…`, which truncates a paise short and makes the payment
  reconciler reject a perfectly good payment as an amount mismatch.
* `activate_subscription` **must** honour `X-Idempotency-Key`. Retry safety on
  the money path depends on it; without it a timeout-then-retry creates a second
  subscription for one payment. `tests/security/test_payment_path.py` asserts we
  present one stable key per order, which is our half of the contract.

---

## 2. Meta WhatsApp templates

### 2a. The tutor-confirmation template — **name unknown, deliberately not guessed**

The brief states the approved name was truncated in the supplied screenshot.
Guessing it would produce a send that Meta rejects silently, hours later, with a
tutor who was never asked and a parent whose demo quietly never confirms.

It is therefore declared in the registry with `approved=False` under the
placeholder key `__unconfirmed_tutor_confirmation__` — chosen so it cannot be
mistaken for a real template name in a log line. `registry().bind()` raises
`TemplateNotApproved`, and `composer.tutor_confirmation_template()` returns
`None`, which the scheduling capability treats as a recoverable degradation:
the confirmation request is skipped and recorded as
`tutor_confirmation_template_unavailable`.

**To close:** put the real name in `TEMPLATE_TUTOR_CONFIRMATION`, set
`approved=True`, and list the languages it is approved in.

### 2b. Two templates named in the brief but not verified against a WABA export

`demo_tutor_request_expired` and `demo_scheduled_confirmation` are declared as
approved on the strength of the brief alone. Their **variable count and order**
are assumptions — and order is the dangerous half, because the right count in
the wrong order delivers happily and reads as nonsense (the tutor's name where
the date should be).

| Template | Assumed variables, in order |
|---|---|
| `demo_tutor_request_expired` | `student_name`, `demo_datetime` |
| `demo_scheduled_confirmation` | `student_name`, `demo_datetime`, `tutor_name`, `join_link` |

The three reminder templates (`demo_reminder_t24h`, `_t2h`, `_t15m`) come from
`config/policies/reminder.v1.yaml`, which was already committed and describes
them as approved in the live account. Their variable lists carry the same
caveat.

**To close:** export the approved templates from Business Manager and reconcile
`integrations/meta_whatsapp/templates.py` against it. `dcc-doctor` already
fails if a reminder policy names a template the registry does not approve.

---

## 3. Onboarding agent contract — inferred

The onboarding agent's repository was not readable. The handoff is modelled on
`AgentEnvelopeV1` and dispatched to `DCC_ONBOARDING_WEBHOOK_URL` with our HMAC
scheme and an `X-Idempotency-Key`.

`HttpAgentBus.dispatch` **refuses to send an envelope with no idempotency key**
rather than sending one that cannot be retried safely — if the receiver cannot
dedupe, a retry creates two onboarding records for one customer.

**To close:** confirm the receiving route, the expected envelope shape and that
it dedupes on the header.

---

## 4. Lead Intake — shape borrowed, endpoint unverified

Lead Intake's POST shape is *not* guessed: it is pinned in
`tutor_match_meta/contracts/handoff.py`, which was read directly. Demo's
`handlers/workers.internal_handoff` accepts the same fields.

The difference from Tutor Intelligence is deliberate. Tutor runs
`caller_sends`, so it returns `reply_text` for Lead Intake to deliver. Demo runs
`self_sends` and therefore returns `reply_text: null` — returning text *and*
sending it ourselves is the double send the whole ownership model exists to
prevent.

**To close:** confirm Lead Intake tolerates a null `reply_text` on an
`accepted` status, and configure `TUTOR_DEMO_AGENT_WEBHOOK_URL` on its side.

---

## 5. No live credentials for any provider

| Provider | State | Exercised by |
|---|---|---|
| Meta WhatsApp | no token | `FakeWhatsApp` + real signature verification tests |
| Google Calendar | no service account | `FakeCalendar`, incl. double-booking and missing-conference paths |
| Cashfree | no merchant account | `FakeCashfree` mints webhooks the **real** verifier accepts |
| OpenAI | no key | `StubLlm` — heuristic, schema-valid, offline |
| Aurora Data API | no cluster | in-memory repositories with the same concurrency contract |
| EventBridge Scheduler | no role ARN | `FakeScheduler` |

The fakes are test doubles with real behaviour, not stubs that return success.
`FakeCalendar` enforces its own double-booking rule; `FakeCashfree` signs with
the production algorithm so `tests/security/test_payment_path.py` drives real
verification code; the in-memory slot repository models the unique index, so the
ten-way concurrency test is meaningful.

**What is genuinely untested:** the wire format of each provider — exact JSON
field names, error-code strings, auth header details. Everything above that
layer is covered.

Live smoke tests are opt-in and skipped by default (`tests/integration/`,
gated on `DCC_TEST_CLUSTER_ARN`).

---

## 6. Data API repositories: three of ten aggregates

`storage/data_api/repositories.py` implements `ConversationRepository`,
`IdempotencyRepository` and `SlotRepository` — the three that carry the
concurrency contract (optimistic locking, single-winner claims, the slot
exclusion index).

The remaining seven (`demos`, `reminders`, `outbox`, `messages`, `analysis`,
`commerce`, `operations`) fall back to their **in-memory implementations** when
`persistence_mode=data_api`.

This is deliberate and it is visible: they fall back to a working in-memory
store rather than to a stub that silently drops writes, `build_data_api_stores`
says so in a comment, and `dcc-doctor` reports it. The migration
(`migrations/0001_dcc_schema.sql`) already defines all their tables, so
completing them is repository code against an existing schema.

**Consequence if deployed as-is:** demo rows, reminders and payment records
would not survive a container recycle. That is a Phase 2 blocker for
production, not for the lifecycle logic this phase delivers.

---

## 7. Environment note: `make` is unavailable on the development host

Every Makefile target's underlying command was executed directly and passes.
`make` itself is not installed on this Windows machine, so the targets were not
invoked through `make`. The Makefile takes `RUN` as an override so the same
commands work against either `uv` or the shared repository venv.
