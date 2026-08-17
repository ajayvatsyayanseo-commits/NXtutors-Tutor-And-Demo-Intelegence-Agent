# Security alarm

**Alarm:** `-injection-campaign` (>25 detections per 5 min), or any
`ContactHarvestAttempt` / abuse signal spike.
**Parent sees:** normal replies. Detection is not blocking.

Start from the right assumption: **a detection is not a breach.** Layer 3 —
schema-constrained output plus the output guard — is what actually holds. Layers
1 and 2 exist to make attacks *visible*, and this alarm is them working.

---

## 1. Is it one actor or many?

```bash
aws logs filter-log-events --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(( ($(date +%s) - 1800) * 1000 )) \
  --filter-pattern '{ $.injection_detected = * }' \
  --query 'events[*].message' --output text | jq -s 'group_by(.conversation_id_hash)
    | map({ref: .[0].conversation_id_hash, hits: length}) | sort_by(-.hits)'
```

- **One or two `conversation_ref`s** → a single actor probing. §2.
- **Many refs, similar payloads** → a campaign, likely automated. §3.
- **Many refs, varied payloads** → check for a false-positive source. §4.

## 2. A single actor

The abuse ladder should already be handling them
(`security/rate_limit.py::AbuseDetector`): signals accumulate into strikes,
strikes escalate `SOFT_THROTTLE → HARD_THROTTLE → COOLDOWN → HUMAN_REVIEW`.

```bash
aws logs filter-log-events --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(( ($(date +%s) - 1800) * 1000 )) \
  --filter-pattern '{ $.message = "abuse enforcement applied" }' \
  --query 'events[*].message' --output text
```

Nothing here while detections climb means the detector is not reaching the
escalation threshold. That is usually correct — the ladder is deliberately slow,
because a parent who sends a malformed message twice is confused, not hostile,
and blocking them is a worse outcome than serving one extra request.

To bite sooner, tighten the per-conversation limit rather than the ladder:

```bash
aws lambda update-function-configuration --function-name "$SVC-match-worker" \
  --environment "Variables={...,TMM_RATE_LIMIT_PER_CONVERSATION_PER_MINUTE=4}"
```

## 3. A campaign

```bash
# Squeeze the identity layer — this is the one that catches
# one-phone-many-conversations, which a per-conversation limit cannot.
aws lambda update-function-configuration --function-name "$SVC-ingress" \
  --environment "Variables={...,TMM_RATE_LIMIT_PER_IDENTITY_PER_MINUTE=5}"

# And the edge.
aws apigatewayv2 update-stage --api-id "$INGRESS_API_ID" --stage-name '$default' \
  --default-route-settings ThrottlingRateLimit=5,ThrottlingBurstLimit=10
```

If the traffic all originates from one network, narrow `ingress_allowed_cidrs`
to the legitimate caller's egress range — that removes the unauthenticated
flood entirely rather than rate-limiting it.

## 4. Rule out a false positive

The override patterns are deliberately broad: a false positive costs a
sanitisation marker, a false negative costs an injection. But a *sustained*
false positive is worth fixing.

```bash
aws logs filter-log-events --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(( ($(date +%s) - 1800) * 1000 )) \
  --filter-pattern '{ $.injection_detected = * }' \
  --query 'events[*].message' --output text | jq -r '.injection_detected[]' \
  | sort | uniq -c | sort -rn
```

| Pattern firing a lot | Plausible innocent source |
| --- | --- |
| `ranking_manipulation` | *"which tutor would you recommend first?"* |
| `exfiltration` | *"can you show me your list of tutors"* |
| `tool_coercion` | A parent asking about a computer-science syllabus |
| `system_prompt_spoof` | A pasted transcript with `Assistant:` prefixes |

A detection is **counted, not blocked**, so a false positive costs nothing
parent-visible. Narrow the pattern only with a test that pins both the true
positive and the newly-allowed innocent case.

## 5. Confirm nothing actually got through

This is the important part. Detections are noise; a successful injection is not.

```sql
-- Did the output guard reject anything? It runs on the finished bytes.
SELECT count(*) FROM tutor_match.match_decision
WHERE generated_at > now() - interval '2 hours' AND requires_human_review;
```

```bash
aws cloudwatch get-metric-statistics --namespace NXTutors/TutorMatchMeta \
  --metric-name OutputGuardRejected --start-time "$(date -u -d '2 hours ago' +%FT%TZ)" \
  --end-time "$(date -u +%FT%TZ)" --period 300 --statistics Sum
```

**`OutputGuardRejected = 0` during an injection campaign is the good outcome**:
the attempts were neutralised before generation and nothing invalid was
produced. A non-zero value means something reached the generator — read
`docs/runbooks/bad-prompt.md` and check whether the rejected content correlates
with the campaign.

Also verify no ranking moved:

```sql
-- Did a tutor's rank change without a data change? (poisoned-record check)
SELECT e->>'tutor_id' AS tutor, (e->>'rank')::int AS rank, count(*)
FROM tutor_match.match_decision d, jsonb_array_elements(d.shortlist) e
WHERE d.generated_at > now() - interval '2 hours'
GROUP BY 1,2 ORDER BY 3 DESC LIMIT 10;
```

A tutor suddenly dominating rank 1 → check their `profile_summary` for injected
text. `tests/security/test_output_guard.py::test_a_tutor_bio_cannot_promote_itself`
asserts this cannot work, but the evidence is worth having.

## 6. Poisoned RAG document

Detections carry a `chunk_id` prefix, which identifies the document:

```bash
... --filter-pattern '{ $.injection_detected = * }' | jq -r '.injection_detected[]' \
  | grep -oE '^[^:]+' | sort | uniq -c
```

```bash
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name,paused,actor,reason)
VALUES ('RAG_PAUSED',true,'$USER','poisoned corpus <INC>')
ON CONFLICT (name) DO UPDATE SET paused=true, actor=EXCLUDED.actor,
  reason=EXCLUDED.reason, changed_at=now();"
```

Lossless — retrieval is supplementary context only, and exact tutor filtering
is unaffected. Then supersede the document version.

## 7. Escalate when

- The output guard rejected content matching the campaign → a successful
  injection reached generation. Treat as a **breach investigation**.
- Any tutor contact detail appears in a parent-facing message → data leak,
  `docs/runbooks/privacy-incident.md`.
- Traffic is authenticated with a **valid** signature and is hostile → the
  signing key is compromised, `docs/runbooks/leaked-key.md`.
