# PostgreSQL outage or saturation

**Alarms:** `-proxy-connections`, `-worker-errors`, `-match-dlq-not-empty`
**Parent sees:** no reply. Messages queue and retry.

This is the one dependency with **no graceful degradation**, deliberately: we
do not produce a recommendation whose decision cannot be recorded. An
unauditable shortlist is worse than a delayed one.

The instance is **shared with `demo_command_center`**. Assume another team is
also affected, and say so early.

---

## 1. Is it us or the instance?

```bash
aws rds describe-db-instances --db-instance-identifier database-1 \
  --query 'DBInstances[0].{status:DBInstanceStatus,az:AvailabilityZone}'

aws cloudwatch get-metric-statistics --namespace AWS/RDS \
  --metric-name DatabaseConnections --dimensions Name=DBProxyName,Value=$RDS_PROXY_NAME \
  --start-time "$(date -u -d '30 min ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
  --period 300 --statistics Maximum
```

Compare the maximum against our ceiling:

```
match_worker_reserved_concurrency + internal_api_reserved_concurrency
```

- **Below the ceiling, instance unhealthy** → an instance problem. Go to §3.
- **At or above the ceiling** → we are the saturating consumer. Go to §2.

## 2. We are saturating it

```bash
# Stop the bleeding: cut our concurrency in half, immediately.
aws lambda put-function-concurrency \
  --function-name "$SVC-match-worker" --reserved-concurrent-executions 10
aws lambda put-function-concurrency \
  --function-name "$SVC-internal-api" --reserved-concurrent-executions 5
```

Messages queue rather than fail — SQS retains for 4 days, so a reduced rate is
recoverable and a saturated database is not.

Then find what changed:

```bash
psql "$TMM_POSTGRES_DSN" -c "
SELECT pid, state, wait_event_type, wait_event,
       now() - query_start AS duration, left(query, 120) AS query
FROM pg_stat_activity
WHERE application_name LIKE 'tutor-match-meta%'
ORDER BY duration DESC LIMIT 20;"
```

A query running longer than `TMM_POSTGRES_STATEMENT_TIMEOUT_MS` (5s) should
have been killed by the statement timeout. If it was not, the timeout is not
being applied — check `create_engine`'s `server_settings`.

Kill a specific runaway:

```bash
psql "$TMM_POSTGRES_DSN" -c "SELECT pg_cancel_backend(<pid>);"
# Only if cancel does not work:
psql "$TMM_POSTGRES_DSN" -c "SELECT pg_terminate_backend(<pid>);"
```

## 3. The instance is down

```bash
# Stop consuming. MATCHING_PAUSED is lossless: Lead Intake keeps answering,
# and no dedup key is consumed, so redeliveries after unpause still work.
psql -h <replica-or-skip> ... # if you cannot reach the DB, use the AWS console
```

If PostgreSQL is unreachable you cannot flip the switch through the database.
Use the environment-variable fallback on the two consumers:

```bash
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --environment "Variables={TMM_FLAG_ENABLED=false,$(aws lambda get-function-configuration \
    --function-name "$SVC-match-worker" --query 'Environment.Variables' --output text)}"
```

Simpler and safer under pressure — disable the event source, which stops
consumption without touching configuration:

```bash
UUID=$(aws lambda list-event-source-mappings \
  --function-name "$SVC-match-worker" --query 'EventSourceMappings[0].UUID' --output text)
aws lambda update-event-source-mapping --uuid "$UUID" --no-enabled
```

Messages accumulate in SQS (4-day retention). Note the time; you need it for §5.

## 4. Recovery

```bash
# 1. Confirm the database answers.
psql "$TMM_POSTGRES_DSN" -c "SELECT 1;"

# 2. Confirm our schema and migration head are intact.
psql "$TMM_POSTGRES_DSN" -c "SELECT version_num FROM tutor_match.alembic_version;"
# Expect: 0003

# 3. Re-enable consumption at REDUCED concurrency first.
aws lambda put-function-concurrency \
  --function-name "$SVC-match-worker" --reserved-concurrent-executions 5
aws lambda update-event-source-mapping --uuid "$UUID" --enabled

# 4. Watch queue age drain before restoring full concurrency.
watch -n 30 'aws sqs get-queue-attributes --queue-url "$MATCH_QUEUE" \
  --attribute-names ApproximateAgeOfOldestMessage ApproximateNumberOfMessages'
```

Restore full concurrency only once queue age is falling.

## 5. After the backlog drains

The outbox may hold replies claimed by a relay that died mid-batch:

```bash
psql "$TMM_POSTGRES_DSN" -c "
SELECT status, count(*) FROM tutor_match.outbox_event GROUP BY status;"
```

Rows stuck in `claiming` are reclaimed automatically by the next relay run
(`CLAIM_LEASE_SECONDS = 900`). To force it:

```bash
aws lambda invoke --function-name "$SVC-scheduled" \
  --payload '{"job":"relay_outbox"}' /dev/stdout
```

Anything in `dead` needs a human: `docs/runbooks/dlq-replay.md`.

## 6. Check for stale conversations

Parents who waited more than a few minutes may have given up or resent.
Duplicates are absorbed by idempotency, but a conversation stuck mid-state is
not:

```bash
psql "$TMM_POSTGRES_DSN" -c "
SELECT state, count(*) FROM tutor_match.conversation_state
WHERE updated_at > now() - interval '4 hours'
GROUP BY state ORDER BY 2 DESC;"
```

A pile-up in `MATCHING` means turns started and never finished. Those parents
got no reply and no error — hand the list to the coordination team.

---

## Escalate when

- The instance is down more than 15 minutes → notify the `demo_command_center`
  owners; this is a shared-infrastructure incident.
- `alembic_version` is not `0003`, or the schema is missing → **stop**. Do not
  re-run migrations under pressure. `docs/runbooks/migrations.md`.
