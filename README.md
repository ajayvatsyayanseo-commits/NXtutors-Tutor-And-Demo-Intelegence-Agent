# NXTutors Tutor and Demo Intelligence Agent

**One agent, two halves, sixteen source agents from the NXTutors register.**

It takes a parent's WhatsApp message and carries it the whole way: from "I need
a maths tutor" to a ranked shortlist of real tutors, a booked demo class with a
Google Meet link, reminders, the demo itself, objection analysis, a policy-bound
offer, a verified payment, and the handoff to onboarding.

The two halves are separate deployables that behave as one product. They share
one `.env`, one database, one correlation id per conversation, and one rule
about who is allowed to speak.

| Half | Package | Directory | Owns |
| --- | --- | --- | --- |
| **Tutor Intelligence** | `tutor_match_meta` | `src/` | Ranking tutors against a requirement, with evidence |
| **Demo Command Center** | `demo_command_center` | `Demo Intelegence Agent/` | The demo lifecycle: scheduling → demo → conversion → payment |

---

## The sixteen agents it fuses

### Tutor Intelligence — eight matching skills

| # | Source agent | Skills it contributes |
| --- | --- | --- |
| **013** | Tutor Availability | Parse schedules · Find free slots · Handle time zones · Check overlap · Suggest slots |
| **014** | Tutor Subject Expertise | Map subjects · Align syllabus · Tag depth level · Score expertise · Flag mismatches |
| **015** | Tutor Past Performance Score | Aggregate reviews · Normalise ratings · Compute retention · Score outcomes · Flag risk |
| **016** | Tutor Personality Compatibility | Profile communication · Map temperament · Score compatibility · Predict conflict · Suggest pairing |
| **017** | Tutor Academic Compatibility | Match board · Match class · Match exam · Map topic coverage · Rate academic fit |
| **019** | Tutor Proximity | Geocode address · Compute distance · Estimate travel time · Validate radius · Cluster locality |
| **021** | Tutor Negotiation Profile | Analyse fee history · Score flexibility · Model minimum fee · Detect overload risk · Suggest negotiation style |
| **022** | Tutor Replacement Risk | Detect churn signals · Score dissatisfaction · Analyse conflict pattern · Raise early alerts · Suggest backup tutor |

### Demo Command Center — eight lifecycle capabilities

| # | Source agent | Skills it contributes |
| --- | --- | --- |
| **025** | Demo Scheduling | Negotiate time · Coordinate calendars · Optimise window · Send confirmations · Handle reschedules |
| **026** | Demo Reminder | Time reminders · Use channels · Score no-show risk · Escalate silence · Throttle notifications |
| **031** | Demo Objection Extraction | Trace arguments · Detect explicit objection · Detect implicit objection · Find root cause · Summarise issues |
| **032** | Post-Demo Conversion | Personalise pitch · Frame urgency · Summarise benefits · Add social proof · Draft closing message |
| **034** | Discount Suggestion | Analyse margin · Suggest discount band · Pick price points · Define conditions · Prevent abuse |
| **036** | Demo-to-Paid Transition | Define migration · Create onboarding flow · Switch status · Prepare payment link · Draft welcome note |
| **018** | Demo Success Forecast | Mine past demos · Weight features · Estimate conversion · Propose strategy · Score risk |
| **129** | Demo Monitoring Regional | View regional demo calendar · Track no-shows · Compare conversion · Compare quality · Alert underperformance |

That the composition is real — every agent registered, weighted, reachable and
producing grounded evidence — is asserted by
[`tests/contract/test_composed_agent_coverage.py`](tests/contract/test_composed_agent_coverage.py)
and `Demo Intelegence Agent/tests/security/test_hardening.py`, not merely
claimed here.

---

## The two rules

**1. Nothing is ever invented.** Not a tutor's name, fee, rating, distance,
availability or profile URL. Not a Meet link, a price, a discount or a payment
status. Every scored dimension carries `evidence` and a `data_quality` flag, and
the explanation layer is mechanically prevented from citing a dimension whose
data is missing, stale or below the policy's minimum sample size.

