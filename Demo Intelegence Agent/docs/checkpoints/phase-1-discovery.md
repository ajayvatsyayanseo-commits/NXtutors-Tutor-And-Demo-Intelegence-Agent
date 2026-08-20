# Phase 1 — Discovery Checkpoint

Written before implementation. Everything below is read from the repository, not
assumed. Where something could not be read, it is listed under **Gaps** with the
fixture contract that stands in for it.

Baseline commit: `594c7585fdb7ab7b448ed3ea30beec2e18160b9f` (branch `main`).

---

## 1. Actual repository structure

```
NXTutors Tutor Intelligence Agent/          <- repo root
├── src/tutor_match_meta/                   <- PROTECTED (131 .py files)
├── config/policies/                        <- PROTECTED (8 scoring policies)
├── migrations/versions/0001..0005           <- PROTECTED (Alembic, tutor schema)
├── infra/                                   <- PROTECTED
├── tests/                                   <- PROTECTED (Tutor-only suites)
├── .github/workflows/                       <- PROTECTED (ci/deploy/rollback)
├── docs/, scripts/, alembic.ini, pyproject.toml, uv.lock
└── Demo Intelegence Agent/                  <- THIS PHASE'S ONLY WRITE TARGET
```

### Pre-existing working-tree state

`git status --short` at start reported **all 29 files under `Demo Intelegence
Agent/` as deleted in the working tree** (` D`), while present in HEAD. The
directory existed on disk but was empty.

Action taken: the 29 blobs were restored **byte-exact** from HEAD
(`git cat-file blob` → file copy). No destructive git command was used
(`git checkout --` was declined by the sandbox; `git reset --hard` was never
attempted). Post-restore `git status --porcelain` shows a clean tree apart from
newly created `Demo Intelegence Agent/docs/`. Nothing pre-existing was
discarded — the restore only added content into an empty directory.

---

## 2. Protected Tutor boundaries

199 files under the protected prefixes were SHA-256 hashed into
`protected-tutor-baseline.json` alongside their HEAD blob ids. Prefixes:

`src/tutor_match_meta/`, `config/policies/`, `migrations/`, `infra/terraform/`,
`tests/`, `.github/workflows/`.

These are read-only for this phase. Demo imports nothing from
`tutor_match_meta` at runtime; the contract compatibility that matters is
asserted by Demo's own contract tests against locally-declared shapes, so
Demo remains independently deployable.

---

## 3. Public Tutor integration surface (read from code)

| Surface | File | Verdict |
|---|---|---|
| `TutorMatchOrchestrator.match(requirement, trace_id=…) -> MatchOutcome` | `orchestration/orchestrator.py:120` | **Chosen boundary.** Pure function of its inputs. Sends nothing. Returns `MatchDecisionV1` with full shortlist, per-dimension `SkillScore`, evidence and `DataQuality`. |
| `POST /internal/v1/handoff` | `api/internal.py:55` | **Rejected for Demo.** Runs `HandoffService` → `TurnService`, which owns Tutor's own conversation FSM, writes Tutor state, and can return `reply_text`. Calling it would create a second conversation owner for one WhatsApp thread. |
| SQS ingress (`handlers/ingress.py`) | | **Rejected.** Same path as above, plus async — Demo needs the candidates inside one turn. |
| `AgentEnvelopeV1` | `contracts/envelope.py` | Already contains `AgentId.DEMO_COMMAND_CENTER = "demo_command_center_agent"`. |
| Handoff graph | `integrations/agents/graph.py:44,53` | `TUTOR_MATCH → DEMO_COMMAND_CENTER` and `DEMO_COMMAND_CENTER → {CHITRAGUPTA, WEBSITE, NOTIFICATION, HUMAN}` already permitted. `WHATSAPP_OWNER = LEAD_INTAKE`. |
| `OutboundOwnership` | `contracts/handoff.py:59` | `CALLER_SENDS` (default) vs `TUTOR_MATCH_SENDS`, mutually exclusive by settings validation. |

### The return-only decision

`TutorMatchOrchestrator.match()` **is already** the return-only mode the spec
asks for. It has no sender, no outbox write and no state write — those live one
layer up in `TurnService`. Demo therefore calls the orchestrator, never the
turn service, and no Tutor source file needs a Demo-specific condition.

