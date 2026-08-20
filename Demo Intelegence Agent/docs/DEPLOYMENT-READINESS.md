# Deployment readiness

**Date:** 2026-08-18
**Verdict:** `DEPLOYABLE — GATEWAY BUILT, NEEDS DEPLOY + GOOGLE CREDENTIAL`

Both agents are production-ready and verified against live services. The
website gateway they depend on has now been built too, in the NXTutors website
repository — but it is not yet deployed, so `nxtutors.com` still answers 404.

Two things must ship before a real customer journey completes: **deploy the
website** (including the migration and the phone-hash backfill), and **supply a
Google credential** for calendar and Meet links. Everything else is done and
checked.

---

## 1. What is verified against live services

| Component | Evidence |
| --- | --- |
| **Meta WhatsApp** | Phone `+91 78360 34313`, "Nxtutors EdTech", quality **GREEN**, CLOUD_API. Token live-checked. |
| **Templates** | All 8 **APPROVED** in the WABA. Arity checked field-by-field against `GET /{waba}/message_templates`. |
| **OpenAI** | Live call through the agent's own client returned validated JSON. |
| **PostgreSQL** | `demo_agent` (37 tables) + `tutor_match` (22) in one database. Round-trip, optimistic lock and slot exclusion all verified live. |
| **Real tutors** | 1,894 in the projection, 111 cities. 7 distinct tutors across 5 different searches. |
| **Cashfree** | Production credentials copied from the website's own `.env`. |
| **Tutor feed auth** | Shared HMAC secret now present on both sides. |

## 2. The gateway — now built

The eight endpoints the agent needs did not exist. They do now, in the website
repository:

| Endpoint | Backed by |
| --- | --- |
| `POST /identity/resolve` | `register` + `demo_leads`, matched on `phone_hash` |
| `GET /tutors/{ref}/contacts` | `register` — **serves tutor refs and `ph_` parent refs alike** |
| `GET /tutors/{ref}/availability` | Returns `[]` with `source: not_recorded` — the schema holds no schedules, and inventing office hours would book a slot nobody agreed to |
| `GET /plans/quote` | `plans`, price as integer paise via `bcmul` |
| `GET /customers/discount-eligibility` | `order_managment` history |
| `POST /demos` | `demo_leads`, idempotent on `X-Idempotency-Key` |
| `POST /subscriptions/activate` | `user_subscriptions`, idempotent inside a locked transaction |
| `GET /operators/{ref}/regions` | `register` city/district/state, empty for unknown |

New on the website side:

* `app/Http/Controllers/Api/AgentGatewayController.php`
* `app/NxtAi/Support/AgentPseudonymiser.php` — the agents' phone hashing,
  reproduced and **pinned against Python-generated vectors**
* `routes/agent_gateway.php` — separate from `api.php`, which stays GET-only
  because the tutor feed's read-only guarantee is asserted by a contract test
  that reads that file
* `database/migrations/..._add_phone_hash_for_agent_gateway.php` + a backfill
  command
* `AgentGatewaySignatureTest` (35 assertions) and `AgentPseudonymiserTest`

### Two integration bugs this surfaced

**The two agents used different identity headers.** The Tutor feed client sends
`X-Nxt-Agent`; the Demo gateway client sends `X-Nxt-Source`. The middleware
checked only the first, so every gateway call would have been a `403
unknown_agent` — a correctly signed, correctly configured agent turned away,
with the log saying it was unrecognised. The middleware now accepts either.

**The gateway client signed the path without the query string.** The verifier
builds its canonical string from `getRequestUri()`, which includes the query.
So `plan_quote`, `discount_eligibility` and `tutor_availability` returned 401
while the parameterless calls succeeded — which reads as an intermittent auth
fault, not a signing bug. Fixed and pinned by
`tests/contract/test_gateway_signing.py`.

Both were found by running the real Python client against the real Laravel
routes, not by reading the code.

## 2b. What still has to happen

1. **Deploy the website.** The routes exist in the repository and are not live:
   `https://www.nxtutors.com/api/agent/v1/*` and `/internal/agent/tutors` still
   return 404 until this ships.
2. **Run the migration and the backfill.**
   ```
   php artisan migrate
   php artisan agent:backfill-phone-hashes --dry-run
   php artisan agent:backfill-phone-hashes
   ```
   Until the backfill runs, every existing person is invisible to the agents:
   a lookup miss reads as an unknown contact, which fails closed as opted-out,
   so no message reaches them and nothing is logged.
3. **Confirm `AGENT_HASH_PEPPER` survives the deploy.** It must equal
   `TMM_HASH_PEPPER` and `DCC_HASH_PEPPER`. A different value raises nothing
   and silently suppresses every message.