**2. Exactly one agent owns a conversation at a time.** Demo owns it for the
whole demo lifecycle. Tutor Intelligence is a *bounded matching dependency* — it
returns ranked candidates and **sends nothing**. That is enforced structurally,
not by convention: Demo calls `TutorMatchOrchestrator.match()`, which has no
sender, no outbox and no conversation store.

---

## How the two halves connect

```
Lead Intake Agent
      │  signed handoff  (ownership transfers)
      ▼
Demo Command Center Agent          ← owns the conversation from here on
      │  capability call  (return-only; ownership does NOT move)
      ▼
Tutor Intelligence Agent
      │  ranked, evidence-backed candidates. Sends nothing.
      ▼
Demo Command Center Agent
      │  scheduling → demo → objections → forecast → offer → payment
      ▼
Onboarding Agent                   ← ownership transfers, exactly once
```

Demo never queries a Tutor table, never writes Tutor state, and Tutor never
writes Demo state. The only coupling is one in-process function call and a
shared envelope format whose compatibility is pinned by a contract test.

---

## One database, two schemas

Both halves use the **same PostgreSQL database**, reached through the **same
`.env`**, and each owns a schema inside it:

| Schema | Tables | Owner |
| --- | --- | --- |
| `tutor_match` | 22 | Tutor Intelligence |
| `demo_agent` | 37 | Demo Command Center |

They share no table. `DCC_POSTGRES_DSN` is deliberately blank in `.env` so Demo
inherits `TMM_POSTGRES_DSN` — the connection string exists in exactly one place
and the two can never drift apart.

Verified live, not assumed:

```bash
cd "Demo Intelegence Agent"
python scripts/verify_live_wiring.py
```

That script round-trips a real state transition, proves optimistic locking
rejects a stale write, proves the slot-hold index rejects a double-booking, and
asserts Tutor's schema is untouched throughout.

PostgreSQL is the only datastore on both sides. No Redis, no MySQL, no DynamoDB,
no container runtime — no ECS, Fargate, EC2 or Kubernetes. Those absences are
asserted by `tests/security/test_network_boundary.py` and
`Demo Intelegence Agent/scripts/scan_prohibited.py`, not assumed.

---

## Quick start (no AWS, no credentials, no Docker)

```bash
# Tutor Intelligence
uv sync --all-extras
make check                     # lint + types + tests + contract tests
make e2e                       # a full local conversation

# Demo Command Center
cd "Demo Intelegence Agent"
make check                     # lint + type + test + contracts + e2e + security
make demo                      # the whole demo lifecycle, step by step
make sync                      # BOTH agents as one product, end to end
make doctor                    # what is coherent, and what is unconfigured
```

`make demo` runs a real conversation from Lead Intake handoff to converted
customer — 30 steps, through the real orchestrator, state machine and outbound
boundary, against a fake Tutor agent.

**`make sync` is the one that proves the two halves compose.** It swaps the fake
for the real `tutor_match_meta` orchestrator running in the same process, so the
requirement translation, policy selection, evidence and candidate snapshot all
cross the boundary for real. It then asserts the sync invariants and prints the
conversation the parent would have received:

```
30/30 steps passed          final state: CONVERTED

TUTOR INTELLIGENCE — what the matching half decided
  policy       : board_exam_prep@1
  #1 Arjun Desai   [NXT10006]  score=0.827
  #2 Anita Sharma  [NXT10001]  score=0.821
  #3 Rohit Bansal  [NXT10009]  score=0.618   fee: — not substantiated, so never quoted

SYNC INVARIANTS
  [PASS] Tutor Intelligence reports it sent nothing
  [PASS] the Tutor adapter holds no sender and no outbox
  [PASS] every candidate presented came from Tutor Intelligence  (3 shown, all in the 3 returned)
  [PASS] every call was made in return-only mode

SYNC OK — both agents ran as one product, end to end.
```

It **refuses to fall back to the fake**. A silent fallback would print thirty
green steps while proving nothing about the integration the command exists to
test.

---

## Layout