Consumed contract types (read-only imports in the *adapter layer only*, never
in Demo's domain): `MatchRequirementV1`, `MatchDecisionV1`, `ShortlistEntry`,
`ScoredCandidate`, `SkillScore`, `DataQuality`, `Tracked`, `Provenance`,
`TuitionMode`, `Freshness`, `WeeklySchedule`.

Demo's domain depends only on `TutorIntelligencePort` +
`TutorMatchRequestV1`/`TutorMatchResultV1`, which are Demo-owned Pydantic
models. The in-process adapter translates. That is what keeps
`tutor_match_meta` an optional dependency: Demo's test suite and E2E run
against the fake and the local adapters with `tutor_match_meta` absent.

---

## 4. Demo scaffold completeness (29 files, all preserved and extended)

Present and **kept as-is** (extended, never rewritten):

- `config/settings.py` — 130+ typed settings, placeholder detection, four
  cross-field validators including `_single_outbound_owner`.
- `config/policies.py` — checksummed YAML loader + `ReminderPolicy`,
  `DiscountPolicy`, `ForecastModel`, `MonitoringPolicy`.
- `contracts/envelope.py` — Demo's `AgentEnvelopeV1`, wire-compatible with
  Tutor's, plus `expires_at` and `tenant_id` which Tutor's lacks.
- `contracts/common.py` — opaque `_REF` pattern, `Requirement`, `Evidence`,
  `Confidence`, `DemoOutcome`, `ContactRef`.
- `contracts/events.py` — `DomainEvent` closed enum + `PUBLIC_EVENTS`.
- `security/{signatures,pii,guardrails,rate_limit,urls}.py` — complete.
- `observability/{logging,metrics}.py` — EMF + PII-scrubbing JSON logs.
- `shared/{clock,ids,money}.py` — `Clock` protocol, ULID ids, `Money` in paise.
- `config/policies/*.yaml` — 4 versioned policies with real values.

**Absent, and built this phase:** state machine, orchestration, all 8
capabilities, domain models, repositories, storage, all 8 integrations,
handlers, glue/outbox, resilience, human handoff, analytics, cost control,
memory, cache, CLI, migrations, infra, scripts, tests, docs.

### Envelope divergence (deliberate, documented)

Demo's envelope carries `expires_at` and `tenant_id`; Tutor's does not. Demo's
`ALLOWED_EDGES` is a Demo-scoped subset. Both are additive — a Demo envelope
deserialises into Tutor's model (extra fields ignored on Tutor's side is *not*
true: Tutor's `AgentEnvelopeV1` has no `extra="allow"`). Therefore Demo never
sends its own envelope model to Tutor; the adapter emits only fields Tutor
declares. Pinned by `tests/contract/test_envelope_compatibility.py`.

---

## 5. Website gateway surface

`E:\NX Tutor\Nxtutors Website` is **outside this workspace** and access was
declined by the sandbox. `packages/nxtutors/demo-command-center-adapter` could
not be read.

Consequence, per spec §4: the gateway client is implemented against **versioned
fixture contracts** under `tests/fixtures/gateway/`, every endpoint is recorded
as unverified in `docs/integration-gaps.md`, and the client is built so the
path/verb/field names are a single typed table that can be corrected in one
place once the Laravel package is readable.

Operations the domain needs from the gateway (derived from the journey in §12
of the brief, not invented from the Laravel schema):

`resolve_identity`, `resolve_tutor_contacts`, `tutor_availability`,
`plan_quote`, `record_demo`, `activate_subscription`, `discount_eligibility`,
`region_authorization`.

Tutor Intelligence's own gateway client (`integrations/website/gateway.py`) was
read for the **auth scheme only**: HMAC signing with a signing key, base URL
from settings, URL-policy validated. Demo mirrors that scheme rather than
inventing a second one.

---

## 6. Agent handoff contracts available

| Agent | Readable? | Contract used |
|---|---|---|
| Lead Intake | Indirectly — its exact POST shape is pinned in `tutor_match_meta/contracts/handoff.py` | `LeadIntakeHandoffV1` shape mirrored as Demo's `LeadIntakeHandoffV1`; same `X-NXTUTORS-INTERNAL-SECRET` header, same `{status, reply_text}` response |
| Onboarding | Not readable | `AgentEnvelopeV1` to `AgentId.ONBOARDING` over the outbox + `onboarding_webhook_url`; adapter + fake + fixture |
| Tutor Intelligence | Fully readable | in-process orchestrator facade (§3) |

---

## 7. Meta WhatsApp template names

Confirmed present in `config/policies/reminder.v1.yaml` (already committed,
described there as approved in the live WABA):

`demo_reminder_t24h`, `demo_reminder_t2h`, `demo_reminder_t15m`

Named in the brief but **not** present in any repository file:
`demo_tutor_request_expired`, `demo_scheduled_confirmation`.

The tutor-confirmation template name was described in the brief as truncated in
a screenshot and is therefore **not guessed**. The registry models it as a
declared-but-unresolved entry: `TemplateRegistry.get()` raises
`TemplateNotApproved` for it, the scheduling capability treats that as a
recoverable degradation (falls back to session-window free-form or human
handoff), and `docs/integration-gaps.md` records it as the one blocking Meta
item. No template is created or renamed by application code.

---

## 8. Storage topology

Tutor uses Alembic + SQLAlchemy + asyncpg against schema `tutor_match_meta`
(migrations `0001`–`0005`). Demo must not share those tables.

Demo uses:
- Its own schema **`dcc`** in the same serverless PostgreSQL/Aurora cluster.
- **Aurora Data API over boto3** — no asyncpg, no SQLAlchemy, no Alembic. That
  is what `pyproject.toml` already encodes (no DB driver in `dependencies`) and
  what lets the orchestrator Lambda live outside the VPC.
- Plain numbered SQL migrations under `Demo Intelegence Agent/migrations/`,
  applied by `dcc-migrate`, tracked in `dcc.schema_migrations`.
- No new cluster is provisioned this phase.

---

## 9. Implementation gaps (carried into `docs/integration-gaps.md`)

1. Laravel gateway — every endpoint unverified; fixture-backed.
2. Tutor-confirmation template name — unknown; registry refuses it.
3. `demo_tutor_request_expired` / `demo_scheduled_confirmation` — named in the
   brief, unverified against the WABA export.
4. Onboarding agent contract — inferred from `AgentEnvelopeV1` only.
5. No live credentials for Meta, Google, Cashfree, OpenAI, Aurora. All are
   exercised through fakes; live smoke tests are opt-in and skipped by default.

---

## 10. Chosen architecture

```
Meta webhook (ack only, no LLM)
   → SQS work queue
      → DemoCommandCenterOrchestrator          (the ONE conversation owner)
         ├─ ownership guard      (only the owner may speak)
         ├─ deterministic FSM    (30 states, table-driven, optimistic locking)
         ├─ capability dispatch  (8 modules, pure-ish, return Commands)
         │    129 monitoring · 018 forecasting · 025 scheduling · 026 reminders
         │    031 objections   · 032 conversion · 034 discounts · 036 paid
         ├─ LLM proposals → schema → state → authz → policy → deterministic exec
         └─ transactional outbox
              → ONE outbound boundary (session/template/opt-out/idempotency)
                 → Meta sender
```

Capability workers get their own Lambda handlers but **never** decide they own
the conversation: they return `Command` objects, and only
`orchestration/outbound.py` may send.

---

## 11. Explicit assumptions

- **A1** `TutorMatchOrchestrator.match()` is a supported in-process boundary.
  Evidence: it is already constructed standalone in `bootstrap.build_local_stack`
  and has no sender/state dependency. No Tutor file is modified to enable it.
- **A2** Demo owns the conversation for the whole demo lifecycle; Tutor
  matching is a capability call, not an ownership lease. Justified by the
  handoff graph having no `DEMO_COMMAND_CENTER → LEAD_INTAKE` edge.
- **A3** `outbound_ownership=self_sends` is the target deployment posture
  (already the settings default). Under `caller_sends`, Meta must be disabled —
  enforced by the existing `_single_outbound_owner` validator.
- **A4** The gateway is the only route to NXTutors data. No direct MySQL.
- **A5** Money is INR paise everywhere; `Money` refuses floats.
- **A6** All stored instants are UTC; all user-facing rendering resolves through
  an IANA zone stored on the demo row.
- **A7** A tutor id may only originate from a Tutor Intelligence result or the
  gateway. An id that appears first in LLM output is rejected — enforced in
  `guardrails/tutor_selection.py`, not by prompt instruction.