Not verifiable here: MySQL is not running on this machine and PHP has no
SQLite extension, so the controllers' database behaviour has unit-level
verification only. The signature layer, the routing and the hash compatibility
are all verified live.

## 3. Data reality worth knowing before launch

Measured from the source database, not assumed:

| Field | Coverage | Effect |
| --- | ---: | --- |
| Board | 1,269 / 1,894 | Matching works well |
| Class | 1,262 / 1,894 | Works, but see below |
| Mode | 1,644 / 1,894 | Works |
| **Subject** | **4** | Effectively absent |

Two consequences:

* **Subjects are not recorded.** `teacher_course_managment.cat_id` holds a
  course *segment* ("Academic (Class XI–XII)"), not a subject, and `sub_id` is
  populated for 6 rows out of 1,365. A tutor with no subject passes the filter
  rather than being excluded, so matching still works — on board, class, mode
  and city — but nothing can rank by subject until the website captures it.
* **Class is almost all "Class 11."** 1,224 tutors, against 36 for Class 12 and
  effectively none below. A Class 10 search returns almost nothing. This looks
  like an import default rather than reality, and it is worth checking before
  launch: it decides which parents the agent can serve.

## 4. Configuration status

| Setting | State |
| --- | --- |
| `DCC_META_*` | **Set** from the Lead Intake agent — one WABA across all three agents |
| `DCC_OPENAI_API_KEY` | Blank, inherits `TMM_OPENAI_API_KEY` — one key, one place |
| `DCC_POSTGRES_DSN` | Blank, inherits `TMM_POSTGRES_DSN` — one database, cannot drift |
| `DCC_HASH_PEPPER` | **Matches** Tutor's — required, or the agents name different people |
| `DCC_INTERNAL_SIGNING_SECRET` | **Matches** Lead Intake ↔ Tutor — one trust domain |
| `DCC_GATEWAY_SIGNING_SECRET` | **Matches** the website's `AGENT_FEED_SIGNING_KEY` |
| `DCC_CASHFREE_*` | **Set**, production |
| `DCC_GOOGLE_*` | Auth mode set; **credential missing** (§5) |
| Queue URLs, ARNs, KMS | Blank — Terraform supplies these at apply time |

## 5. Before you deploy

**Required for a working customer journey**

1. **Deploy the website.** The tutor feed and all eight `/api/agent/v1/*`
   endpoints now exist in the repository; none of them is live. Then run
   `php artisan migrate` and `php artisan agent:backfill-phone-hashes` — see
   §2b, because until the backfill runs every existing person is invisible to
   the agents.
2. **Set `persistence_mode`** in `<env>.tfvars`. No default, deliberately — see
   `terraform.tfvars.example`. Read the network note in `final-integration-gaps.md`
   before choosing `postgres_dsn`.
3. **Terraform apply.** Queue URLs, scheduler group/role, KMS key and the OIDC
   deploy role all come from it.

**Required for scheduling**

4. **Google credential.** Put `{client_id, client_secret, refresh_token}` in
   Secrets Manager, point `DCC_GOOGLE_CREDENTIALS_SECRET` at it, set
   `DCC_GOOGLE_ENABLED=true`. Scope: `calendar.events`. Use `oauth_refresh` —
   `service_account` needs RS256 signing and the `cryptography` package this
   Lambda deliberately does not ship.

**Worth doing first**

5. **Check the Class 11 skew** (§3). It decides who the agent can serve.
6. **Cashfree is live.** `DCC_CASHFREE_ENV=production` with real keys. A test
   run can create a real order. Point it at sandbox for staging.

## 6. Rollback

`.github/workflows/demo-command-center-rollback.yml` moves the Lambda alias to a
previous published version. It does **not** re-run migrations and cannot
destroy payment or scheduling state — automating that was prohibited and is not
automated.

Reverting the `.env` changes from this audit: `.env.backup-before-audit`,
`.env.backup-before-clean`, `.env.backup-before-meta`. The website's original
is at `.env.backup-before-agent-wiring`.

## 7. What the gates say

```
Demo suite          809 passed, 83.61% coverage
Tutor regression    772 passed, 18 skipped, 1 xfailed
ruff / mypy         clean / 137 files, no issues
bandit              High 0, Medium 0
pip-audit           no known vulnerabilities
templates           8/8 approved, bindable, guard-survivable
prohibited scan     OK
live database       WIRING OK
make demo / sync    LIFECYCLE OK / SYNC OK
doctor              0 problems, 2 gaps
terraform           fmt OK, validate Success
protected paths     199 files, 0 drift
```
