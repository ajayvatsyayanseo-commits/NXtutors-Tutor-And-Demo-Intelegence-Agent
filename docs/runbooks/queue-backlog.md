# Queue backlog

**Alarms:** `-match-queue-age` (>120s), `-optimistic-lock-conflicts`
**Parent sees:** a reply that arrives minutes late, or not at all.

Queue age is measured separately from processing time on purpose. A service
whose p95 processing time looks healthy while queue age climbs is a service
that is *under-provisioned*, not slow.

---

## 1. How far behind, and getting worse?

```bash
aws sqs get-queue-attributes --queue-url "$MATCH_QUEUE" --attribute-names \
  ApproximateNumberOfMessages ApproximateAgeOfOldestMessage \
  ApproximateNumberOfMessagesNotVisible
```

- `NotVisible` high, `Messages` low → workers are busy. Throughput problem, §2.
- `Messages` high, `NotVisible` low → workers are not consuming. §3.
- Both climbing → §2 and §3.

## 2. Workers are busy but too slow

```bash
aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Throttles --dimensions Name=FunctionName,Value="$SVC-match-worker" \
  --start-time "$(date -u -d '1 hour ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
  --period 300 --statistics Sum
```

**Throttles > 0 means reserved concurrency is the binding limit.** Before
raising it, check what it protects:

```bash
aws cloudwatch get-metric-statistics --namespace AWS/RDS \
  --metric-name DatabaseConnections --dimensions Name=DBProxyName,Value="$RDS_PROXY_NAME" \
  --start-time "$(date -u -d '1 hour ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
  --period 300 --statistics Maximum
```

Raising concurrency without raising the proxy's connection limit is how the
**shared** database gets exhausted — and `demo_command_center` depends on it
too. If there is headroom:

```bash
aws lambda put-function-concurrency \
  --function-name "$SVC-match-worker" --reserved-concurrent-executions 40
```

Then raise `match_worker_reserved_concurrency` in Terraform via a PR, so the
change survives the next apply.

**Which stage is slow?** This is what the stage metrics are for:

```bash
for m in StageExtractionMs StageCandidateSqlMs StageSkillScoringMs \
         StageRagMs StageExplanationMs StagePersistenceMs; do
  echo -n "$m: "
  aws cloudwatch get-metric-statistics --namespace NXTutors/TutorMatchMeta \
    --metric-name "$m" --start-time "$(date -u -d '1 hour ago' +%FT%TZ)" \
    --end-time "$(date -u +%FT%TZ)" --period 3600 --extended-statistics p95 \
    --query 'Datapoints[0].ExtendedStatistics.p95' --output text
done
```

| Dominant stage | Likely cause | Fix |
| --- | --- | --- |
| `StageExtractionMs` | Provider latency | `LLM_PAUSED` — deterministic parsing is fast and the majority is well-formed |
| `StageCandidateSqlMs` | Missing index, or an unbounded pool | §4 |
| `StageRagMs` | Vector search slow | `RAG_PAUSED` — supplementary only |
| `StagePersistenceMs` | Database contention | `docs/runbooks/db-outage.md` |

## 3. Workers are not consuming

```bash
aws lambda list-event-source-mappings --function-name "$SVC-match-worker" \
  --query 'EventSourceMappings[0].{state:State,enabled:State,uuid:UUID,concurrency:ScalingConfig}'
```

`State` should be `Enabled`. If it is `Disabled`, someone disabled it during a
prior incident and did not restore it:

```bash
aws lambda update-event-source-mapping --uuid "<uuid>" --enabled
```

Check for a poison record blocking a message group. **FIFO ordering means one
stuck message blocks its whole `MessageGroupId`** — that is one conversation,
not the queue, but it looks like a stall:

```bash
aws sqs get-queue-attributes --queue-url "$MATCH_DLQ" \
  --attribute-names ApproximateNumberOfMessages
```

## 4. Slow candidate SQL

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM tutor_match.tutor_projection
WHERE synced_at >= now() - interval '24 hours'
  AND lower(city) = ANY(ARRAY['gurugram','gurgaon'])
ORDER BY review_count DESC, rating_avg DESC NULLS LAST, tutor_id
LIMIT 60;
```

Expect an index scan on `ix_tutor_projection_synced_city` and no sort node
(`ix_tutor_projection_rank` covers the ORDER BY). A sequential scan means
migration 0003's CONCURRENTLY index build failed and left an INVALID index:

```sql
SELECT indexrelid::regclass AS index, indisvalid
FROM pg_index WHERE NOT indisvalid;
```

Rebuild any invalid index (outside a transaction):

```sql
DROP INDEX CONCURRENTLY tutor_match.ix_tutor_projection_synced_city;
CREATE INDEX CONCURRENTLY ix_tutor_projection_synced_city
  ON tutor_match.tutor_projection (synced_at, city);
```

## 5. Optimistic lock conflicts

`-optimistic-lock-conflicts` firing alongside a backlog almost always means
**FIFO grouping is not being honoured**: two workers have the same
conversation at once, which should be impossible with
`MessageGroupId = conversation_id`.

```bash
aws sqs get-queue-attributes --queue-url "$MATCH_QUEUE" --attribute-names \
  FifoQueue ContentBasedDeduplication DeduplicationScope FifoThroughputLimit
```

Expect `DeduplicationScope=messageGroup` and
`FifoThroughputLimit=perMessageGroupId`. Also confirm the event source is
`batch_size = 1` — a batch of 10 serialises unrelated conversations behind each
other *and* can put two turns of one conversation in one invocation.

## 6. Shed load if nothing else works

```bash
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name,paused,actor,reason)
VALUES ('MATCHING_PAUSED',true,'$USER','backlog <INC>')
ON CONFLICT (name) DO UPDATE SET paused=true, actor=EXCLUDED.actor,
  reason=EXCLUDED.reason, changed_at=now();"
```

Lead Intake keeps answering. New messages are declined at the edge rather than
queued, so the backlog drains instead of growing — and because the switch is
read before the idempotency claim, nothing is lost.

## 7. Confirm recovery

```bash
watch -n 30 'aws sqs get-queue-attributes --queue-url "$MATCH_QUEUE" \
  --attribute-names ApproximateAgeOfOldestMessage ApproximateNumberOfMessages'
```

Unpause only when age is falling steadily, not merely when it stops rising.
