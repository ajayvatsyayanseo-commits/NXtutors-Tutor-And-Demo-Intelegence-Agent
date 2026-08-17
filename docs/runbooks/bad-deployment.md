# Bad deployment

**Trigger:** smoke test failed, `-ingress-5xx`, `-worker-errors`, or any quality
alarm within 30 minutes of a deploy.

**Default action: roll back first, diagnose after.** A rollback takes ~2
minutes. Diagnosing in production takes longer and costs parents replies.

---

## 1. Confirm what is actually deployed

```bash
curl -s -H "X-NXTUTORS-INTERNAL-SECRET: $TMM_INTERNAL_SECRET" \
  "$INTERNAL_URL/internal/v1/version" | jq
```

```json
{
  "app_version": "0.1.0",
  "git_sha": "abc123…",
  "schema_revision": "0003",
  "default_policy": "regular_school_support.v1",
  "prompt_versions": {"extraction": "v1", "explanation": "v1"},
  "kill_switches": {...},
  "holding_work": []
}
```

**If `git_sha` is not the SHA you just deployed, the deploy silently did not
land.** That is a different incident: the Lambda still runs the old code and
the alarm has another cause.

## 2. Is it the code, or something that rolls back on its own axis?

Check in this order — three of the four are faster than an application rollback:

| Changed | Rollback | Time |
| --- | --- | --- |
| `prompt_versions` | `TMM_PROMPT_PINS` | ~30s, no deploy |
| `default_policy` | `TMM_DEFAULT_POLICY` | ~30s, no deploy |
| Model ids | `TMM_MODEL_*` | ~30s, no deploy |
| `git_sha` only | Application rollback, §3 | ~2 min |

## 3. Application rollback

```bash
gh workflow run rollback.yml \
  -f environment=production \
  -f target_sha=<previous-good-sha>
```

Or directly:

```bash
PREV=<previous-good-sha>
for fn in ingress internal-api match-worker outbound-worker scheduled; do
  aws lambda update-function-code \
    --function-name "$SVC-$fn" \
    --s3-bucket "$ARTIFACT_BUCKET" \
    --s3-key "tutor-match-meta/$PREV.zip" --publish
done
```

**Code only.** Migrations are additive by policy (`scripts/check_migration_safety.py`
blocks anything else in the automated pipeline), so the previous code runs
correctly against the current schema. Rolling the schema back is a separate,
manual, human-approved decision — see `docs/runbooks/migrations.md`.

## 4. Verify

```bash
TMM_SMOKE_INTERNAL_URL="$INTERNAL_URL" \
TMM_SMOKE_INGRESS_URL="$INGRESS_URL" \
python scripts/smoke_test.py --environment production --expect-sha "$PREV"
```

Then confirm the parent-visible path:

```bash
aws sqs get-queue-attributes --queue-url "$MATCH_QUEUE" \
  --attribute-names ApproximateAgeOfOldestMessage
```

## 5. Drain what accumulated

```bash
# Anything that failed during the bad window is in the DLQ.
aws sqs get-queue-attributes --queue-url "$MATCH_DLQ" \
  --attribute-names ApproximateNumberOfMessages
```

Replay is safe once the rollback is verified — idempotency means a message that
partially succeeded will not produce a second decision.
`docs/runbooks/dlq-replay.md`.

```bash
# Outbox rows leased by a worker that was killed mid-deploy.
aws lambda invoke --function-name "$SVC-scheduled" \
  --payload '{"job":"relay_outbox"}' /dev/stdout
```

`relay_outbox` reclaims abandoned leases before it claims new ones, which is
the correct order — otherwise an orphaned row is never picked up again.

## 6. Root cause

The deploy pipeline runs the full gate (`docs/release-gate-report.md`), so a
defect that reached production is also a **gap in the gate**. Ask which gate
should have caught it:

| Symptom | Gate that should have caught it |
| --- | --- |
| Import error, `AttributeError` | unit suite |
| Contract mismatch between components | contract suite |
| PII leak | security suite |
| Timeout under load | load benchmark — **currently NOT EXECUTED** |
| Bad shortlist quality | evaluation suite — **currently does not exist** |

The last two are named release blockers. If the failure falls into either, the
rollback is correct and the gate gap is the real finding.

Add the failing case to the appropriate suite **before** rolling forward again.
