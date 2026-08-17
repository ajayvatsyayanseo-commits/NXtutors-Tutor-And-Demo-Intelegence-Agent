# Runbooks

Every alarm in `infra/terraform/` names one of these in its
`alarm_description`, so an on-call engineer is never guessing at 3am.

## Conventions

Each runbook is written to be **executed, not read for background**. Commands
are copy-pasteable; `$ENV` is `dev|staging|production`.

Set these once per session:

```bash
export ENV=production
export AWS_REGION=ap-south-1
export SVC="tutor-match-meta-$ENV"
export INTERNAL_URL=$(terraform -chdir=infra/terraform output -raw internal_api_url)
export MATCH_DLQ=$(terraform -chdir=infra/terraform output -raw match_dlq_url)
```

Every runbook opens with the same two steps, because they are right almost
every time:

1. **What does a parent see right now?** Fix that first.
2. **What changed?** `GET /internal/v1/version` — app, SHA, schema, policy and
   prompt versions are reported separately because they roll back
   independently.

```bash
curl -s -H "X-NXTUTORS-INTERNAL-SECRET: $TMM_INTERNAL_SECRET" \
  "$INTERNAL_URL/internal/v1/version" | jq
```

## The seven emergency controls

Flip a kill switch when you need to stop something *now*, without a deploy.
Each has a documented safe behaviour (`config/kill_switches.py`), and `actor`
and `reason` are mandatory — an unexplained pause during an incident is nearly
as bad as no pause at all.

```bash
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name, paused, actor, reason)
VALUES ('LLM_PAUSED', true, '$USER', 'spend alarm INC-1234')
ON CONFLICT (name) DO UPDATE SET
  paused = EXCLUDED.paused, actor = EXCLUDED.actor,
  reason = EXCLUDED.reason, changed_at = now();"
```

Takes effect within `TMM_KILL_SWITCH_TTL_SECONDS` (10s) across all warm
containers.

| Switch | Effect | Loses work? |
| --- | --- | --- |
| `MATCHING_PAUSED` | Decline to Lead Intake; they keep answering | No |
| `LLM_PAUSED` | Deterministic matching only | No |
| `RAG_PAUSED` | Skip supplementary context | No |
| `WEBSITE_WRITEBACK_PAUSED` | Hold demo requests in the outbox | **Yes — must unpause** |
| `OUTBOUND_PAUSED` | Hold messages in the queue | **Yes — must unpause** |
| `MEMORY_WRITES_PAUSED` | Spool deeds to the WAL | No |
| `AUTO_DEMO_PAUSED` | Escalate demo booking to a human | **Yes — must unpause** |

**Before you go off shift**, list anything still holding work:

```bash
curl -s -H "X-NXTUTORS-INTERNAL-SECRET: $TMM_INTERNAL_SECRET" \
  "$INTERNAL_URL/internal/v1/version" | jq '.holding_work'
```

## Index

| Runbook | Alarm |
| --- | --- |
| [db-outage.md](db-outage.md) | `-proxy-connections`, `-worker-errors` |
| [provider-429.md](provider-429.md) | `-llm-spend-rate`, `CircuitOpen` |
| [queue-backlog.md](queue-backlog.md) | `-match-queue-age`, `-optimistic-lock-conflicts` |
| [dlq-replay.md](dlq-replay.md) | `-match-dlq-not-empty`, `-outbound-dlq-not-empty`, `-outbox-dead` |
| [bad-scoring-policy.md](bad-scoring-policy.md) | `-human-handoff-spike`, `-no-match-rate` |
| [bad-prompt.md](bad-prompt.md) | `-output-guard-rejections`, `-fabrication-violations` |
| [bad-deployment.md](bad-deployment.md) | smoke-test failure, `-ingress-5xx` |
| [privacy-incident.md](privacy-incident.md) | any confirmed leak |
| [leaked-key.md](leaked-key.md) | gitleaks, provider anomaly |
| [website-sync-stale.md](website-sync-stale.md) | `-projection-stale` |
| [duplicate-sends.md](duplicate-sends.md) | `-duplicate-spike` |
| [high-llm-spend.md](high-llm-spend.md) | `-llm-spend-rate`, `-llm-budget-exhausted` |
| [high-no-match.md](high-no-match.md) | `-no-match-rate` |
| [chitragupta-unavailable.md](chitragupta-unavailable.md) | `ChitraguptaFailures` |
| [emergency-pause.md](emergency-pause.md) | operator judgement |
| [security-alarm.md](security-alarm.md) | `-injection-campaign` |
| [rollback.md](rollback.md) | any rollback trigger |
| [migrations.md](migrations.md) | deploy-time |
| [on-call.md](on-call.md) | general index |
