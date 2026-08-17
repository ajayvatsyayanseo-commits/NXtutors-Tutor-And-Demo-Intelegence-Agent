# Chitragupta (memory) unavailable

**Metric:** `ChitraguptaFailures`; `degraded_sources` contains `chitragupta`
**Parent sees:** nothing. Matching is unaffected.

Memory is **audit and personalisation, never a matching input**. The default
local stack uses `NullMemory`, which is *always* unavailable — so every
end-to-end test in the suite is already proof the match path does not depend on
it.

---

## 1. Confirm the blast radius is what it should be

```bash
aws logs filter-log-events --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(( ($(date +%s) - 3600) * 1000 )) \
  --filter-pattern '{ $.degraded = "*chitragupta*" }' \
  --query 'events | length(@)'
```

Then confirm matching still succeeds during the outage:

```sql
SELECT count(*) FILTER (WHERE matched)     AS matched,
       count(*) FILTER (WHERE NOT matched) AS no_match
FROM tutor_match.match_decision
WHERE generated_at > now() - interval '1 hour'
  AND degraded_sources::text LIKE '%chitragupta%';
```

A normal matched/no-match ratio here means the degradation is working as
designed. **If matching is also failing, Chitragupta is not the cause** — look
elsewhere.

## 2. What is actually lost

| Capability | With memory | Without |
| --- | --- | --- |
| Recall of a prior preference (language, teaching style) | Fills a gap the parent has not restated | The parent is asked, or the field stays empty |
| Cross-conversation continuity | A returning family is recognised | Treated as new |
| Deed audit trail | Written live | Spooled to a WAL, replayed later |
| Hard filters, scoring, ranking, explanation | — | **Identical** |

Recalled facts are already filtered to `confidence >= 0.6 and not denied`, and
memory ranks **below** anything this conversation established. A remembered
"Class 9" from last year must never overwrite a parent saying "Class 10" today
— so losing memory removes a convenience, never a correctness guarantee.

## 3. Are deeds being spooled?

Deeds spool to a per-container WAL at `/tmp/tmm-memory.wal`. `/tmp` is the only
writable path in Lambda and it is **per container**, so the WAL is best-effort
by construction — a container that recycles takes its unspooled deeds with it.

That is an accepted trade: the alternative (a durable queue for audit events)
costs more than the events are worth, given they are not a matching input.

```bash
aws logs filter-log-events --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(( ($(date +%s) - 3600) * 1000 )) \
  --filter-pattern '{ $.message = "*wal*" }' --query 'events[*].message' --output text
```

## 4. If it is a long outage

```bash
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name,paused,actor,reason)
VALUES ('MEMORY_WRITES_PAUSED',true,'$USER','chitragupta outage <INC>')
ON CONFLICT (name) DO UPDATE SET paused=true, actor=EXCLUDED.actor,
  reason=EXCLUDED.reason, changed_at=now();"
```

This stops the attempt entirely rather than paying a 3-second timeout per turn.
`TMM_CHITRAGUPTA_TIMEOUT_SECONDS` is 3s and every call is best-effort, so the
worst case without the switch is 3s added to a turn — noticeable against Lead
Intake's 2s budget, which is why the switch exists.

Alternatively disable it outright:

```bash
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --environment "Variables={...,TMM_CHITRAGUPTA_ENABLED=false}"
```

`build_memory` then returns `NullMemory` and no call is attempted at all.

## 5. Recovery

```bash
psql "$TMM_POSTGRES_DSN" -c "
UPDATE tutor_match.kill_switch SET paused=false, actor='$USER',
  reason='chitragupta recovered <INC>', changed_at=now()
WHERE name='MEMORY_WRITES_PAUSED';"
```

Spooled deeds replay on the next relay run. **Some will have been lost** to
container recycling — that is expected and documented, not an incident. If the
audit trail matters for a specific conversation, `match_decision` is the
authoritative record and it never depended on Chitragupta.

## 6. Do not

- **Do not** make memory a hard dependency to "fix" this. The whole design
  rests on it being optional.
- **Do not** manufacture a past preference to fill a gap. `_recall` returning
  nothing means "nothing known", never "no preference" — inventing one is
  exactly the fabrication this service refuses.
