# Demo Command Center Agent

One conversational agent that owns the whole demo-class lifecycle for NXTutors —
from a Lead Intake handoff through tutor selection, scheduling, reminders, the
demo itself, objection analysis, an offer, payment, and the handoff to
Onboarding.

* **Display name:** Demo Command Center Agent
* **Python package:** `demo_command_center`
* **Service identifier:** `demo_command_center_agent`
* **Directory:** `Demo Intelegence Agent/` (the misspelling is the existing
  repository path and is preserved deliberately)

---

## Quick start

```bash
# From `Demo Intelegence Agent/`
make install
make demo      # the full lifecycle against in-memory adapters — no credentials
make doctor    # what is coherent, and what is unconfigured
make check     # lint + type + test + contracts + e2e + security
```

`make demo` runs a real conversation from handoff to converted with no AWS, no
OpenAI, no Google, no Cashfree and no Meta. It exercises the actual orchestrator,
state machine and outbound boundary — only the provider edges are doubled.

---

## The one idea

**One conversation owner, one state machine, one send path, eight capabilities.**

```
Meta webhook  ──►  SQS  ──►  DemoCommandCenterOrchestrator
(ack only,                    │
 no LLM)                      ├─ ownership guard      only the owner may speak
                              ├─ deterministic FSM    30 states, table-driven
                              ├─ capability dispatch  the eight modules below
                              ├─ LLM proposal ─► schema ─► state ─► authz
                              │                 ─► policy ─► execution
                              └─ transactional outbox
                                     │
                                     └─►  ONE outbound boundary
                                          (ownership · state · opt-out ·
                                           idempotency · session window ·
                                           guardrail · rate limit)
                                              │
                                              └─►  Meta Cloud API
```

The eight capabilities are **modules, not agents**. They return `OutboundMessage`
values; they cannot send. That is what makes eight capability Lambdas safe: a
worker that finishes late cannot talk over a human who took the conversation,
because ownership is checked at *send* time, not at *decide* time.

| ID | Capability | Module |
|---|---|---|
| 129 | Regional demo monitoring | `capabilities/monitoring/` |
| 018 | Demo success forecast | `capabilities/forecasting/` |
| 025 | Demo scheduling | `capabilities/scheduling/` |
| 026 | Demo reminders | `capabilities/reminders/` |
| 031 | Objection extraction | `capabilities/objection_extraction/` |
| 032 | Post-demo conversion | `capabilities/conversion/` |
| 034 | Discount suggestion | `capabilities/discounts/` |
| 036 | Demo-to-paid transition | `capabilities/paid_transition/` |

---

## What the LLM may and may not do

It may classify intent, extract requirements, interpret a time phrase, extract
objections and word a message.

It may **not** decide tutor eligibility, availability, a state transition, a
price, a discount, a payment, an authorization, a no-show, regional access, a
recipient or a Meet URL.

That is enforced by the type graph, not by a prompt:

* A `PaymentOrder` can only be built from an `ApprovedOffer`, which can only come
  from `DiscountDecision.approve()`. There is no `amount` parameter anywhere on
  that path.
* A tutor reference is **looked up** in the snapshot we persisted when we
  presented the options. `guardrails/tutor_selection.resolve()` maps an ordinal
  or a name onto a stored candidate; a reference appearing in a payload is
  ignored.
* `Trigger.PAYMENT_PAID` is authorised only for `Actor.PAYMENT_PROVIDER`, and
  its guard requires signature verification, an amount match and an order match.
* Every objection quote is checked against the actual transcript. Enough
  fabricated citations and the whole analysis is discarded.

---

## Integration with the Tutor Intelligence Agent

Demo calls `TutorMatchOrchestrator.match()` **in process**. That method is a pure
function of its inputs: no sender, no state write, no outbox. It is the
return-only boundary the design requires, and it needed no change to the
protected service.

Demo deliberately does **not** call `/internal/v1/handoff` or `TurnService` —
those own Tutor's own conversation FSM and can return `reply_text`, which would
make Tutor a second owner of one WhatsApp thread.

`tutor_match_meta` is an optional runtime dependency. `local_adapter.available()`
guards the import and the composition root falls back to the deterministic fake,
so Demo deploys and tests standalone.

---

## Data

Demo owns schema **`dcc`** in the existing Aurora cluster, reached through the
**Data API** — which is why the orchestrator Lambda can live outside the VPC and
still call Meta, Google, Cashfree and OpenAI without a NAT Gateway.

It shares no table with Tutor Intelligence, adds no column to one, and holds no
copy of the tutor projection. A tutor reference here is an opaque handle
resolved through the website gateway at the moment it is needed.

Migrations are numbered SQL under `migrations/`, applied by `dcc-migrate` and
recorded with a checksum — editing an applied migration is a hard error.

---

## Layout

```
src/demo_command_center/
├── state/            30-state machine: states, triggers, transition table
├── orchestration/    the one owner: orchestrator, commands, composer, outbound
├── capabilities/     the eight modules
├── domain/           demo, slots, reminders, objections, pricing, payments
├── contracts/        envelope, ownership, ports, tutor_match, events
├── guardrails/       output guard, tutor-selection integrity
├── integrations/     tutor_intelligence, gateway, meta, google, cashfree, openai
├── storage/          memory/ and data_api/ repositories, scheduler
├── security/         signatures, pii, rate limits, url allowlist, input guards
├── resilience/       http client, circuit breaker
├── handlers/         Lambda entry points (webhooks, workers)
└── cli/              doctor, local_e2e, migrate
```

---

## Commands

| Command | What it does |
|---|---|
| `make install` | `uv sync --all-extras` |
| `make format` | ruff format + autofix |
| `make lint` | ruff format check + lint |
| `make type` | mypy strict |
| `make test` | pytest with coverage (80% floor) |
| `make contracts` | contract tests, incl. envelope compatibility with Tutor |
| `make e2e` | the full lifecycle plus concurrency and failure paths |
| `make test-security` | signatures, guardrails, boundaries, the money path |
| `make security` | bandit + ruff security rules |
| `make audit` | pip-audit against the locked dependencies |
| `make check` | the gate: all of the above that matter |
| `make demo` | the lifecycle, printed step by step |
| `make doctor` | build coherence and configuration gaps |
| `make migrate` | apply (or `--dry-run`) the SQL migrations |

`RUN` is overridable when the project shares the repository venv:

```bash
make check RUN='../.venv/Scripts/python.exe -m'
```

---

## Configuration

Everything environment-specific is a `DCC_`-prefixed setting
(`config/settings.py`); nothing is hardcoded. Business numbers — reminder
offsets, discount bands, forecast weights, alert thresholds — are **not**
settings: they live in checksummed YAML under `config/policies/`, and the
checksum is stamped onto every decision they produced. "Why did this customer
get 15% off?" is answerable six months later from the stored stamp rather than
from today's source.

Copy `.env.example` to `.env` to start. With nothing configured, the service
runs entirely on fakes and `dcc-doctor` lists what is missing.

---

## Known gaps

See [`docs/integration-gaps.md`](docs/integration-gaps.md). The short version:
the Laravel gateway endpoints are unverified, the tutor-confirmation template
name is unknown and deliberately not guessed, and seven of ten aggregates still
use in-memory repositories under `persistence_mode=data_api`.
