# High model spend

**Alarms:** `-llm-spend-rate`, `-llm-budget-exhausted`
**Parent sees:** nothing yet. This is a cost incident, not an outage — and the
response must keep it that way.

---

## 1. Stop the bleeding (30 seconds)

```bash
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name, paused, actor, reason)
VALUES ('LLM_PAUSED', true, '$USER', 'spend alarm <INC>')
ON CONFLICT (name) DO UPDATE SET paused=true, actor=EXCLUDED.actor,
  reason=EXCLUDED.reason, changed_at=now();"
```

`LLM_PAUSED` degrades to deterministic extraction and scoring. The well-formed
majority of messages are matched with **zero** model calls; ambiguous ones get
one clarifying question instead of a guess. **The service keeps working.** This
is lossless — you can leave it paused while you investigate.

## 2. Is it demand, or a loop?

Almost always a loop. Check before raising any ceiling:

```bash
aws cloudwatch get-metric-statistics --namespace NXTutors/TutorMatchMeta \
  --metric-name DuplicateEvents --start-time "$(date -u -d '2 hours ago' +%FT%TZ)" \
  --end-time "$(date -u +%FT%TZ)" --period 300 --statistics Sum

aws cloudwatch get-metric-statistics --namespace NXTutors/TutorMatchMeta \
  --metric-name LlmCalls --start-time "$(date -u -d '2 hours ago' +%FT%TZ)" \
  --end-time "$(date -u +%FT%TZ)" --period 300 --statistics Sum
```

**Calls per completed match is the diagnostic.** The ceiling is
`TMM_LLM_MAX_CALLS_PER_TURN = 2`; a healthy service sits near 1.

```sql
-- Spend by purpose and model over the last hour.
SELECT purpose, model, count(*) AS calls,
       sum(prompt_tokens) AS in_tok, sum(completion_tokens) AS out_tok,
       sum(cost_micros)/1e6 AS usd
FROM tutor_match.llm_usage
WHERE created_at > now() - interval '1 hour'
GROUP BY 1,2 ORDER BY usd DESC;

-- The conversations responsible. `conversation_hash`, never a phone number.
SELECT conversation_hash, count(*) AS calls, sum(cost_micros)/1e6 AS usd
FROM tutor_match.llm_usage
WHERE created_at > now() - interval '1 hour'
GROUP BY 1 ORDER BY calls DESC LIMIT 20;
```

| Pattern | Cause | Fix |
| --- | --- | --- |
| A few conversations with 50+ calls | Redelivery loop, or a caller retrying | §3 |
| `escalation` model dominating | Escalation trigger too loose | §4 |
| Calls evenly spread, volume genuinely up | Real demand | §5 |
| `retry_count > 0` on most rows | Provider is degraded; retries are the cost | `docs/runbooks/provider-429.md` |

## 3. A redelivery loop

```bash
# Is the caller retrying because we are too slow?
aws cloudwatch get-metric-statistics --namespace AWS/ApiGateway \
  --metric-name Latency --dimensions Name=ApiId,Value=$INTERNAL_API_ID \
  --start-time "$(date -u -d '1 hour ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
  --period 300 --extended-statistics p95
```

Lead Intake times out at 2 seconds. If our p95 is near or above that, **they
retry, and one message becomes three**. That is the most common cause of this
alarm and the fix is latency, not budget.

If one conversation is looping, the per-conversation LLM limit should already
be shedding it (`TMM_RATE_LIMIT_LLM_PER_CONVERSATION_PER_MINUTE = 4`). Confirm
it is actually configured:

```bash
aws lambda get-function-configuration --function-name "$SVC-match-worker" \
  --query 'Environment.Variables' | grep -i rate_limit
```

## 4. Escalation firing too often

Escalation is capped at `TMM_LLM_MAX_ESCALATIONS_PER_CONVERSATION = 1`, so a
spike means many *conversations* are ambiguous — usually a change in inbound
phrasing, or a deterministic parser regression.

```sql
SELECT date_trunc('hour', created_at) AS hour, count(*)
FROM tutor_match.llm_usage
WHERE purpose = 'ambiguous_requirement_escalation'
  AND created_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 1;
```

A step change aligned to a deploy → roll back the application
(`docs/runbooks/bad-deployment.md`). Otherwise route escalation to the cheap
model as a stopgap:

```bash
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --environment "Variables={...,TMM_MODEL_ESCALATION=gpt-4o-mini}"
```

## 5. Genuine demand

Recalculate the budget from `docs/cost-architecture.md` at the new volume, then
raise `llm_monthly_budget_micros` in Terraform — **through a pull request**, so
the new ceiling is reviewed rather than typed into a console during an
incident.

## 6. Unpause

```bash
psql "$TMM_POSTGRES_DSN" -c "
UPDATE tutor_match.kill_switch
SET paused=false, actor='$USER', reason='resolved <INC>', changed_at=now()
WHERE name='LLM_PAUSED';"
```

Watch `LlmCalls` and `LlmCostMicros` for 15 minutes before closing.

---

## Prevention already in place

Four independent ceilings, all checked **before** the provider call
(`integrations/llm/routing.py::GuardedProvider`):

| Ceiling | Default |
| --- | --- |
| Tokens per conversation | 40,000 |
| Calls per turn | 2 |
| Calls per conversation | 12 |
| Escalations per conversation | 1 |

Plus a per-conversation rate limit checked before the spend, and a circuit
breaker that sheds calls after 5 failures rather than paying for 12-second
timeouts.
