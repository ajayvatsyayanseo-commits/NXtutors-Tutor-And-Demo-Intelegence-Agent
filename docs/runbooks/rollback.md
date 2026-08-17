# Rollback

## What rolls back

Deployment artifacts are immutable and keyed by git SHA, so a rollback is a
redeploy of a known-good object rather than a rebuild.

```
gh workflow run rollback.yml -f environment=production -f target_sha=<good-sha>
```

This updates all four Lambda functions and runs the smoke test.

## What does not roll back automatically

**Database migrations.** Migrations are additive by policy and gated by
`scripts/check_migration_safety.py`, so the previous code runs against the
current schema without change. Rolling a schema back is a separate, manual,
human-approved operation — an automatic destructive migration during an incident
is how a rollback becomes data loss.

**Scoring policy.** A policy is a versioned YAML document, so reverting one is a
normal code change through the same pipeline. Every `match_decision` records
`policy_id`, `policy_version` and `policy_checksum`, so you can identify exactly
which decisions used the bad policy and re-run them if needed.

## Verifying a rollback

1. Smoke test passes (the workflow runs it).
2. `MatchRequests` recovers to its baseline rate.
3. `FabricationViolations` returns to zero.
4. Spot-check one real conversation's shortlist against the projection.

## Rolling back a policy only

Faster than a code rollback when only ranking is wrong: revert the YAML, deploy,
and confirm new decisions carry the previous `policy_checksum`.
