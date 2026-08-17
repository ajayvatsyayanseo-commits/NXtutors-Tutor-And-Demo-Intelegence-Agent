# NXTutors Tutor Intelligence Agent (`tutor-match-meta`)

**This one agent is the fusion of eight agents from the NXTutors register.**
Everything the following eight were specified to do is done here, in one
process, against one database:

| # | Source agent | Category | Skills it contributes |
| --- | --- | --- | --- |
| **013** | Tutor Availability | Tutor Discovery & Matching | Parse schedules · Find free slots · Handle time zones · Check overlap · Suggest slots |
| **014** | Tutor Subject Expertise | Tutor Discovery & Matching | Map subjects · Align syllabus · Tag depth level · Score expertise · Flag mismatches |
| **015** | Tutor Past Performance Score | Tutor Discovery & Matching | Aggregate reviews · Normalise ratings · Compute retention · Score outcomes · Flag risk |
| **016** | Tutor Personality Compatibility | Tutor Discovery & Matching | Profile communication · Map temperament · Score compatibility · Predict conflict · Suggest pairing |
| **017** | Tutor Academic Compatibility | Tutor Discovery & Matching | Match board · Match class · Match exam · Map topic coverage · Rate academic fit |
| **019** | Tutor Proximity | Tutor Discovery & Matching | Geocode address · Compute distance · Estimate travel time · Validate radius · Cluster locality |
| **021** | Tutor Negotiation Profile | Tutor Discovery & Matching | Analyse fee history · Score flexibility · Model minimum fee · Detect overload risk · Suggest negotiation style |
| **022** | Tutor Replacement Risk | Tutor Discovery & Matching | Detect churn signals · Score dissatisfaction · Analyse conflict pattern · Raise early alerts · Suggest backup tutor |

Forty skills, eight bounded evaluators, one orchestrator. That the composition
is real — every agent registered, weighted, reachable and producing grounded
evidence — is asserted by
[`tests/contract/test_composed_agent_coverage.py`](tests/contract/test_composed_agent_coverage.py),
not merely claimed here.

---

Turns a parent's WhatsApp message into a ranked shortlist of 2–3 real NXTutors
tutors, with real profile links and a plain-language reason for each.

> "Need class 10 cbse maths teacher near sector 57 after 6:30, home tuition,
> around 900 per hour"

→ a structured requirement, a hard-filtered candidate pool from live NXTutors
data, eight scored dimensions, a versioned ranking policy, and a short reply a
coordinator could have written.

---

## The one rule

**Nothing about a tutor is ever invented.** Not a name, fee, rating, distance,
availability, verification status or profile URL. Every scored dimension carries
`evidence` and a `data_quality` flag, and the explanation layer is mechanically
prevented from citing a dimension whose data is missing, stale, or below the
policy's minimum sample size (`orchestration/evidence_guard.py`).

Where the website simply does not hold the data — tutor availability, geographic
coordinates, fee units, churn history — the service says so rather than guessing.
See [docs/assumptions.md](docs/assumptions.md).

---

## Architecture in one paragraph

One orchestrator, not eight chattering agents. `TutorMatchOrchestrator` runs a
staged pipeline and calls eight **bounded, side-effect-free, independently
testable** evaluators. Each takes a typed requirement plus a typed candidate and
returns a `SkillScore` with score, confidence, evidence, data quality, flags and
reason codes. The orchestrator combines them using a **versioned `ScoringPolicy`
loaded from YAML** — no business weight appears in Python source — and stamps the
policy id, version and checksum onto every decision.

### The network boundary shapes everything

There is **no NAT Gateway**, and that is not a detail — it decides which code
runs where. A Lambda in the private subnets reaches RDS Proxy and the VPC
endpoints and *nothing else*. A Lambda outside the VPC reaches the internet and
*not* PostgreSQL. Neither one can do both, so the turn is split across the
boundary and every hop is an SQS FIFO queue with its own DLQ:

```
WhatsApp / Lead Intake
        │  signed HTTPS (HMAC, replay window, body cap)
        ▼
  Ingress Lambda                              [internet — no DB]
   validate, dedup, rate-limit, enqueue
        │  SQS enrich.fifo   (MessageGroupId = conversation_id)
        ▼
  Enrich Worker Lambda                        [internet — no DB grant at all]
   A extract requirement   (deterministic → OpenAI tier 1)
   B recall permitted memory
        │  SQS match.fifo   — carries a versioned EnrichmentV1
        ▼
  Match Worker Lambda                         [VPC — no internet route]
   C decide what to ask     (smallest missing set)
   D hard filters           (deterministic; a model cannot override one)
   E candidate retrieval    (structured SQL + pgvector on the projection)
   F eight skill scorers    (013–022 above)
   G rank via ScoringPolicy (versioned YAML, checksummed onto the decision)
   H explain from approved evidence only
   I resolve canonical profile links
        │
        ├─► PostgreSQL   state, decisions, outbox, cache, rate buckets
        │                 (the single application database — see below)
        │  SQS outbound
        ▼
  Outbound Worker Lambda                      [internet — no DB grant]
   WhatsApp Cloud API  ·  Chitragupta memory deeds
```

