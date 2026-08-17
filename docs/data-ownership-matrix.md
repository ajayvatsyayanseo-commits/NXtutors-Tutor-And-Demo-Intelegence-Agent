# Data Ownership Matrix — tutor-match-meta

"Owner" = the system permitted to create/update the value. Everyone else holds a
copy that must be treated as stale-able and must never be written back over the
owner's value.

---

## Legend

| Symbol | Meaning |
| --- | --- |
| **O** | Owner / system of record |
| R | Read-only replica or projection |
| W | May write, via the owner's approved command interface |
| — | No access |

Systems: **WEB** = Laravel/MySQL website · **TMM** = this service (PostgreSQL) ·
**CHT** = Chitragupta memory · **LI** = Lead Intake Agent · **LLM** = model provider

---

## Tutor data

| Data | WEB | TMM | CHT | LLM | Notes |
| --- | --- | --- | --- | --- | --- |
| Tutor identity (`user_id`, name) | **O** | R | — | pseudonym only | LLM sees `cand_1`…`cand_n`, never `user_id` |
| Tutor account status | **O** | R | — | — | drives hard filter A2 |
| Tutor email / phone / password / OTP | **O** | — | — | — | never leaves Laravel |
| Tutor KYC documents, DOB | **O** | — | — | — | never leaves Laravel |
| Tutor home address (`address`) | **O** | — | — | — | **never copied.** Only city/district/state/pincode are projected |
| Tutor city / district / state / pincode | **O** | R | — | locality only | |
| Tutor coordinates | — | **O** | — | — | derived by TMM from pincode/locality; not a website concept |
| Tutor subjects / boards / classes / modes | **O** | R | — | R | union of both course schemas |
| Tutor experience / education | **O** | R | — | R | free text, parsed on projection |
| Tutor fee band (`budget`) | **O** | R | — | band only | never labelled "per hour" (A3) |
| Tutor reviews + sub-scores | **O** | R (aggregates) | — | aggregate only | raw review text is not sent to the LLM |
| Tutor availability slots | — | **O** | — | R | TMM-owned; absent today (A4) |
| Tutor travel radius | — | **O** (policy default) | — | — | A6 |
| Replacement-risk score | — | **O** | — | — | internal only; never shown to parents |
| Canonical profile URL | **O** (route) | R (derived, verified) | — | — | derived deterministically, emitted only for active tutors |

## Parent / student data

| Data | WEB | TMM | CHT | LI | LLM | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Parent phone number | **O** | hashed + encrypted | hashed | **O** (intake) | — | never in logs or metric labels |
| Conversation transcript | — | **O** (retained per policy) | summaries only | R | redacted turn only | full transcript never sent to Chitragupta |
| Match requirement (structured) | — | **O** | R (facts) | R | R | |
| Confirmed preferences | — | R | **O** | — | R | Chitragupta is the cross-agent source of truth |
| Enquiry record | **O** | W (command) | — | — | — | via `PublishTutorLeadCommand` |
| Demo request | **O** | W (command) | — | — | — | via `CreateDemoRequestCommand` |
| Parent selection | **O** | W (command) | W (deed) | — | — | |

## Match / decision data

| Data | Owner | Retention | Notes |
| --- | --- | --- | --- |
| `match_session` | TMM | 400 days | conversation state, FSM version, lock version |
| `match_requirement` | TMM | 400 days | per-field confidence + provenance |
| `match_candidate_pool` | TMM | 90 days | pre-filter candidate ids + hard-filter reasons |
| `match_score_snapshot` | TMM | 400 days | full skill vector, policy version, evidence refs |
| `match_decision` | TMM | 400 days | final ranking, shortlist, timestamps, freshness |
| `match_feedback` | TMM | 400 days | parent selection / rejection |
| `llm_usage` | TMM | 180 days | tokens, cost, model, tier, trace |
| `outbox_event` | TMM | 30 days after delivery | website + memory write-back |
| `idempotency_record` | TMM | 7 days | inbound dedup |
| `rag_document` / `rag_chunk` | TMM | until superseded | with sensitivity + access scope |
| `geo_point` | TMM | 180 days | pincode/locality → lat/lng cache |

## Policy / configuration

| Data | Owner | Notes |
| --- | --- | --- |
| Scoring weights and thresholds | `config/policies/*.yaml`, version-controlled | never hardcoded in Python; version stamped on every decision |
| Model routing + budgets | `config/policies/model_routing.v1.yaml` | |
| Approved negotiation strategies | policy files | A12 |
| Feature flags / rollout % | env + SSM | |
| Secrets | AWS Secrets Manager | never in git, never in logs |

---

## Hard rules

1. **TMM never writes to MySQL outside a typed command.** No ad-hoc SQL writes,
   no LLM-generated SQL, ever.
2. **The projection is a copy, never an authority.** On conflict, MySQL wins and
   the projection is re-synced.
3. **Cache is never the system of record.** Every cached value has a TTL and a
   canonical re-read path.
4. **The LLM receives pseudonyms and an approved evidence bundle**, never a raw
   database dump, never `user_id`, never a phone number, never an address.
5. **A fact with no provenance is not a fact.** Every scored dimension carries
   `evidence` and `data_quality`; the explanation layer can only cite fields that
   passed `evidence_guard`.
