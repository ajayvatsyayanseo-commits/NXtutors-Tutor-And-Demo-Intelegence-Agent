# Privacy incident

**Trigger:** PII found anywhere it must not be — a log, a metric dimension, an
analytics export, a model payload, a DLQ dashboard, a ticket.

**This runbook outranks every other one.** Availability work stops until
containment is done.

DPDP Act 2023 applies. Do not decide the notification question alone.

---

## 0. First five minutes

1. **Do not delete anything yet.** Deleted evidence makes scope unknowable, and
   "we cannot determine the scope" is a far worse position than a known one.
2. **Screenshot or copy the finding into the incident channel with the value
   redacted.** Record *where* and *what class*, never the value itself.
3. **Page the data owner.** This is not an on-call-alone decision.

## 1. Contain

Identify the destination and stop the flow.

| Destination | Immediate action |
| --- | --- |
| CloudWatch Logs | `aws logs put-retention-policy --log-group-name <lg> --retention-in-days 1` — buys time; deletion comes after scoping |
| Metric dimensions | `assert_label_safe` should have prevented this. If it did not, deploy the fix; the metric already exists and cannot be edited |
| Analytics table | `UPDATE tutor_match.kill_switch ... MEMORY_WRITES_PAUSED`, then `TMM_ANALYTICS_ENABLED=false` |
| S3 export | Suspend the Glue crawler; the objects are KMS-encrypted and private, so exposure is internal |
| Model payload | `LLM_PAUSED` immediately. Data has left our boundary — see §4 |
| A ticket or chat | Delete the message; ask the platform admin to purge edit history |

If the leak is ongoing and you cannot isolate the path:

```bash
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name,paused,actor,reason)
VALUES ('MATCHING_PAUSED',true,'$USER','privacy incident <INC>')
ON CONFLICT (name) DO UPDATE SET paused=true, actor=EXCLUDED.actor,
  reason=EXCLUDED.reason, changed_at=now();"
```

Lossless: Lead Intake keeps answering, no dedup key is consumed.

## 2. Scope

Answer four questions, in writing:

**What class of data?** (`docs/data-classification.md`)
- PII → notification is likely required
- CONFIDENTIAL → commercial harm, not statutory
- INTERNAL/PUBLIC → probably not an incident

**How many people?**

```bash
aws logs start-query --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(date -u -d '7 days ago' +%s) --end-time $(date -u +%s) \
  --query-string 'fields @timestamp | filter @message like /<redacted pattern>/ | stats count()'
```

**How long?** Compare the first occurrence against the deploy that introduced
it (`git log`, `/internal/v1/version`).

**Who could have read it?** CloudWatch Logs → anyone with `logs:FilterLogEvents`
on the account. S3 → the bucket is private and KMS-encrypted; check CloudTrail
`GetObject`. A model provider → §4.

## 3. Eradicate

Only after scoping is written down.

```bash
# Log streams containing the leak.
aws logs delete-log-stream --log-group-name "/aws/lambda/$SVC-..." \
  --log-stream-name "<stream>"
```

CloudWatch cannot delete individual events — the stream is the unit. If the
leak spans a whole group and the retention loss is acceptable, delete the group;
Terraform recreates it on the next apply.

```sql
-- Analytics rows. Verify the count first, then delete.
SELECT count(*) FROM tutor_match.analytics_event
WHERE dimensions::text ~ '<pattern>';

DELETE FROM tutor_match.analytics_event
WHERE dimensions::text ~ '<pattern>';
```

```bash
# S3 objects, if the export already ran.
aws s3 rm "s3://$SVC-analytics/exports/<partition>/" --recursive
# Versioning is on: delete markers are not enough.
aws s3api list-object-versions --bucket "$SVC-analytics" --prefix "exports/<partition>/"
aws s3api delete-object --bucket "$SVC-analytics" --key "<key>" --version-id "<vid>"
```

## 4. If data reached a model provider

- `TMM_LLM_STORE_RESPONSES` is **false** by default, so provider-side retention
  should be minimal. Confirm the deployed value:

```bash
aws lambda get-function-configuration --function-name "$SVC-match-worker" \
  --query 'Environment.Variables.TMM_LLM_STORE_RESPONSES'
```

- If it was `true`, raise a deletion request with the provider and record the
  ticket reference.
- Rotate the API key regardless (`docs/runbooks/leaked-key.md`): you cannot
  prove the key was not also exposed by whatever caused the leak.

## 5. Notification

**Not your decision alone.** Bring the data owner and legal the written scope
from §2. DPDP obligations turn on affected-person count and data category.

Prepare, do not send:
- number of people affected
- categories of data
- window of exposure
- who could have accessed it
- what has been done

## 6. Prevent

An incident that produces no test is an incident that recurs.

1. **Add the failing case to the suite.** The four privacy layers each have
   one: `test_model_payload.py`, `test_analytics_privacy.py`,
   `test_cache_hygiene.py`, `test_invariants.py`. The new case belongs in
   whichever layer failed.
2. **Ask why the existing layer did not catch it.** If a field arrived at a
   model, `ModelContext` was bypassed — find out how. If a dimension was
   exported, it was on `ALLOWED_DIMENSIONS` and should not have been.
3. **Update `docs/data-classification.md`** if the field's class was wrong.

## 7. Resume

```bash
psql "$TMM_POSTGRES_DSN" -c "
UPDATE tutor_match.kill_switch SET paused=false, actor='$USER',
  reason='contained, fix deployed <INC>', changed_at=now()
WHERE name IN ('MATCHING_PAUSED','LLM_PAUSED');"
```

Only after the fix is deployed and its test is green in CI.
