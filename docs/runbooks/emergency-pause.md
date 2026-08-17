# Emergency pause

**When:** you need something to stop **now** and a deploy is too slow.

Seven independent switches, each with a documented safe behaviour. Read at
most `TMM_KILL_SWITCH_TTL_SECONDS` (10s) old across every warm container.

---

## Flip a switch

```bash
SWITCH=LLM_PAUSED
REASON="spend alarm INC-1234"

psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name, paused, actor, reason)
VALUES ('$SWITCH', true, '$USER', '$REASON')
ON CONFLICT (name) DO UPDATE SET
  paused = true, actor = EXCLUDED.actor,
  reason = EXCLUDED.reason, changed_at = now();"
```

`actor` and `reason` are **NOT NULL**. An unexplained pause during an incident
is nearly as bad as no pause at all — the next person on shift has to be able
to tell "someone is fixing this" from "someone forgot".

## Choose the right one

| Switch | Use when | Behaviour | Loses work |
| --- | --- | --- | --- |
| `MATCHING_PAUSED` | We are broken; Lead Intake should take over | Decline every handoff. **No dedup key is consumed**, so redeliveries after unpause still work | No |
| `LLM_PAUSED` | Provider outage, spend alarm, bad prompt with no rollback target | Deterministic extraction and scoring; one clarifying question where a model would have resolved ambiguity | No |
| `RAG_PAUSED` | Poisoned corpus, vector search slow | Skip supplementary context. **Exact tutor filtering is unaffected** | No |
| `WEBSITE_WRITEBACK_PAUSED` | Laravel API failing or returning wrong data | Demo requests held in the outbox | **Yes** |
| `OUTBOUND_PAUSED` | Duplicate sends, wrong content going out, Meta incident | Messages held in the queue; the decision is already persisted so nothing is regenerated | **Yes** |
| `MEMORY_WRITES_PAUSED` | Chitragupta failing or writing wrong data | Deeds spool to the WAL | No |
| `AUTO_DEMO_PAUSED` | Demo bookings landing wrong | A coordinator confirms instead | **Yes** |

## Verify it took effect

```bash
curl -s -H "X-NXTUTORS-INTERNAL-SECRET: $TMM_INTERNAL_SECRET" \
  "$INTERNAL_URL/internal/v1/version" | jq '{kill_switches, holding_work}'
```

`holding_work` lists the switches currently **holding real work** — the ones
someone must remember to unpause. A lossless degradation can sit paused for a
week; a held outbox cannot.

## Unpause

```bash
psql "$TMM_POSTGRES_DSN" -c "
UPDATE tutor_match.kill_switch
SET paused = false, actor = '$USER', reason = 'resolved INC-1234', changed_at = now()
WHERE name = '$SWITCH';"
```

Then drain whatever was held:

```bash
# Held outbound messages.
aws lambda invoke --function-name "$SVC-scheduled" \
  --payload '{"job":"relay_outbox"}' /dev/stdout | jq

# Spooled memory events.
aws lambda invoke --function-name "$SVC-scheduled" \
  --payload '{"job":"relay_outbox"}' /dev/stdout
```

Watch for a thundering herd: a long `OUTBOUND_PAUSED` releases a large batch at
once. The relay is bounded to `BATCH_SIZE = 25` per run precisely so it drains
in steps rather than all at once.

## If PostgreSQL is unreachable

The switches live in PostgreSQL, so you cannot flip them. Stop consumption at
the event source instead:

```bash
UUID=$(aws lambda list-event-source-mappings --function-name "$SVC-match-worker" \
  --query 'EventSourceMappings[0].UUID' --output text)
aws lambda update-event-source-mapping --uuid "$UUID" --no-enabled
```

Messages accumulate in SQS (4-day retention). **Write down the time** — you
need it to reason about which parents were affected.

Re-enable at reduced concurrency, never at full:

```bash
aws lambda put-function-concurrency --function-name "$SVC-match-worker" \
  --reserved-concurrent-executions 5
aws lambda update-event-source-mapping --uuid "$UUID" --enabled
```

## The `-kill-switch-active` alarm

Fires when a switch has been held for roughly an hour. It is not telling you
something is broken — it is asking whether the pause is still intentional. The
most common real incident it catches is a lossy switch left on after the
original problem was fixed.

## Before you go off shift

```bash
curl -s -H "X-NXTUTORS-INTERNAL-SECRET: $TMM_INTERNAL_SECRET" \
  "$INTERNAL_URL/internal/v1/version" | jq '.holding_work'
```

Empty array, or a written handover naming each switch, why it is on, and what
has to be true to turn it off.
