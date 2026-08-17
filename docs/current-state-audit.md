# Current State Audit — NXTutors TutorMatch Meta Agent

Audit date: 2026-08-11
Auditor: implementation team (this repository)
Method: direct inspection of local checkouts. No repository was guessed at.

---

## 1. What was actually inspected

| Source | Location inspected | Access |
| --- | --- | --- |
| `nxtutors-lead-intake-agent` | `../nxtutors-lead-intake-agent` (local checkout) | read |
| `nxtutors-chitragupta-memory-Agent` | `../nxtutors-chitragupta-memory` (local checkout) | read |
| `nx-whatsapp-onboarding-agent` | `../Onbording agent/nx-whatsapp-onboarding-agent` | read |
| NXTutors Laravel website | `../Nxtutors Website/public` (local checkout) | read |
| Production MySQL structure | `../Nxtutors Website/public/127_0_0_1.sql` (240 MB dump) | read (DDL only) |
| Demo Command Center Agent | `../Demo Command Center Agent` | read (conventions only) |
| `Agents 141 and skills .xlsx` | `../../Agents 141 and skills .xlsx` | partially machine-readable |

Nothing in any of those repositories was modified by this build.

---

## 2. The authoritative tutor schema (MySQL, Laravel)

This is the single most important audit finding: **the website schema stores far
less than a naive matcher would assume.** Every design decision downstream is
constrained by what actually exists.

### 2.1 `register` — the tutor/parent master table

Discriminator: `join_as = 'teacher'`, active flag `status = 't'`.

Columns relevant to matching:

| Column | Type | Matching use | Caveats |
| --- | --- | --- | --- |
| `user_id` | varchar | canonical tutor identity | **string**, not the numeric `id` |
| `name` | varchar | display + profile slug | |
| `city`, `district`, `state`, `pincode` | varchar | proximity | free text, unnormalised |
| `address` | text | locality hint only | **contains private home addresses** |
| `gender` | varchar | hard filter when parent explicitly asks | `male`/`female` free text |
| `experience` | varchar | performance/expertise | free text e.g. `"5 years"` |
| `education`, `other_education` | varchar/text | expertise evidence | free text |
| `budget` | varchar | fee band | free text, **no unit stored** |
| `class_type` | varchar | mode hint | free text (`Online`/`Home`/`Both`) |
| `for_class` | varchar | class hint | free text |
| `profile`, `profile_desc`, `pro_desc` | text | RAG narrative + personality evidence | free text |
| `avatar` | varchar | display | |
| `status` | enum('t','f') | hard filter | |

Columns that must **never** leave the service boundary:
`email`, `phone`, `password`, `c_password`, `otp`, `otp_status`, `dob`,
`document_type`, `document_number`, `frount_image`, `back_image`.

The Laravel side already enforces this allowlist in
`app/NxtAi/Support/PublicTutorFieldMapper.php`. This service mirrors that
allowlist rather than inventing a second one.

### 2.2 `teacher_courses` — the string course schema

`user_id, subject, board, for_class, class_type, mode, status('t'/'f'), date`

Free-text subject/board/class strings. This is the primary capability source.

### 2.3 `teacher_course_managment` — the id-based course schema

`user_id, pid, cid, cat_id, sub_id` — all joining into `category`
(`cat_title`, `pid`, `cid`, `slug`, `status`).

**Both schemas are live.** The Laravel model exposes an `effective_courses`
accessor that prefers one and falls back to the other. The projection builder in
this service must union both, exactly as `PublicTutorFieldMapper::capabilities()`
does, or it will silently drop capability rows for half the tutor base.

### 2.4 `teacher_review` — the only outcome evidence that exists

`user_id, name, rating, expertise, patience, reliability, communication, message, date, status`

All rating columns are **varchar**, cast at query time. `status='t'` gates
visibility. This table is the sole grounding for:

- past-performance scoring (`rating`, review count),
- personality/communication compatibility (`patience`, `communication`),
- reliability signals (`reliability`),
- subject-expertise corroboration (`expertise`).

Sample sizes are small for most tutors. Low-n handling is mandatory, not optional.

### 2.5 Locality reference data

`city_managment` (`city_name`, `slug`, `status`) and
`city_area_list_managment` (`city_id`, `name`, `slug`, `pincode`, `status`)
provide the canonical city/locality vocabulary and pincodes.

### 2.6 Lead/enquiry write targets

- `student_enquiry_managment` — parent enquiry rows.
- `demo_leads` — demo booking rows (`name, phone, service, subject, child_class, preferred_time, mode, location, message, source_page`).

---

## 3. Data that does NOT exist (critical gaps)

These gaps drive the honesty rules in the matcher. Each is a place where a
careless implementation would fabricate.

