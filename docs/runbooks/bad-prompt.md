# Bad prompt

**Alarms:** `-output-guard-rejections`, `-fabrication-violations`
**Parent sees:** the safe fallback — *"Let me get one of our coordinators to
look at this…"* — instead of a shortlist.

**The whole point of this runbook: you do not need an application deploy.**
A prompt is versioned, checksummed and pinnable at runtime.

---

## 1. What is being rejected?

```bash
aws logs filter-log-events --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(( ($(date +%s) - 1800) * 1000 )) \
  --filter-pattern '{ $.message = "outbound message failed validation*" }' \
  --query 'events[*].message' --output text | head -40
```

Each line carries `violations` and a bounded, non-identifying `detail`. The
message itself is never logged.

| Violation | Meaning |
| --- | --- |
| `unsupported_guarantee` | The model promised a result. |
| `unknown_tutor_referenced` | A hallucinated profile link. |
| `unauthorised_fee` | A number not on any shortlist entry's fee band. |
| `internal_field_leak` | A score, a `tutor_id`, or `cand_N`. |
| `prompt_text_leak` | The prompt itself came back. |
| `pii_leak` | A phone number or email in the message. |
| `too_long` | Over 1400 chars — usually a template loop. |

## 2. Which prompt version is live?

```bash
curl -s -H "X-NXTUTORS-INTERNAL-SECRET: $TMM_INTERNAL_SECRET" \
  "$INTERNAL_URL/internal/v1/version" | jq '.prompt_versions'
# {"extraction":"v1","explanation":"v1","clarification":"v1"}
```

Compare against the last known-good deploy. If the versions are identical, the
prompt is not the cause — go to §5.

## 3. Roll the prompt back — no deploy

```bash
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --environment "Variables={$(aws lambda get-function-configuration \
      --function-name "$SVC-match-worker" \
      --query 'Environment.Variables' --output json | jq -r \
      'to_entries|map("\(.key)=\(.value)")|join(",")'),TMM_PROMPT_PINS=explanation=v1}"
```

Format: `TMM_PROMPT_PINS=explanation=v1,extraction=v2` — comma-separated,
`prompt_id=version`.

**A pin naming a version this build does not contain fails `/ready`**, loudly,
rather than silently serving something else. Verify:

```bash
curl -s "$INTERNAL_URL/internal/v1/ready" | jq
# {"status":"ready"}   → the pin resolved
# 503 with prompt_pin_unresolvable:... → the version does not exist in this build
```

Takes effect on the next cold start; force one:

```bash
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --description "pin explanation=v1 $(date -u +%FT%TZ)"
```

## 4. If no earlier version exists

There is nothing to pin to. Disable the model path instead — the deterministic
templates over guard-approved evidence still produce a correct shortlist:

```bash
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name, paused, actor, reason)
VALUES ('LLM_PAUSED', true, '$USER', 'bad prompt, no rollback target <INC>')
ON CONFLICT (name) DO UPDATE SET paused=true, actor=EXCLUDED.actor,
  reason=EXCLUDED.reason, changed_at=now();"
```

## 5. Not the prompt

If prompt versions are unchanged, the cause is upstream of generation:

- **`unknown_tutor_referenced` with unchanged prompts** → the link resolver or
  the projection. Check `docs/runbooks/website-sync-stale.md`.
- **`unauthorised_fee`** → a fee label changed shape in the projection.
- **`pii_leak`** → a template started echoing the parent's message. Check the
  most recent application deploy; `docs/runbooks/bad-deployment.md`.

## 6. Fixing it properly

A prompt change is a production change. It needs:

1. A **new version** in `prompts/registry.py` — never an edit in place. The
   checksum is computed from the text, so an edit that forgets to bump the
   version is caught by `tests/security/test_prompt_hygiene`-style assertions.
2. `model_compatibility` listing the models it was evaluated against.
3. `MUST_CARRY_INJECTION_CLAUSE` still satisfied — the untrusted-data clause is
   asserted for every registered prompt by
   `tests/security/test_invariants.py::test_every_prompt_declares_the_data_clause`.
4. `make check` green.
5. Deploy with the **old** version still pinned, then unpin to roll forward.
   That way the rollback path is proven before you need it.

---

## Why the parent never saw the bad text

`orchestration/output_guard.py` runs on the assembled message, after every
template and model, with no path back to the generator. A failure substitutes
`SAFE_FALLBACK` and counts the incident — it is never patched up and sent.
`test_the_fallback_itself_passes_validation` ensures the fallback cannot itself
trip the guard and loop.
