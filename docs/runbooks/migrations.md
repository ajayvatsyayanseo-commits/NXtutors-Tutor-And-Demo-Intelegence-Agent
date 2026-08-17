# Migrations

## Rules

1. **Additive only in the automated pipeline.** New tables, new nullable
   columns, new indexes. Anything that drops or narrows is manual.
2. **Two-phase for a rename.** Add the new column, backfill, deploy code that
   writes both and reads the new one, then drop the old one in a later release.
3. **Indexes concurrently.** `CREATE INDEX CONCURRENTLY` outside a transaction;
   a plain `CREATE INDEX` locks writes on `tutor_projection`, which stalls the
   sync job and ages the projection out.
4. **The gate is not advisory.** `scripts/check_migration_safety.py` fails the
   deploy on `DROP TABLE`, `DROP COLUMN`, a non-concurrent index on a large
   table, or a `NOT NULL` added without a default.

## Applying

```
uv run alembic upgrade head          # normal
uv run alembic downgrade -1          # only with a human decision and a backup
```

## If a migration fails mid-deploy

The deploy stops before Terraform applies, so the old code is still running
against a partially-migrated schema. Because migrations are additive, the old
code is unaffected by a half-applied additive change. Fix forward.