```
NXTutors Tutor and Demo Intelligence Agent/
├── .env                        ONE env file. Both halves read it.
├── src/tutor_match_meta/       ── the Tutor half ──
│   ├── contracts/              versioned typed shapes
│   ├── matching/               the eight bounded evaluators + hard filters
│   ├── scoring/                versioned policy loading and rank combination
│   ├── orchestration/          staged pipeline, evidence guard, explanation
│   ├── state/                  deterministic FSM with optimistic locking
│   ├── repositories/           PostgreSQL persistence + tutor projection
│   ├── integrations/           website feed, chitragupta, whatsapp, openai, geo
│   ├── rag/ cache/ security/ observability/ config/ handlers/ analytics/
│   └── (schema: tutor_match)
├── config/policies/            the versioned ScoringPolicy documents
├── migrations/                 Alembic — Tutor schema
├── infra/terraform/            Tutor infra: Lambda, SQS FIFO, VPC endpoints, S3, IAM
├── tests/                      unit · contract · integration · security · e2e · load
│
└── Demo Intelegence Agent/     ── the Demo half ──
    ├── src/demo_command_center/
    │   ├── state/              30-state machine: states, triggers, transition table
    │   ├── orchestration/      the one conversation owner + the one send path
    │   ├── capabilities/       the eight lifecycle modules
    │   ├── domain/             demo, slots, reminders, objections, pricing, payments
    │   ├── contracts/          envelope, ownership, ports, tutor_match
    │   ├── guardrails/         output guard, tutor-selection integrity
    │   ├── integrations/       tutor_intelligence, gateway, meta, google, cashfree
    │   ├── storage/            memory/ · postgres/ · data_api/ repositories
    │   ├── glue/ memory/ cache/ cost_control/ human_handoff/ resilience/
    │   └── (schema: demo_agent)
    ├── migrations/             numbered SQL — Demo schema, 36 tables
    ├── infra/terraform/        Demo infra: 13 Lambdas, 5 queue lanes, IAM, alarms
    │                           (no S3, no VPC, no NAT Gateway)
    └── docs/                   architecture, security, operations, release gate
```

---

## Where the safety actually lives

Neither half trusts a language model with authority. The model reads text and
proposes; every consequential decision is a table lookup, a database constraint
or a signature check:

| Decision | What decides it |
| --- | --- |
| Which tutors are eligible | Deterministic hard filters. A model cannot override one. |
| The ranking | A versioned `ScoringPolicy` in YAML, checksummed onto the decision |
| Which tutor was chosen | Looked up in the persisted candidate snapshot by ordinal |
| Whether a slot is free | A partial unique index on `(tutor, minute)` |
| The state of a conversation | A 30-state transition table with actor authorization |
| The discount | A deterministic band engine with a price floor |
| Whether payment succeeded | An HMAC over raw bytes, plus exact amount reconciliation |
| Whether someone attended | Calendar RSVP or conference participation, never inference |

---

## Status — read before deploying

| Document | What it tells you |
| --- | --- |
| [`Demo Intelegence Agent/docs/final-release-gate-report.md`](Demo%20Intelegence%20Agent/docs/final-release-gate-report.md) | The combined verdict, component by component |
| [`Demo Intelegence Agent/docs/final-integration-gaps.md`](Demo%20Intelegence%20Agent/docs/final-integration-gaps.md) | Every external contract that could not be verified |
| [`docs/release-gate-report.md`](docs/release-gate-report.md) | The Tutor half's own gate |
| [`Demo Intelegence Agent/docs/security/threat-model.md`](Demo%20Intelegence%20Agent/docs/security/threat-model.md) | 34 threats, each with a control and a test |

Nothing is called verified merely because the code exists. Components are
classified `VERIFIED LOCALLY`, `VERIFIED WITH LIVE PROVIDER`,
`IMPLEMENTED — LIVE CREDENTIAL REQUIRED`, `EXTERNAL CONTRACT NOT AVAILABLE`,
`BLOCKED BY BUSINESS CONFIGURATION`, `KNOWN RISK` or `FAILED`.

**Current verdict: `READY FOR STAGING — LIVE PROVIDER VERIFICATION REQUIRED`.**
Meta, Google, Cashfree and the NXTutors gateway have no live credentials in this
environment, so their wire formats are exercised against fakes rather than the
real services.
