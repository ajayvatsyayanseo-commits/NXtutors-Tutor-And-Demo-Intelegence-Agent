# Leaked credential

**Trigger:** gitleaks in CI, a provider anomaly alert, a key found in a log or
a ticket, or a laptop loss.

**Assume compromise.** "Probably not exposed" is not a state you can act on.

---

## 1. Rotate first, investigate second

Order matters: every minute of investigation is a minute the key is live.

### OpenAI

```bash
# 1. Create a NEW key in the OpenAI dashboard (do not revoke the old one yet —
#    revoking first is a self-inflicted outage).
# 2. Update Secrets Manager.
aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" \
  --secret-string "$(aws secretsmanager get-secret-value --secret-id "$SECRET_NAME" \
      --query SecretString --output text | jq -c '.TMM_OPENAI_API_KEY = "sk-NEW"')"

# 3. Force cold starts so warm containers pick it up.
for fn in match-worker internal-api scheduled; do
  aws lambda update-function-configuration --function-name "$SVC-$fn" \
    --description "key rotation $(date -u +%FT%TZ)"
done

# 4. Confirm the new key works, THEN revoke the old one in the dashboard.
```

If you cannot rotate immediately, pause spend instead:

```bash
psql "$TMM_POSTGRES_DSN" -c "
INSERT INTO tutor_match.kill_switch (name,paused,actor,reason)
VALUES ('LLM_PAUSED',true,'$USER','key rotation <INC>')
ON CONFLICT (name) DO UPDATE SET paused=true, actor=EXCLUDED.actor,
  reason=EXCLUDED.reason, changed_at=now();"
```

Matching continues deterministically, so this is not an outage.

### Ingress signing key

**Coordinate with the caller.** Rotating unilaterally breaks every inbound
request — the caller signs with the old key and gets 401s.

1. Agree a cutover window with Lead Intake.
2. Update both sides in the same window.
3. Watch the 401 rate:

```bash
aws logs filter-log-events --log-group-name "/aws/apigateway/$SVC" \
  --start-time $(( ($(date +%s) - 600) * 1000 )) \
  --filter-pattern '{ $.status = 401 }' --query 'events | length(@)'
```

### Internal secret

Same coordination requirement: it is what Lead Intake sends as
`X-NXTUTORS-INTERNAL-SECRET`. `_authorised` fails **closed**, so a mismatch is
a total handoff outage rather than an open door — safe, but visible.

### Hash pepper

**Do not rotate casually.** The pepper is what makes pseudonyms stable.
Rotating it re-keys every `conversation_ref` and `phone_hash`, which:

- breaks continuity of every in-flight conversation's memory recall;
- makes historical analytics discontinuous;
- makes existing rate-limit buckets unreachable (they refill and expire, so
  this one self-heals within the hour).

Rotate only if the pepper itself leaked. If it did, that is a **privacy**
incident too — an attacker with the pepper can brute-force a 10-digit phone
number in minutes. Run `docs/runbooks/privacy-incident.md` in parallel.

### Meta access token

Rotate in the Meta App dashboard, update Secrets Manager, force cold starts.
Held messages in the outbox are replayed on recovery.

### Database credentials

The service uses IAM authentication (`rds-db:connect`), so there is usually no
password to rotate. If a static DSN is in use, rotate it in Secrets Manager and
force cold starts — the connection pool reconnects.

## 2. Scope the exposure

```bash
# Was it ever committed?
git log --all -S '<key-fragment>' --oneline

# Is it in a log?
aws logs start-query --log-group-name "/aws/lambda/$SVC-match-worker" \
  --start-time $(date -u -d '30 days ago' +%s) --end-time $(date -u +%s) \
  --query-string 'fields @timestamp, @message | filter @message like /<fragment>/ | limit 20'
```

**This should return nothing.** Every credential is a `SecretStr`, so an
accidental `repr` prints `**********`, and Terraform references the Secrets
Manager secret **by name** so no value enters state. A hit means one of those
protections was bypassed — find out how, and add a test.

## 3. Look for use

| Credential | Where to look |
| --- | --- |
| OpenAI | Usage dashboard — requests from unexpected IPs, or a spend spike |
| Meta | App dashboard message log — sends we did not make |
| AWS | CloudTrail for the affected principal |
| Ingress key | Our own 202 rate versus Lead Intake's send count — a gap means someone else is calling us |

## 4. If the key was in git history

Rotation is mandatory and sufficient for the *live* risk; history rewriting is
about the *residual* risk.

```bash
# Confirm the current tree is clean.
gitleaks detect --source . --no-git
```

Rewriting published history (`git filter-repo`) requires every clone to be
re-cloned. Decide with the repository owner. **Rotation is what makes the leak
harmless; rewriting only removes the artefact.**

## 5. Prevent

1. CI already runs gitleaks and greps `.env.example` for credential shapes.
   Confirm the leak path was not covered, and cover it.
2. Never `print()` or log a `SecretStr`. `settings.py` keeps every credential
   in one, precisely so an accidental interpolation is safe.
3. **Known open item:** `.env` in the working tree holds real credentials for a
   live RDS instance and an OpenAI key. It is gitignored, but it is on disk on
   a developer machine. Recorded in `docs/production-readiness-final.md` §14.

## 6. Record

In the incident log: which credential, when rotated, by whom, what evidence of
use was found, what the exposure window was. Never the value, and never a
fragment long enough to be useful.