Each function declares its side as `TMM_NETWORK_ZONE`, the process refuses a
configuration it cannot honour, and
[`tests/security/test_network_boundary.py`](tests/security/test_network_boundary.py)
asserts that declaration matches Terraform's `vpc_config`.

### One database, and nothing else

PostgreSQL is the only datastore. There is no Redis (the shared cache, rate
buckets and kill switches are PostgreSQL tables — see
`cache/postgres_store.py`), no MySQL (tutor data arrives over a signed HTTPS
feed — see `integrations/website/tutor_feed.py`), no DynamoDB, no MongoDB, and
no container runtime anywhere: no ECS, no Fargate, no EC2, no Kubernetes. Those
absences are asserted, not assumed.

The eight evaluators, and where each lives:

| # | Skill | Module | Weighted in policy |
| --- | --- | --- | --- |
| 013 | Tutor Availability | `matching/availability` | yes |
| 014 | Tutor Subject Expertise | `matching/subject_expertise` | yes |
| 015 | Tutor Past Performance | `matching/performance` | yes |
| 016 | Tutor Personality Compatibility | `matching/personality` | yes |
| 017 | Tutor Academic Compatibility | `matching/academic` | yes |
| 019 | Tutor Proximity | `matching/proximity` | yes |
| 021 | Tutor Negotiation Profile | `matching/negotiation` | yes |
| 022 | Tutor Replacement Risk | `matching/replacement_risk` | yes (internal-only reasoning) |

---

## Quick start (no AWS, no OpenAI key, no Docker)

```bash
uv sync --all-extras
make check          # lint + types + tests + contract tests
make e2e            # full local conversation, end to end
```

`make e2e` runs a real multi-turn conversation through the real orchestrator
against in-memory adapters and a seeded synthetic tutor set. It exercises
extraction, the FSM, hard filters, all eight evaluators, ranking, evidence
guarding, link resolution and outbound composition.

With a disposable PostgreSQL reachable, set `TMM_INTEGRATION_DSN` and
`pytest -m integration` exercises the real persistence path. That is the only
external dependency the test suite has — there is no second database to stand
up.

---

## Layout

```
src/tutor_match_meta/
  contracts/       versioned typed shapes (requirement, tutor, scoring, inbound, schedule)
  domain/          normalizers: subjects, boards, classes, modes, fees, localities, schedules
  matching/        the eight bounded evaluators + hard filters
  scoring/         versioned policy loading and rank combination
  orchestration/   the staged pipeline, evidence guard, explanation, link resolver
  state/           deterministic FSM with optimistic locking
  repositories/    PostgreSQL persistence + tutor projection
  integrations/    website feed + write-back, chitragupta, whatsapp, openai, geo
  rag/             ingestion, chunking, hybrid retrieval, injection defense
  cache/           L1 in-process / L2 PostgreSQL, behind one interface (no Redis)
  security/        signing, PII, rate limits, injection, URL allowlist
  observability/   structured logs, EMF metrics, tracing, cost telemetry
  config/          typed settings + versioned business policies (YAML)
  handlers/        Lambda entry points (ingress · enrich · match · outbound · api)
  analytics/       sanitized exports
config/policies/   the versioned ScoringPolicy documents
migrations/        Alembic
infra/terraform/   Lambda, SQS FIFO + DLQ, VPC endpoints, EventBridge, S3, IAM, alarms
                   (no NAT Gateway, no ECS/Fargate/EC2, no ElastiCache)
tests/             unit · contract · integration · security · e2e · load
docs/              audit, assumptions, ownership, runbooks, final report
```

---

## Status

Read [docs/release-gate-report.md](docs/release-gate-report.md) for the
component-by-component PASS / FAIL / EXTERNAL-NOT-VERIFIED verdict, and
[docs/final-implementation-report.md](docs/final-implementation-report.md)
before deploying. It classifies every capability as **VERIFIED**, **IMPLEMENTED
BUT REQUIRES EXTERNAL CREDENTIAL**, **DEFERRED WITH JUSTIFICATION**, or **KNOWN
RISKS**, and nothing is called verified merely because the code exists.
