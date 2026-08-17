# Unexpectedly high no-match rate

**Alarm:** `-no-match-rate` (>20 per 15 min, 2 periods)
**Parent sees:** *"I couldn't find a tutor matching…"* when tutors exist.

Read the direction carefully. A **drop** in no-match rate is also suspicious:
it can mean a hard filter went soft and families are being shown tutors who do
not actually fit.

---

## 1. Which constraint is emptying the pool?

`_diagnose()` records the dominant rejection reason on every no-match decision,
so this is one query:

```sql
SELECT no_match_reason, count(*)
FROM tutor_match.match_decision
WHERE NOT matched AND generated_at > now() - interval '2 hours'
GROUP BY 1 ORDER BY 2 DESC;
```

| Reason | Meaning | Go to |
| --- | --- | --- |
| `empty_candidate_pool` | The SQL returned nothing at all | §2 |
| `filtered_by:<rule>:<n>` | A hard filter removed everyone | §3 |
| `below_quality_threshold` | Candidates survived but scored too low | §4 |
| `no_resolvable_profile_links` | Links could not be verified | §5 |

## 2. `empty_candidate_pool` — nothing came back from SQL

Almost always freshness, not the filters:

```sql
SELECT count(*) AS total,
       count(*) FILTER (WHERE synced_at > now() - interval '24 hours') AS usable
FROM tutor_match.tutor_projection;
```

`usable` far below `total` → the projection has aged out.
→ `docs/runbooks/website-sync-stale.md`. This is the single most common cause
of this alarm.

If freshness is fine, the pool is being over-constrained upstream. Check
whether the query is filtering on a low-confidence guess:

```sql
SELECT payload->'subject'->>'value'    AS subject,
       payload->'subject'->>'confidence' AS confidence,
       payload->'subject'->>'provenance' AS provenance
FROM tutor_match.match_requirement
WHERE conversation_id IN (
  SELECT conversation_id FROM tutor_match.match_decision
  WHERE NOT matched AND generated_at > now() - interval '1 hour' LIMIT 20);
```

`usable_for_hard_filter` requires a confidence above the policy floor precisely
so a model guess cannot delete candidates. Provenance `LLM` with a confidence
above the floor would be a regression — extraction confidence is capped at 0.70
(`LLM_CONFIDENCE`) deliberately, below `HARD_FILTER_MIN_CONFIDENCE`.

## 3. `filtered_by:<rule>` — a hard filter is too strict

```sql
SELECT r->>'rule' AS rule, count(*) AS rejections,
       count(DISTINCT d.match_session_id) AS sessions
FROM tutor_match.match_decision d, jsonb_array_elements(d.rejections) r
WHERE d.generated_at > now() - interval '2 hours'
GROUP BY 1 ORDER BY 2 DESC;
```

Compare against the same window last week. A rule whose rejection count jumped
is either genuinely stricter, or **its input data disappeared** — a projection
column that stopped populating looks exactly like a strict filter.

```sql
-- Is the data still there?
SELECT count(*) FILTER (WHERE jsonb_array_length(subjects) = 0) AS no_subjects,
       count(*) FILTER (WHERE jsonb_array_length(boards)   = 0) AS no_boards,
       count(*) FILTER (WHERE jsonb_array_length(classes)  = 0) AS no_classes,
       count(*) FILTER (WHERE fee_min IS NULL)                  AS no_fee,
       count(*) AS total
FROM tutor_match.tutor_projection
WHERE synced_at > now() - interval '24 hours';
```

A column that is suddenly empty across the projection is a **sync contract
break**, not a matching problem → `docs/runbooks/website-sync-stale.md` §4.

Note the design already tolerates *missing* capability data: the candidate
query only filters on a capability when the tutor declares one (`not
c.capabilities.subjects or any(...)`). A filter rejecting on *unknown* would be
a code regression worth checking in the last deploy.

## 4. `below_quality_threshold` — candidates scored too low

```sql
SELECT round(avg((c->>'final_score')::numeric), 3)     AS avg_score,
       round(avg((c->>'weight_coverage')::numeric), 3) AS avg_coverage
FROM tutor_match.match_decision d, jsonb_array_elements(d.score_snapshot) c
WHERE d.generated_at > now() - interval '2 hours';
```

**Low coverage is the tell.** It means most dimensions could not be evaluated —
missing availability, missing reviews, missing coordinates — so scores collapse
toward the neutral value and fall below the cutoff. That is a data problem
wearing a scoring problem's clothes.

High coverage but low scores → the policy genuinely changed.
→ `docs/runbooks/bad-scoring-policy.md`.

## 5. `no_resolvable_profile_links`

A link we cannot verify is not published — sending a 404 to a parent is worse
than sending one fewer tutor. If this reason dominates, `public_ref` is
missing or malformed in the projection:

```sql
SELECT count(*) FILTER (WHERE public_ref IS NULL OR public_ref = '') AS missing_ref
FROM tutor_match.tutor_projection WHERE synced_at > now() - interval '24 hours';
```

## 6. Genuine demand outside coverage

Sometimes the answer is simply that NXTutors has no tutor for that request.
That is a **business** signal, not a defect, and it is one of the more valuable
outputs this service produces:

```sql
SELECT payload->'subject'->>'value'        AS subject,
       payload->'student_class'->>'value'  AS class,
       payload->'location'->>'city'        AS city,
       count(*) AS unmet
FROM tutor_match.match_requirement r
JOIN tutor_match.match_decision d USING (conversation_id)
WHERE NOT d.matched AND d.generated_at > now() - interval '7 days'
GROUP BY 1,2,3 ORDER BY unmet DESC LIMIT 20;
```

Send this to the supply team. It is a tutor-recruitment brief.

## 7. What not to do

**Do not relax the hard filters to make the alarm stop.** An honest no-match is
a correct answer; a shortlist of tutors who cannot teach the subject is not.
The relaxation ladder in `docs/fallback-matrix.md` exists for this, and every
rung above "nearby time" asks the family before violating a stated requirement.

Note: rungs 1–4 of that ladder are **not implemented yet** — the service goes
straight from exact to an honest no-match with a named blocking constraint.
That is safe, and it is a known gap
(`docs/production-readiness-final.md` §14).