| Missing | Consequence for this service |
| --- | --- |
| **No tutor availability table** | There is no weekday/time-slot data anywhere in MySQL. Availability cannot be asserted from website data. The Meta Agent owns its own `tutor_availability` projection, sourced from tutor-declared slots captured by this agent or by onboarding — and reports `data_quality = "missing"` until that exists. It must never claim "available Mon/Wed after 6:30" without a record. |
| **No latitude/longitude** | Distance cannot be computed from `register` alone. This service geocodes `pincode`/`locality` (never the raw private `address`) into its own cached `geo_point` table, and falls back to pincode/locality equality when geocoding is unavailable. |
| **No fee unit** | `register.budget` is free text with no per-hour/per-month marker. Fees are surfaced as an unqualified label (`₹800–₹1,200`), never as "per hour". |
| **No travel radius** | Tutor travel radius is not stored. A configurable default radius per city tier is used, marked as a policy default rather than tutor-declared fact. |
| **No churn / replacement history** | Replacement-risk has almost no grounding. The evaluator returns low confidence and `data_quality="insufficient"` unless this service has accumulated its own outcome records. It must not manufacture a risk narrative. |
| **No verification status field** | There is no explicit "verified" flag beyond `status='t'` and the presence of KYC document columns. "Verified" is therefore *not* claimed to parents. |
| **No structured syllabus/topic map** | Topic-depth matching relies on the RAG corpus (curriculum documents) plus free-text course strings, and reports low confidence when unmapped. |

---

## 4. Canonical tutor profile URL (verified)

From `resources/views/tutor/partials/cards.blade.php` and
`HomeController::showsingletutornew()`:

```
/tutor/{Str::slug(city ?: 'india')}/{base64url(user_id + '-nxt')}/{Str::slug(name ?: 'tutor')}
```

- base64 is URL-safe (`+/` → `-_`) with `=` padding stripped.
- The controller reverses it and 404s unless the decoded value ends in `-nxt`.
- The route requires `join_as='teacher' AND status='t'`, so a link to an
  inactive tutor is a hard 404.

This service reproduces the encoding exactly and **verifies the tutor is active
in the projection before emitting any link.** No slug is ever guessed.

---

## 5. Existing Laravel matching logic (must not be contradicted)

`app/NxtAi/Services/TutorSearchService.php` already implements a bounded search:

- candidate pool capped at 80,
- parameter-bound queries only,
- city aliases via `CityNormalizer`, pincode exact,
- gender as a hard SQL filter,
- subject as a hard PHP filter,
- fee/experience/rating as soft filters,
- ordering by `TutorRanker`, never by the LLM.

The Meta Agent's hard-filter rules are a **superset** of these, and its subject
matching is deliberately stricter (normalised synonyms rather than substring
containment, which currently lets "Science" match "Computer Science").

---

## 6. Chitragupta memory contract (verified from SDK source)

`sdk/python/chitragupta_client`:

- `ChitraguptaClient.post_event(event) -> {"ack": "remote"|"local_wal", ...}`;
  4xx (except 429) raises `ChitraguptaRejectedError` and is **not** spooled.
- `query(MemoryQueryRequest) -> MemoryPacket` with explicit
  `allowed_fields`/`denied_fields`/`allowed_resources`/`denied_resources`.
- `make_event()` enforces `deed_type` regex `^[A-Z][A-Z0-9_\.]{2,63}$`,
  `lifecycle_status ∈ {started, progress, completed, failed}`, 2000-char summary
  caps, and refuses secret-shaped keys (`assert_safe`).
- `AgentLifecycle` wraps started/completed/failed with trace propagation headers.

This service **imports that contract shape** into a vendored client that speaks
the identical wire protocol, so it does not depend on the Chitragupta repo being
pip-installable in the Lambda build. Contract tests assert the two stay aligned.

---

## 7. Upstream contract from the Lead Intake Agent

`app/services/integrations/tutor_matching_agent.py` publishes to
`TUTOR_MATCHING_AGENT_WEBHOOK_URL` using the event shape in
`app/services/integrations/events.py`:

```json
{
  "event_id": "...", "event_type": "lead.captured",
  "lead_id": "...", "phone_hash": "sha256(...)",
  "class": "Class 10", "subject": "Maths", "board": "CBSE",
  "city": "Gurgaon", "tuition_mode": "home",
  "missing_fields": [], "confidence_score": 0.82,
  "created_at": "..."
}
```

Note: the lead intake agent forwards a **`phone_hash`, not a phone number.**
The ingress contract in this service accepts that shape natively.

The webhook client on the lead-intake side is currently a stub
(`webhook_mode_not_enabled_for_external_calls`). Live end-to-end delivery from
lead intake is therefore **UNVERIFIED_EXTERNAL** until that flag is enabled by
its owners. This service's ingress is complete and independently testable.

---

## 8. House conventions adopted

From the newest in-house service (Demo Command Center Agent):

- `src/<package>` layout, `uv` + `hatchling`, Python 3.12.
- `ruff` (E,F,I,UP,B,ASYNC,S,T20,RUF), `mypy --strict`, `pytest` with
  `integration`/`contract`/`security` markers, coverage gate.
- FastAPI + Mangum for Lambda, SQLAlchemy 2 + asyncpg + Alembic, structlog.
- `Makefile` with a single `make check` gate.
- Terraform under `infra/terraform`, GitHub Actions per environment.

Deviation from the prompt's suggested `app/` tree: this repo uses
`src/tutor_match_meta/<module>` with the same module names. Rationale: it matches
the newest in-house standard, keeps the package importable without path hacks,
and is what `hatchling`/`uv` expect. The internal module names from the brief
(`handlers`, `contracts`, `domain`, `orchestration`, `matching/*`, `scoring`,
`state`, `repositories`, `integrations/*`, `rag`, `cache`, `security`,
`observability`, `config`, `prompts`, `analytics`) are preserved verbatim.
