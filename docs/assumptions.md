# Assumptions — tutor-match-meta

Each assumption states what was assumed, why, the blast radius if wrong, and
where to change it. Assumptions are numbered so code and tests can cite them.

---

### A1 — `register.user_id` (string) is the canonical tutor identity, not `register.id`

**Why:** every public route, the base64 profile token, `teacher_courses.user_id`,
`teacher_review.user_id` and `teacher_course_managment.user_id` all key on
`user_id`. `register.id` is an internal autoincrement.
**If wrong:** every join and every profile link breaks loudly (404), not silently.
**Change at:** `repositories/website_tutor.py`, `domain/identity.py`.

### A2 — A tutor is matchable only when `join_as='teacher' AND status='t'`

**Why:** `HomeController::showsingletutornew()` 404s otherwise, so a link to any
other row is a broken link.
**If wrong:** shortlists would include tutors whose profile pages 404.
**Change at:** `matching/hard_filters.py::ACTIVE_TUTOR`.

### A3 — `register.budget` has no unit and must never be presented as "per hour"

**Why:** the column is free text with no unit anywhere in the schema. The Laravel
mapper makes the same choice and comments on it explicitly.
**If wrong:** we under-communicate. That is the safe direction; the alternative
is quoting a wrong price to a parent.
**Change at:** `domain/fees.py::format_fee_label`.

### A4 — Tutor availability is not in the website database and must not be inferred

**Why:** no availability table exists. `teacher_courses.date` is a record
timestamp, not a schedule.
**Consequence:** `AvailabilityEvaluator` returns `data_quality="missing"` and a
neutral score with low confidence when this service has no availability record of
its own. The explanation layer is forbidden from asserting specific days/times
that are not backed by a record (enforced by `orchestration/evidence_guard.py`
and `tests/security/test_no_fabrication.py`).
**Change at:** populate `tutor_availability` via the availability capture flow.

### A5 — Geocoding uses pincode/locality granularity only

**Why:** `register.address` contains private home addresses. Sending them to a
third-party geocoder or an LLM is an unnecessary privacy exposure.
**If wrong:** distance precision is coarser (≈ pincode centroid, typically
1–3 km error in Indian urban pincodes). Accepted deliberately.
**Change at:** `integrations/geo/`, `matching/proximity/`.

### A6 — Default tutor travel radius is a policy default, not a tutor fact

**Why:** no radius column exists.
**Values:** metro 8 km, tier-2 12 km, other 15 km — all in
`config/policies/*.yaml`, not in Python.
**If wrong:** tune the policy; no code change.
**Change at:** `config/policies/home_tuition.v1.yaml`.

### A7 — Review sub-scores are 1–5 on the same scale as `rating`

**Why:** they are stored identically (varchar) and rendered on the same widget in
the Laravel views.
**Guard:** values outside 0–5 are discarded as malformed rather than clamped, and
counted in `review_parse_failures` metrics.
**Change at:** `domain/reviews.py`.

### A8 — Fewer than 3 published reviews is an insufficient sample

**Why:** a single 5★ review is not evidence. The threshold is a policy value, not
a constant.
**Consequence:** `PerformanceEvaluator` returns the policy's neutral prior with
`data_quality="insufficient"` and the explanation never cites a rating.
**Change at:** `min_reviews_for_rating` in the scoring policy.

### A9 — Personality compatibility uses only pedagogical/communication evidence

Permitted evidence: review sub-scores (`patience`, `communication`), explicit
self-described teaching style in `profile_desc`, and explicit parent statements.
Forbidden: inferring temperament, mental-health traits, or any protected
attribute. Enforced by an allowlist of evidence sources in
`matching/personality/evidence.py` and by `tests/security/test_sensitive_inference.py`.

### A10 — Replacement risk is internal-only and evidence-poor today

**Why:** no churn data exists in MySQL. Until this service accumulates its own
outcome records, the evaluator has almost nothing to work with.
**Consequence:** it returns `data_quality="insufficient"` for nearly all tutors,
is excluded from the parent-facing explanation entirely, and only influences
tie-breaks. It never fires an alert on no evidence.

### A11 — Gender is a hard filter only when the parent explicitly requests it

**Why:** matches existing Laravel behaviour; an unrequested gender preference
would be discriminatory filtering.
**Change at:** `matching/hard_filters.py::GENDER_IF_REQUESTED`.

### A12 — Fee negotiation stays inside versioned business rules

No personalised pricing derived from inferred willingness to pay. The negotiation
evaluator may only: compare the parent's stated budget to the tutor's stored fee
band, report the gap, and select one of a **fixed, approved set** of negotiation
strategies from the policy file. Anything outside the policy's allowed band
requires HITL approval.
**Change at:** `config/policies/*.yaml → negotiation.strategies`.

### A13 — Ingress trusts a `phone_hash`, not a phone number, from lead intake

**Why:** that is what `events.py::phone_hash()` sends.
**Consequence:** conversation identity is `conversation_id`; the parent's phone
number is only present when the WhatsApp handoff supplies it, and is stored
hashed + encrypted-at-rest, never in logs or metric labels.

### A14 — Two live course schemas must both be read

`teacher_courses` (strings) and `teacher_course_managment` (category ids) are both
populated. The projection unions them.
**If wrong (only one read):** roughly half the tutor base silently loses all
subject/board/class capability and is hard-filtered out.
**Change at:** `repositories/website_tutor.py::_capabilities`.

### A15 — `category` hierarchy is board → class → subject via `pid`/`cid`

Inferred from `teacher_course_managment` columns (`pid`, `cid`, `cat_id`,
`sub_id`) and the Laravel relation names (`board`, `classCategory`, `category`).
**Confidence: medium.** The mapping is isolated in one function and the sync job
logs a `category_shape_mismatch` counter if the relation yields empty titles for
more than 5% of rows, so a wrong assumption surfaces in metrics rather than in
bad matches.

### A16 — Projection staleness beyond 24h blocks recommendation of that tutor

**Why:** a tutor who deactivated yesterday must not be recommended today.
**Values:** `fresh ≤ 6h`, `aging ≤ 24h` (recommendable, flagged), `stale > 24h`
(excluded), `unknown` (excluded). Policy-configurable.
**Change at:** `config/settings.py → projection_freshness_*`.

### A17 — Default shortlist size is 3, minimum quality gate is absolute

A candidate below the policy's `min_final_score` is never added just to reach
three. Returning one strong match, or none, is a valid outcome.

### A18 — LLM is never on the critical path for a decision

Tier 0 (deterministic) handles well-formed messages. Tier 1 handles ambiguous
extraction and phrasing. Tier 2 is entered only on explicit triggers listed in
`config/policies/model_routing.v1.yaml`. If every LLM call fails, the service
still produces a match using deterministic extraction and a templated
explanation. Proven by `tests/integration/test_llm_outage.py`.

### A19 — Timezone is Asia/Kolkata unless the requirement states otherwise

**Why:** the entire user base is Indian. IST has no DST, which removes a whole
class of bugs — but the schedule engine is still tz-aware end to end so a future
NRI/online-abroad case does not require a rewrite.
**Change at:** `config/settings.py → default_timezone`.

### A20 — Agent identity for Chitragupta is `tutor-match-meta`

The spreadsheet numbers the constituent capabilities 013, 014, 015, 016, 017,
019, 021, 022. Those are recorded in
`config/settings.py → composed_agent_ids` and attached to every memory event as
`skills_composed`, but the acting `agent_id` is the single meta agent — because
one orchestrator acted, not eight agents.
