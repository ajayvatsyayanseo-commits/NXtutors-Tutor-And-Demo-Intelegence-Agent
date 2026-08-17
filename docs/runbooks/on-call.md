# On-call runbook — tutor-match-meta

Every alarm below maps to a real parent-visible symptom. Fix the symptom first,
then the cause.

---

## `match-dlq-not-empty`

**Symptom:** parents sent a message and got no reply.

1. Inspect: `aws sqs receive-message --queue-url $MATCH_DLQ --max-number-of-messages 10`
2. Read the `trace_id` from the body and pull the logs:
   `aws logs filter-log-events --log-group-name /aws/lambda/tutor-match-meta-$ENV-match-worker --filter-pattern "{ $.trace_id = \"<id>\" }"`
3. Classify:
   - **Malformed envelope** — a producer bug. Fix upstream; the message will
     never succeed on replay. Delete it and tell the producing team.
   - **Transient dependency** — redrive once the dependency is healthy:
     `aws sqs start-message-move-task --source-arn $MATCH_DLQ_ARN`
   - **Our bug** — fix, deploy, then redrive.

Redriving is safe: the idempotency table means a message that was already fully
processed is a no-op on replay.

---

## `outbound-dlq-not-empty`

**Symptom:** a shortlist was generated but never delivered.

Delivery is separate from matching by design, so the decision is already
persisted. Check `match_decision` for the conversation, confirm the shortlist
exists, then redrive. **Never re-run the match to "regenerate" the reply** — the
parent would receive a different set of tutors than the one we decided on.

Permanent failures (Meta error 131047, closed 24-hour window) are not retryable.
Hand those to a coordinator to reach the parent another way.

---

## `match-queue-age` above two minutes

1. Is the worker throttled? `Throttles` on the match-worker function. If yes,
   `match_worker_reserved_concurrency` is the ceiling — raising it **requires
   raising the RDS Proxy connection limit at the same time**, or the database
   becomes the next bottleneck.
2. Is a dependency slow? Check `LlmLatencyMs` and `DbLatencyMs`. An LLM stall
   should trip the circuit breaker within five failures and fall back to the
   deterministic path; if it has not, check `CircuitOpen`.
3. Is one conversation hot-looping? `MessageGroupId` serialises a conversation,
   so a single misbehaving conversation cannot block others — but it can block
   itself. Check the per-conversation rate limiter.

---

## `fabrication-violations` above zero

**Treat as a correctness incident, not a warning.** The evidence guard blocked a
claim that was not backed by data. Nothing wrong reached the parent — the guard
is why — but something tried.

1. Find the trace and read `guard_refusals` on the decision.
2. The usual cause is a new template or evaluator citing a dimension whose
   `data_quality` is MISSING/INSUFFICIENT/STALE.
3. Fix the claim source. **Do not relax the guard.**

---

## `projection-stale`

**Symptom:** we may be recommending tutors who deactivated.

Tutors older than `projection_aging_hours` are already excluded from matching, so
the parent-facing risk is a smaller pool, not a wrong recommendation.

1. Check `sync_checkpoint` for `last_success_at` and `last_error`.
2. Confirm MySQL reachability and that the read-only grant still covers the
   seven allowlisted tables.
3. Run a manual sync:
   `aws lambda invoke --function-name tutor-match-meta-$ENV-scheduled --payload '{"job":"sync_projection"}' /dev/stdout`
4. If the incremental sync is healthy but drift persists, run
   `{"job":"reconcile_projection"}`.

Alarm treats missing data as breaching: no metric means the job is not running
at all, which is worse than a stale value.

---

## Chitragupta unavailable

Memory is non-blocking by design. Matches continue; `degraded_sources` records
`chitragupta` on each decision and events spool to the per-container WAL in
`/tmp`.

Lambda `/tmp` does not survive a container recycle, so a long outage loses
spooled deeds. That is an accepted trade-off — memory events are an audit and
personalisation aid, not part of the matching decision. If the outage exceeds an
hour, tell the Chitragupta owners that backfill will be incomplete.

---

## Suspected credential compromise

1. Rotate in Secrets Manager. Lambda picks up the new value on the next cold
   start; force one with a no-op configuration update.
2. `TMM_INGRESS_SIGNING_KEY` is shared with the calling agents — rotate on both
   sides in the same window or ingress starts returning 401.
3. `TMM_HASH_PEPPER` is different: rotating it **changes every pseudonym**, so
   historical logs and metrics stop correlating. Only rotate on actual
   compromise, and record the rotation time so analysts can bridge the gap.
