# Provider rate limiting (429) or outage

**Alarms:** `CircuitOpen`, `-llm-spend-rate` (retries cost money), `-match-queue-age`
**Parent sees:** normal replies. Matching is deterministic; the model is optional.

---

## 1. Confirm the circuit did its job

The breaker opens after 5 consecutive failures and half-opens after 30s with a
single probe. Without it, every turn pays a 12-second timeout and the queue
backs up — which is the real damage from a provider outage, not the missing
model output.

```bash
aws logs filter-log-events --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(( ($(date +%s) - 1800) * 1000 )) \
  --filter-pattern '{ $.message = "llm call failed" }' \
  --query 'events[*].message' --output text | head -20
```

Look at `error_code` and `retry_count`:

| `error_code` | Meaning | Retried? |
| --- | --- | --- |
| `LLMRateLimited` | 429 | Yes, jittered backoff |
| `LLMTimeout` | Timeout, connection error, or upstream 5xx | Yes |
| `LLMSchemaViolation` | Malformed or truncated output | **No** — deterministic; retrying repeats it |
| `LLMCircuitOpen` | Shedding | No call made |

## 2. Decide: ride it out, or pause

**Ride it out** if queue age is stable. The breaker is doing its job and
matching is unaffected.

**Pause** if queue age is climbing or the spend alarm has fired — retries are
not free:

```bash
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name,paused,actor,reason)
VALUES ('LLM_PAUSED',true,'$USER','provider outage <INC>')
ON CONFLICT (name) DO UPDATE SET paused=true, actor=EXCLUDED.actor,
  reason=EXCLUDED.reason, changed_at=now();"
```

`LLM_PAUSED` refuses **before** the call, so a paused provider costs nothing at
all — no timeout, no retry, no latency.

## 3. What actually degrades

| Capability | With the model | Paused |
| --- | --- | --- |
| Well-formed message ("class 10 cbse maths sector 57 after 6:30 home 900") | Parsed deterministically — **the model was never called** | Identical |
| Ambiguous message ("science tutor for my son") | Model resolves it | One clarifying question |
| Hard filters, 8 evaluators, ranking | Deterministic | Identical |
| Explanation wording | Model phrasing | Template over the same guard-approved evidence |

The parent-visible difference is one extra question on the minority of messages
the parsers cannot fully resolve.

## 4. Sustained 429s that are not an outage

If the provider is healthy but rate-limiting *us*, we are sending too much.
Check calls per completed match:

```sql
SELECT date_trunc('hour', created_at) AS hour,
       count(*) AS calls,
       count(DISTINCT conversation_hash) AS conversations,
       round(count(*)::numeric / NULLIF(count(DISTINCT conversation_hash),0), 2) AS per_conv
FROM tutor_match.llm_usage
WHERE created_at > now() - interval '6 hours'
GROUP BY 1 ORDER BY 1;
```

`per_conv` well above 2 means a loop, not demand → `docs/runbooks/high-llm-spend.md`.

Tighten the pre-spend limiter as a stopgap:

```bash
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --environment "Variables={...,TMM_RATE_LIMIT_LLM_PER_CONVERSATION_PER_MINUTE=2}"
```

## 5. Recovery

```bash
psql "$TMM_POSTGRES_DSN" -c "
UPDATE tutor_match.kill_switch SET paused=false, actor='$USER',
  reason='provider recovered <INC>', changed_at=now() WHERE name='LLM_PAUSED';"
```

The breaker recovers on its own: after `reset_seconds` it half-opens and lets
one probe through rather than reopening blindly.

## 6. Other providers

Same shape, different switch:

| Provider | Detection | Response |
| --- | --- | --- |
| Chitragupta | `ChitraguptaFailures` | `docs/runbooks/chitragupta-unavailable.md` |
| Website (Laravel) | `WebsiteFailures` | `WEBSITE_WRITEBACK_PAUSED` — holds demo requests durably |
| Geocoder | `GeocodeCalls` flat, distances missing | Falls back to stored coordinates automatically; no action |
| Meta Cloud API | `-outbound-dlq-not-empty` | `OUTBOUND_PAUSED` — holds messages in the queue |

Every one of these degrades rather than failing, and `degraded_sources` on the
decision records which were down for any given match.
