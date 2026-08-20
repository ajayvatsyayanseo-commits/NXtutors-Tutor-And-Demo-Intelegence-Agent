# Final protected-path verification

**Date:** 2026-08-17
**Verdict:** `NO DRIFT` — 199 protected files, 0 modified, 0 deleted, 0 renamed.

The Tutor Intelligence Agent was declared protected and read-only for the whole
of this work. This file is the evidence, not the assertion.

---

## 1. What was declared protected

| Path | Tracked files | Working-tree changes | Diff vs HEAD |
| --- | ---: | ---: | ---: |
| `src/tutor_match_meta/` | 131 | 0 | 0 lines |
| `config/policies/` | 8 | 0 | 0 lines |
| `migrations/` | 7 | 0 | 0 lines |
| `infra/terraform/` | 10 | 0 | 0 lines |
| `tests/` | 40 | 0 | 0 lines |
| `.github/workflows/ci.yml` | 1 | 0 | 0 lines |
| `.github/workflows/deploy.yml` | 1 | 0 | 0 lines |
| `.github/workflows/rollback.yml` | 1 | 0 | 0 lines |
| **Total** | **199** | **0** | **0** |

Note that `migrations/`, `infra/terraform/` and `tests/` above are the **root**
(Tutor-owned) directories. The Demo agent has its own directories with the same
names under `Demo Intelegence Agent/`, and those are Demo-owned and were written
freely. They are different paths and are not covered by this table.

## 2. How it was checked

```bash
git status --porcelain -- src/tutor_match_meta config/policies migrations \
    infra/terraform tests .github/workflows/ci.yml \
    .github/workflows/deploy.yml .github/workflows/rollback.yml
# → no output

git diff HEAD --stat -- <the same paths>
# → no output
```

Empty output from both is the proof. `--porcelain` reports modifications,
deletions, renames and untracked additions inside those paths; `diff HEAD`
reports staged changes that `--porcelain` alone would show as clean.

## 3. Commit identity

| Fact | Value |
| --- | --- |
| `HEAD` at the start of the work | `594c7585fdb7ab7b448ed3ea30beec2e18160b9f` |
| `HEAD` at this verification | `594c7585fdb7ab7b448ed3ea30beec2e18160b9f` |
| Tree hash of `src/tutor_match_meta` | `9637b9514ca3533b28168601c2c54335de444980` |

`HEAD` is unchanged because **no commit was made**, as instructed. The tree hash
is recorded so a future verification can compare one value instead of walking
131 files:

```bash
git rev-parse HEAD:src/tutor_match_meta
# must still be 9637b9514ca3533b28168601c2c54335de444980
```

## 4. What was changed outside the protected set

Everything the Demo work touched is either inside `Demo Intelegence Agent/`, or
one of these three root-level files:

| Path | Change | Why it is not protected |
| --- | --- | --- |
| `.env` | 109 `DCC_*` keys appended | The shared configuration file both agents read. The 91 pre-existing `TMM_*` keys were not modified — see §5. |
| `README.md` | Rewritten as the combined README | Documentation. Never appeared on the protected list. |
| `.github/workflows/demo-command-center-*.yml` | 3 new files | New files with new names. The three protected workflows (`ci`, `deploy`, `rollback`) are untouched; these are path-filtered to `Demo Intelegence Agent/**`. |

## 5. The `.env` merge, specifically

`.env` is shared, so "protected" could not mean "unwritable" — Demo has to get
its configuration from somewhere, and the instruction was explicitly that both
agents read one file. What was protected instead was every **existing** key:

| Fact | Value |
| --- | --- |
| `TMM_*` keys before the merge | 91 |
| `TMM_*` keys after the merge | 91 |
| `TMM_*` keys whose value changed | 0 |
| `DCC_*` keys added | 109 |
| Backup taken before writing | `.env.backup-before-dcc-merge` |

Demo does not read `TMM_*` keys as configuration. It reads exactly one, and only
as a fallback: when `DCC_POSTGRES_DSN` is blank, `Settings` inherits
`TMM_POSTGRES_DSN` so the connection string exists in one place and cannot
drift. That is a read, never a write.

## 6. The Tutor test suite still passes

Protection means the code is unchanged; this means it still *works* unchanged,
including with Demo present on the same path and reading the same `.env`.

```
772 passed, 18 skipped, 1 xfailed in 12.39s
```

The 18 skips are `tests/integration/test_postgres_stores.py`, which needs
`TMM_INTEGRATION_DSN` pointing at a disposable PostgreSQL. That variable is not
set here. **Skipped is not passed** and is recorded as `NOT EXECUTED` in
`final-release-gate-report.md`.

The 1 `xfail` is `test_the_internal_secret_is_still_missing_upstream` — an
expected failure that documents a gap in the *Lead Intake* agent
(`TUTOR_MATCHING_AGENT_INTERNAL_SECRET` missing from its `app/core/config.py`).
It is pre-existing, unrelated to this work, and outside this repository.

## 7. What could still break protection later

Recorded so the next person does not have to rediscover it:

- **`pythonpath = ["src", "../src"]`** in Demo's `pyproject.toml` puts the Tutor
  package on `sys.path` during Demo's tests. That is a read-only import for the
  envelope contract test. If anyone adds a Demo test that *monkeypatches* a
  Tutor module, protection is broken at runtime even though git stays clean.
- **The shared database.** Demo holds a DSN with whatever grants that role has.
  Nothing in the code targets `tutor_match`, and `verify_live_wiring.py` asserts
  the Tutor table count is identical before and after it runs, but the grant
  itself is an operator concern. The durable fix is a database role for Demo
  with no privileges on `tutor_match`; that is listed in
  `final-integration-gaps.md`.
