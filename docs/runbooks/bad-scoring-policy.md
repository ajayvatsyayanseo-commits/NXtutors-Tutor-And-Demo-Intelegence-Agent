# Bad scoring policy

**Alarms:** `-human-handoff-spike`, `-no-match-rate`, replacement-rate regression
**Parent sees:** wrong tutors, or "no match" when tutors clearly exist.

A scoring policy is **data**, not code. Rolling it back does not need a
release.

---

## 1. Which policy produced the bad decisions?

Every decision is stamped with the policy id, version **and checksum**:

```sql
SELECT policy_id, policy_version, policy_checksum,
       count(*) AS decisions,
       count(*) FILTER (WHERE NOT matched) AS no_match,
       count(*) FILTER (WHERE requires_human_review) AS hitl,
       round(avg(jsonb_array_length(shortlist)), 2) AS avg_shortlist
FROM tutor_match.match_decision
WHERE generated_at > now() - interval '24 hours'
GROUP BY 1,2,3 ORDER BY decisions DESC;
```

Compare against the same query over the previous week. A step change aligned
to a deploy is the answer.

## 2. Was the policy actually edited?

The checksum catches an unversioned edit — a live policy file changed without
its version bumped:

```sql
SELECT policy_id, policy_version, count(DISTINCT policy_checksum) AS checksums
FROM tutor_match.match_decision
WHERE generated_at > now() - interval '30 days'
GROUP BY 1,2 HAVING count(DISTINCT policy_checksum) > 1;
```

**Any row here is a process failure**: the same `policy_id.version` produced
two different weightings, so historical decisions cannot be compared. Treat it
as an incident in its own right.

## 3. Roll back — configuration, not code

```bash
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --environment "Variables={...,TMM_DEFAULT_POLICY=regular_school_support.v1}"
```

Confirm:

```bash
curl -s -H "X-NXTUTORS-INTERNAL-SECRET: $TMM_INTERNAL_SECRET" \
  "$INTERNAL_URL/internal/v1/version" | jq '.default_policy'
```

**Historical scores are never rewritten.** `match_decision` keeps the policy
that produced each decision, so a rollback changes future matching only — which
is what makes a before/after comparison possible at all.

## 4. Diagnose which dimension is wrong

```sql
-- Which hard filter is emptying the pool?
SELECT r->>'rule' AS rule, count(*)
FROM tutor_match.match_decision d,
     jsonb_array_elements(d.rejections) r
WHERE d.generated_at > now() - interval '6 hours'
GROUP BY 1 ORDER BY 2 DESC;
```

A dominant rule means a filter is too strict — or its input data went missing
(a projection column that stopped populating looks exactly like a strict
filter).

```sql
-- Weight coverage: how much of the policy could actually be evaluated?
SELECT round(avg((c->>'weight_coverage')::numeric), 3) AS avg_coverage
FROM tutor_match.match_decision d, jsonb_array_elements(d.score_snapshot) c
WHERE d.generated_at > now() - interval '6 hours';
```

Coverage below `HUMAN_REVIEW_COVERAGE_FLOOR` (0.35) forces human review — so a
HITL spike with **low coverage** is a *data* problem, not a weighting problem.
Check `docs/runbooks/website-sync-stale.md` before touching weights.

## 5. Changing a policy properly

§27 requires all of this, and none of it is optional:

1. **New version file** — `config/policies/<name>.v2.yaml`. Never edit `v1`.
2. **Record the reason** in the file header: what changed, why, expected effect.
3. **Offline evaluation** against the evaluation dataset.
   ⚠️ **The evaluation dataset does not exist yet** — this is a named release
   blocker (`docs/production-control-matrix.md` control 22). Until it does,
   step 3 cannot be performed and a policy change cannot be justified
   quantitatively before rollout.
4. **Shadow comparison** — `TMM_FLAG_SHADOW_MODE=true` runs the new policy on
   real traffic and returns `DECLINED`, so decisions are recorded with no
   parent-visible effect.
5. **Staged rollout** — `TMM_FLAG_PERCENTAGE_ROLLOUT=5`, then 25, then 100.
6. **Rollback ready** — the previous version stays deployed and pinnable.

## 6. Multi-objective check before declaring success

Never optimise for conversion alone (§38). Compare all of these against the
previous version:

| Metric | Source | Regression is |
| --- | --- | --- |
| Top-3 acceptance | `MatchAccepted` / `MatchRequests` | Any drop |
| No-match rate | `NoMatch` | A drop is **also** suspicious — it can mean the filters went soft |
| Subject/board match error | Manual review of a sample | Any increase |
| Availability conflict rate | Replacement events | Any increase |
| Replacement rate | `match_feedback` | Any increase |
| HITL rate | `HumanHandoff` | Any increase |
| Cost per match | `LlmCostMicros` / `MatchAccepted` | >50% |

A policy that raises acceptance while raising the replacement rate has made
matching **worse** and is generating a lagging cost. That is the specific
failure §38 exists to prevent.
