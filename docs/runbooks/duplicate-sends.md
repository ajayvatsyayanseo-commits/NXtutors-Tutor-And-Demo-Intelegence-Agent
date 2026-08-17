# Duplicate messages

**Alarm:** `-duplicate-spike` (>50 per 5 min)
**Parent sees:** usually nothing — duplicates are absorbed. If they *do* see a
repeated shortlist, that is a defect, not load.

Distinguish two very different situations:

| | Meaning | Severity |
| --- | --- | --- |
| **`DuplicateEvents` high, no repeated replies** | Idempotency is working. Something upstream is retrying. | Investigate |
| **A parent received the same shortlist twice** | Idempotency failed. | **Incident** |

---

## 1. Which is it?

```sql
-- More than one decision for one conversation in a short window is the smoking gun.
SELECT conversation_id, count(*) AS decisions,
       min(generated_at), max(generated_at)
FROM tutor_match.match_decision
WHERE generated_at > now() - interval '2 hours'
GROUP BY 1 HAVING count(*) > 1
ORDER BY 2 DESC LIMIT 20;
```

A conversation legitimately produces several decisions across a long
conversation. Several **within seconds** is a duplicate.

```sql
-- Did the same reply get delivered twice?
SELECT conversation_id, dedup_key, status, delivered_at
FROM tutor_match.outbox_event
WHERE conversation_id = '<id>' ORDER BY created_at;
```

`dedup_key` is unique (primary key), so two delivered rows for one message is
structurally impossible. Two rows with **different** dedup keys and the same
body means the *upstream* sent two distinct `provider_message_id`s for one
parent message — which is an upstream bug, not ours.

## 2. Absorbed duplicates — find the retrier

```bash
aws logs filter-log-events --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(( ($(date +%s) - 3600) * 1000 )) \
  --filter-pattern '{ $.message = "duplicate event ignored" }' \
  --query 'events[*].message' --output text | head -20
```

Then ask **why** they are retrying. The overwhelmingly common cause is that we
are too slow and the caller times out:

```bash
aws cloudwatch get-metric-statistics --namespace AWS/ApiGateway \
  --metric-name Latency --dimensions Name=ApiId,Value="$INTERNAL_API_ID" \
  --start-time "$(date -u -d '2 hours ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
  --period 300 --extended-statistics p95 p99
```

Lead Intake times out at **2 seconds** (`contracts/handoff.py::HANDOFF_BUDGET_SECONDS`).
p95 approaching 1800ms means they are already retrying some requests, and one
message becomes three — including three times the model spend.

Fix the latency, not the duplicate handling: `docs/runbooks/queue-backlog.md` §2.

Other causes:

| Cause | Evidence | Fix |
| --- | --- | --- |
| Meta redelivering a webhook | Same `wa_message_id` upstream | Normal. Absorbed. |
| SQS visibility timeout too short | `NotVisible` churn, worker duration near the timeout | `visibility_timeout` must exceed the worker timeout — it is set to `× 6` |
| A retry loop in the caller | Bursts at a fixed interval | Talk to the caller |

## 3. A parent genuinely received two replies

This is an incident. Work through the layers in order:

**Layer 1 — SQS FIFO dedup.**

```bash
aws sqs get-queue-attributes --queue-url "$MATCH_QUEUE" --attribute-names \
  FifoQueue ContentBasedDeduplication DeduplicationScope
```

Expect `ContentBasedDeduplication=false` — dedup is supplied per message from
the provider message id, never derived from the body, because two genuinely
different turns can have identical text.

**Layer 2 — the idempotency table.**

```sql
SELECT key, claimed_at, expires_at FROM tutor_match.idempotency_record
WHERE key LIKE '%<conversation>%';
```

The claim is `INSERT … ON CONFLICT DO NOTHING` inspecting `rowcount`, so the
database decides the winner. If two workers both proceeded, either the key
differed (upstream sent two ids) or the TTL had expired.

**Layer 3 — the outbox lease.** This is the one that was broken before the
hardening pass: `claim_batch` held `FOR UPDATE SKIP LOCKED` in an autocommit
session and never wrote the row, so the lock died with the session and two
overlapping relays could both send.

```sql
SELECT status, count(*) FROM tutor_match.outbox_event
WHERE created_at > now() - interval '1 hour' GROUP BY 1;
```

Confirm the fix is deployed:

```bash
curl -s -H "X-NXTUTORS-INTERNAL-SECRET: $TMM_INTERNAL_SECRET" \
  "$INTERNAL_URL/internal/v1/version" | jq '.schema_revision'
# Must be "0003" or later — `claimed_at` and the `claiming` status arrive there.
```

If `schema_revision` is `0002`, the lease does not exist and **this is the
cause**. Apply migration 0003.

**Layer 4 — provider-side.** `MetaCloudSender` sends `dedup_key` per message.
Two sends with the same key reaching Meta would still be two messages; the
protection is layers 1–3.

## 4. Immediate mitigation

```bash
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name,paused,actor,reason)
VALUES ('OUTBOUND_PAUSED',true,'$USER','duplicate sends <INC>')
ON CONFLICT (name) DO UPDATE SET paused=true, actor=EXCLUDED.actor,
  reason=EXCLUDED.reason, changed_at=now();"
```

**Lossy** — this holds messages. Note it, and unpause as soon as the cause is
found; `holding_work` on `/version` will keep reminding you.

## 5. After

Duplicate handling is covered by
`tests/e2e/test_resilience.py::TestDuplicateDelivery` — including the
**concurrent** case, which a check-then-act implementation passes sequentially
and fails concurrently. If the incident is not represented there, add it.
