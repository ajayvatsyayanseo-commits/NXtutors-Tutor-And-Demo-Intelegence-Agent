# DLQ inspection and replay

**Alarms:** `-match-dlq-not-empty`, `-outbound-dlq-not-empty`, `-outbox-dead`
**Parent sees:** no reply at all. Every message in a DLQ is a family who was
never answered — the threshold is 0 for exactly that reason.

---

## 1. Inspect without consuming

```bash
aws sqs receive-message --queue-url "$MATCH_DLQ" \
  --max-number-of-messages 10 --visibility-timeout 0 \
  --message-attribute-names All \
  --query 'Messages[*].Body' --output text | jq -s '.'
```

`--visibility-timeout 0` returns the message immediately, so reading does not
hide it from the next reader.

**The body is an `InboundEnvelope`.** Read `trace_id`, `conversation_id`,
`source_agent` and `kind`. Do **not** paste the payload into a ticket: for a
WhatsApp turn it contains the parent's message text.

## 2. Find out why

```bash
TRACE=<trace_id from the body>
aws logs filter-log-events --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(( ($(date +%s) - 86400) * 1000 )) \
  --filter-pattern "{ \$.trace_id = \"$TRACE\" }" \
  --query 'events[*].message' --output text
```

## 3. Classify — this decides everything that follows

| Class | Evidence | Action |
| --- | --- | --- |
| **Poison** — malformed envelope, schema violation | `unparseable sqs record`, `ValidationError` | Never replayable. §4 |
| **Transient** — dependency was down | `OperationalError`, `LLMTimeout`, connection reset | Replayable once healthy. §5 |
| **Our bug** — logic error | `TypeError`, `KeyError`, `AttributeError` | Fix, deploy, then replay. §5 |
| **Stale** — older than the conversation | `received_at` more than a few hours ago | §6 |

## 4. Poison messages

A malformed envelope will never succeed. Replaying it wastes an hour and ends
in the same place.

```bash
# Capture it for the producing team first — WITHOUT the payload body.
aws sqs receive-message --queue-url "$MATCH_DLQ" --max-number-of-messages 10 \
  --query 'Messages[*].{id:MessageId,receipt:ReceiptHandle}' > /tmp/poison.json

# Then delete.
aws sqs delete-message --queue-url "$MATCH_DLQ" --receipt-handle "<handle>"
```

Raise it with whoever produced it. A schema violation from Lead Intake is a
contract break: `docs/event-contracts.md` pins the shape.

## 5. Replay

**Only after the cause is fixed.** Replaying into a still-broken service
refills the DLQ and burns the retry budget.

```bash
# Confirm the dependency is actually healthy first.
curl -s "$INTERNAL_URL/internal/v1/ready" | jq

# Redrive everything back to the source queue.
aws sqs start-message-move-task \
  --source-arn "$(aws sqs get-queue-attributes --queue-url "$MATCH_DLQ" \
      --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)" \
  --max-number-of-messages-per-second 5
```

Rate-limit the move (`5/s`) — dumping a large DLQ back at full speed is how a
recovering database is knocked over a second time.

Watch it:

```bash
aws sqs list-message-move-tasks --source-arn "$DLQ_ARN" --max-results 1
```

**Replay is safe.** Idempotency is keyed on `provider_message_id`, so a message
that partially succeeded before failing will not produce a second decision or a
second reply. `tests/e2e/test_resilience.py::TestDuplicateDelivery` proves it,
including the concurrent case.

## 6. Stale messages

A turn from six hours ago is worse than useless: the parent has moved on, and
replying now is confusing.

```bash
# Decide by age.
aws sqs receive-message --queue-url "$MATCH_DLQ" --max-number-of-messages 10 \
  --visibility-timeout 0 --query 'Messages[*].Body' --output text \
  | jq -r '.received_at'
```

Older than ~2 hours: delete, and hand the conversation list to the
coordination team for a human follow-up. Note that `conversation_id` contains
the phone number — pass it through a secure channel, not a ticket comment.

## 7. The outbox `dead` state

Distinct from the DLQ. A row reaches `dead` when delivery failed 5 times, or
when it could never be addressed at all.

```sql
SELECT dedup_key, kind, status, attempts, last_error, created_at
FROM tutor_match.outbox_event
WHERE status = 'dead' ORDER BY created_at DESC LIMIT 20;
```

| `last_error` | Meaning | Action |
| --- | --- | --- |
| `invalid_delivery_payload` | The row does not satisfy `OutboundDeliveryV1` — usually written by an older deployment | Never deliverable. Record and delete. |
| `meta_<code>` | Meta refused | Look up the code. A closed 24-hour window is permanent; retrying wastes the signal. |
| `sqs unreachable` | Transient | Reset to `pending` (below). |
| `no_outbound_queue_configured` | Misconfiguration | Fix `TMM_OUTBOUND_QUEUE_URL`, then reset. |

Reset a transient failure:

```sql
UPDATE tutor_match.outbox_event
SET status = 'pending', attempts = 0, available_at = NULL,
    claimed_at = NULL, last_error = NULL
WHERE dedup_key = '<key>';
```

Then force a relay run:

```bash
aws lambda invoke --function-name "$SVC-scheduled" \
  --payload '{"job":"relay_outbox"}' /dev/stdout
```

## 8. Rows stuck in `claiming`

A relay that timed out mid-batch leaves its rows leased. They are neither
`pending` (so no relay picks them up) nor `dead` (so nothing alarms) — which
is precisely the silent-loss case `reclaim_stale` exists to close.

```sql
SELECT count(*) FROM tutor_match.outbox_event
WHERE status = 'claiming' AND claimed_at < now() - interval '15 minutes';
```

The next relay run reclaims them automatically (`CLAIM_LEASE_SECONDS = 900`).
If you need it now, invoke `relay_outbox` — it reclaims **before** it claims,
which is the correct order.

---

## After any DLQ event

1. Add the failure class to `tests/e2e/test_resilience.py` if it is not
   already covered. A DLQ event that could recur without a test is a gap.
2. Record it in the incident log with the trace id — never the payload.
