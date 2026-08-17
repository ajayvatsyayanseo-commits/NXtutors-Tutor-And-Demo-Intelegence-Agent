# Tutor projection stale

**Alarm:** `-projection-stale` (>24h). `treat_missing_data = "breaching"` — no
data means the sync job is not running, which is worse than slow data.
**Parent sees:** fewer tutors, or "no match" where tutors exist. Never a wrong
tutor: freshness is a `WHERE` clause, so a stale row does not enter the pool.

---

## 1. How stale, and is the job running?

```sql
SELECT max(synced_at) AS newest,
       now() - max(synced_at) AS age,
       count(*) AS rows,
       count(*) FILTER (WHERE synced_at > now() - interval '6 hours') AS fresh,
       count(*) FILTER (WHERE synced_at > now() - interval '24 hours') AS usable
FROM tutor_match.tutor_projection;
```

`usable` is the number of tutors that can currently be recommended at all.

```sql
SELECT name, last_run_at, last_success_at, rows_processed, last_error
FROM tutor_match.sync_checkpoint;
```

- `last_run_at` recent, `last_success_at` old → the job runs and fails. §3.
- Both old → the job is not running. §2.

## 2. The job is not running

```bash
aws events describe-rule --name "$SVC-sync-projection"
aws lambda get-function-configuration --function-name "$SVC-scheduled" \
  --query '{state:State,reason:StateReason}'
```

Common causes, in order of likelihood:

| Cause | Check | Fix |
| --- | --- | --- |
| EventBridge rule disabled | `State` in `describe-rule` | `aws events enable-rule --name "$SVC-sync-projection"` |
| Function in `Failed` state | `StateReason` | Usually a VPC/ENI problem — see §5 |
| Concurrency exhausted by the worker | Lambda `Throttles` | The scheduled function has no reserved concurrency; raise the account limit or reserve some |

Force a run:

```bash
aws lambda invoke --function-name "$SVC-scheduled" \
  --payload '{"job":"sync_projection"}' /dev/stdout | jq
```

## 3. The job runs and fails

```bash
aws logs filter-log-events --log-group-name "/aws/lambda/$SVC-scheduled" \
  --start-time $(( ($(date +%s) - 7200) * 1000 )) \
  --filter-pattern '{ $.level = "ERROR" }' --query 'events[*].message' --output text
```

| Error | Cause | Action |
| --- | --- | --- |
| `aiomysql` connection refused / timeout | The website MySQL is unreachable | Check with the website team. The projection ages but stays usable for 24h. |
| `Access denied` | Credentials rotated upstream | Update `TMM_MYSQL_DSN` in Secrets Manager |
| `Unknown column` | The website schema changed | **Contract break.** §4 |
| `statement timeout` | The source query got slow | Reduce the page size; check for a missing index on `register.id` |

## 4. The website schema changed

`repositories/mysql_tutor.py::REGISTER_COLUMNS` is an explicit allowlist. A
renamed or dropped column breaks the read.

```bash
uv run pytest tests/contract/test_external_contracts.py -q
```

These tests pin the shape we depend on. Fix the mapping, add the new shape to
the contract test, deploy. Do **not** widen the allowlist to `SELECT *` — the
allowlist is what stops an unreviewed new column (a document number, a phone)
entering the projection.

## 5. VPC / ENI failure

The scheduled function is VPC-attached and reaches AWS services through the
endpoints in `endpoints.tf`. If those are missing or unhealthy, every call
hangs until the function times out — presenting as a slow, intermittent,
extremely confusing outage.

```bash
aws ec2 describe-vpc-endpoints \
  --filters "Name=tag:Service,Values=tutor-match-meta" \
  --query 'VpcEndpoints[*].{svc:ServiceName,state:State}'
```

Every one should be `available`. There is **no NAT Gateway** by design, so a
missing endpoint means no route at all — not a slower route.

## 6. While it is stale

Nothing to do. The design already handles it:

- rows older than `projection_aging_hours` (24h) never enter the candidate pool;
- the evidence guard refuses to quote a dimension whose data is stale;
- every decision records `oldest_source_data_at`;
- the pool cache recomputes freshness on read, so a row cached while FRESH
  cannot be served as FRESH after it ages out.

**Do not "fix" this by raising `projection_aging_hours`.** That does not make
the data fresher; it makes stale data recommendable, which is the failure the
threshold exists to prevent.

## 7. After recovery

```bash
# Reconcile: full checksum comparison, repairs drift the incremental sync missed.
aws lambda invoke --function-name "$SVC-scheduled" \
  --payload '{"job":"reconcile_projection"}' /dev/stdout | jq

# Invalidate cached pools so the fresh data is served immediately rather than
# up to 120s later.
psql "$TMM_POSTGRES_DSN" -c "DELETE FROM tutor_match.kv_entry WHERE key LIKE 'v1:pool:%';"
```

Confirm:

```bash
aws lambda invoke --function-name "$SVC-scheduled" \
  --payload '{"job":"check_staleness"}' /dev/stdout | jq
```
