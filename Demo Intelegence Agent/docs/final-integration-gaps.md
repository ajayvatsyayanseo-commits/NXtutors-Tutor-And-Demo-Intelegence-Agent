# Final integration gaps

**Date:** 2026-08-17

Every external contract that could not be verified, and every decision an
operator must make before this system touches a real customer. Nothing here is
softened; a gap that is only implied is a gap that ships.

Classification:

| Status | Meaning |
| --- | --- |
| `IMPLEMENTED — LIVE CREDENTIAL REQUIRED` | Code complete and unit-tested; the external call has never run |
| `EXTERNAL CONTRACT NOT AVAILABLE` | We do not have the other side's specification |
| `BLOCKED BY BUSINESS CONFIGURATION` | A human must supply a value |
| `OPERATOR DECISION REQUIRED` | Two valid designs; picking one is not ours to do silently |
| `KNOWN RISK` | Accepted, with the reason |

---

## 1. Database role separation — `OPERATOR DECISION REQUIRED`

**The gap.** Both halves connect with the same credential, because both inherit
`TMM_POSTGRES_DSN`. Nothing in Demo's code names a `tutor_match` table, and
`verify_live_wiring.py` asserts Tutor's table count is unchanged after Demo
writes — but the *grant* is wide. A bug or an injection in Demo could reach the
Tutor schema, and only the absence of such SQL is stopping it.

**Why it was left this way.** The instruction was explicit that both agents
share one `.env` and one connection string. Creating a second role and a second
DSN would satisfy least privilege and violate the stated requirement. That
trade-off is the operator's to make, not something to decide unilaterally.

**The durable fix**, when the operator wants it:

```sql
CREATE ROLE demo_agent_app LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA demo_agent TO demo_agent_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA demo_agent TO demo_agent_app;
REVOKE ALL ON SCHEMA tutor_match FROM demo_agent_app;
```

Then set `DCC_POSTGRES_DSN` to that role's DSN. The inheritance in
`Settings._share_the_tutor_database` only fires when `DCC_POSTGRES_DSN` is
blank, so filling it in is the entire change — no code edit.

**Until then:** the schema separation is enforced by code and by `search_path`,
not by the database's permission system. That is a real difference.

---

## 2. `postgres_dsn` from a Lambda outside a VPC — `OPERATOR DECISION REQUIRED`

**The gap.** The Demo functions have no `vpc_config`, which is what removes the
need for a NAT Gateway. In `data_api` mode that is entirely consistent: the Data
API is a public AWS endpoint reached with IAM.

In `postgres_dsn` mode the function opens a direct connection to the database
instead. A Lambda outside a VPC has **no stable source address**, so there is
nothing narrow for the database's security group to admit.

**The three resolutions, and what each costs:**

| Option | Cost | Consequence |
| --- | --- | --- |
| Enable the Aurora Data API and use `data_api` mode | Requires an Aurora Serverless cluster; the current database is a **plain RDS instance with no Data API** | Cleanest. No VPC, no NAT, no security group, IAM-authenticated |
| Put the functions in the VPC with a NAT Gateway | **A NAT Gateway is prohibited for new Demo resources** | Would need that prohibition lifted |
| Put the functions in the VPC with VPC endpoints and no NAT | VPC endpoints for SQS, Secrets Manager, EventBridge, KMS, plus internet egress for Meta/Google/Cashfree/OpenAI — which still needs NAT | Only works if outbound provider calls move behind something else |

**What must not be done:** widening the database security group to `0.0.0.0/0`
to make option zero work. That is stated in `variables.tf` and in `lambdas.tf`
as an instruction, not left to judgement.

**Current state.** `persistence_mode` is now a Terraform variable **with no
default**, so a deploy cannot pick one silently. `terraform.tfvars.example`
documents both. Locally, `postgres_dsn` works and is verified — the gap is
specific to Lambda.

---

## 3. Meta WhatsApp — `IMPLEMENTED — LIVE CREDENTIAL REQUIRED`

| | |
| --- | --- |
| Never executed | Send, template send, delivery status, webhook verification against real Meta |
| Verified | Signature verification over raw bytes, template variable ordering, session-window logic, opt-out, idempotency — all against fixtures |
| Needs | `DCC_META_ACCESS_TOKEN`, `DCC_META_PHONE_NUMBER_ID`, `DCC_META_APP_SECRET`, `DCC_META_VERIFY_TOKEN` |

**Sub-gap — one template name is unknown. `BLOCKED BY BUSINESS CONFIGURATION`.**

`__unconfirmed_tutor_confirmation__` is a deliberate placeholder. The approved
template name was truncated in the supplied screenshot and **has not been
guessed**. A wrong template name is a rejected send at the provider, in
production, on the first real booking — the worst possible place to discover a
typo.

`make doctor` lists it as unusable until `DCC_TEMPLATE_TUTOR_CONFIRMATION` is
set to the real approved name.

**Not done, by instruction:** no template was created or renamed through
application code.

---

## 4. Google Calendar — `IMPLEMENTED — LIVE CREDENTIAL REQUIRED`

| | |
| --- | --- |
| Never executed | Event creation, Meet link generation, patch-on-reschedule, RSVP read, conference participation read |
| Verified | The request shapes, the patch-not-create logic, compensation on failure |
| Needs | `DCC_GOOGLE_*` credentials and a calendar the service account may write |

**Watch on first live run:** Google creates the conference **asynchronously**.
The Meet link may not be present in the create response. The scheduling worker
has a 90-second timeout for this reason, but the real settling behaviour has
never been observed. If the link is absent, the system must not invent one — the
guard already prevents that, so the failure mode is a missing link, not a wrong
one.

**Attendance is never inferred.** It comes from RSVP or conference participation
only. A demo with 2+ conference participants counts both as attended.

---

## 5. Cashfree — `IMPLEMENTED — LIVE CREDENTIAL REQUIRED`

| | |
| --- | --- |
| Never executed | Order creation, payment link, webhook receipt, refund |
| Verified | HMAC over raw bytes, exact amount reconciliation, replay rejection, idempotency, saga compensation |
| Needs | `DCC_CASHFREE_APP_ID`, `DCC_CASHFREE_SECRET_KEY`, a reachable webhook URL |

**The highest-consequence unverified path in the system.** Money moves here.
Verify in a sandbox account before production, specifically:

1. The exact webhook signature algorithm and which bytes it covers.
2. That the amount in the webhook is in the units we reconcile against (paise).
3. That a duplicate webhook delivery is absorbed by the idempotency key.
4. That a browser redirect claiming success **without** a webhook does not
   activate a subscription. The code refuses this; confirm it against the real
   redirect.

---

## 6. NXTutors gateway (Laravel) — `EXTERNAL CONTRACT NOT AVAILABLE`

| | |
| --- | --- |
| Never executed | Identity resolution, tutor contacts, quotes, activation |
| Verified | Request signing, retry and timeout behaviour, the fake |
| Needs | `DCC_GATEWAY_BASE_URL` and a signing secret |

**This is a contract gap, not only a credential gap.** We do not have the
gateway's response schema. The Demo client codes against the shape it was told
about, and every field crossing the boundary is re-validated into Demo's own
Pydantic contracts — so a mismatch fails loudly at the boundary rather than
propagating a wrong value into a message.

**Not done, by instruction:** no direct Laravel or MySQL access. Everything goes
through the gateway, and no backend Python or credential was placed in Laravel's
`public/` directory.

---

## 7. OpenAI — `IMPLEMENTED — LIVE CREDENTIAL REQUIRED`

| | |
| --- | --- |
| Never executed | Any real model call. Spend to date: **zero** |
| Verified | The offline stub path, budget ceilings, `FORBIDDEN_USES`, timeout and retry |
| Needs | `DCC_OPENAI_API_KEY` |

Currently the stub is in use and objection extraction is **heuristic**.
`make doctor` reports that. It is honest degradation, not a silent fallback that
resembles a working model.

The model has no authority in either mode, so this gap affects extraction
quality — not correctness, cost or safety.

---

## 8. EventBridge Scheduler — `IMPLEMENTED — LIVE CREDENTIAL REQUIRED`

| | |
| --- | --- |
| Never executed | Schedule creation, firing, cancellation on reschedule |
| Verified | The port, the payload, cancellation logic |
| Needs | `DCC_SCHEDULER_GROUP_NAME`, `DCC_SCHEDULER_ROLE_ARN` |

**Without this, reminders do not fire on time.** Capability 026 is inert. There
is deliberately no polling fallback, because a poller is an always-running
worker and those are prohibited.

---

## 9. Aurora Data API — `EXTERNAL CONTRACT NOT AVAILABLE`

The `data_api` persistence backend is implemented and unit-tested but has
**never run against a real cluster**, because the shared database is a plain RDS
instance with no Data API.

This is what forced the `postgres_dsn` backend into existence — and that turned
out well: it closed a larger gap in which 7 of 10 aggregates had only in-memory
implementations. All 10 now have real PostgreSQL repositories, verified live.

The Data API path remains the better Lambda design (see §2) and remains
unverified. Both are true.

---

## 10. Tutor integration tests — `NOT EXECUTED`

18 tests in `tests/integration/test_postgres_stores.py` skip because
`TMM_INTEGRATION_DSN` is unset. They need a **disposable** PostgreSQL — they are
not safe against the shared database.

Recorded as `NOT EXECUTED`, never as passing. The test module's own skip message
says the same thing.

---

## 11. Load testing against deployed infrastructure — `NOT EXECUTED`

The four in-process load profiles pass and measure orchestrator work per
conversation. They **cannot** measure cold starts, SQS behaviour or real
provider latency. Four further profiles (`peak`, `burst`, `stress`, `soak`) are
defined and have never been run.

No throughput or user-count claim is made anywhere in this work.

---

## 12. Terraform state backend — `OPERATOR DECISION REQUIRED`

There is no `backend` block. State would be local, which is wrong for a team.

The reason is a genuine conflict: the standard backend is an S3 bucket plus a
DynamoDB lock table, and **new S3 buckets are prohibited for Demo**. The
resolutions are (a) reuse an existing state bucket the operator already owns,
(b) use Terraform Cloud, or (c) lift the prohibition for this one bucket.

Documented in `docs/operations/terraform-state.md`. `terraform init` will not
produce a shared-state setup until one is chosen.

---

## 13. `terraform plan` / `apply` — `NOT EXECUTED`

`terraform fmt -check -recursive` and `terraform validate` both pass. Neither
`plan` nor `apply` has run: there are no AWS credentials here, and a plan
against a real account is an operator action.

`validate` proves the configuration is internally consistent. It does **not**
prove the referenced ARNs exist or that the account permits the resources.

---

## 14. Regional authorization is application-level — `KNOWN RISK`

Capability 129's region scoping is enforced in the application, not by IAM.
`iam.tf` states this in a comment rather than implying the boundary is stronger
than it is. A compromised monitoring worker could read outside its region.

Making it an IAM boundary needs per-region roles or database row-level security.
Accepted, not solved.

---

## 15. Upstream: Lead Intake is missing a setting — `EXTERNAL CONTRACT NOT AVAILABLE`

`tests/contract/test_agent_harmony_contracts.py` has a standing `xfail`:
`TUTOR_MATCHING_AGENT_INTERNAL_SECRET` is absent from Lead Intake's
`app/core/config.py`, though the value is already in their `.env`.

Pre-existing, outside this repository, and not introduced by this work. It is an
`xfail` rather than a skip precisely so it turns into a failure the day someone
fixes it upstream and forgets to tell us.

---

## Summary

| # | Gap | Status | Blocks production? |
| ---: | --- | --- | --- |
| 1 | Shared database role | `OPERATOR DECISION REQUIRED` | No — mitigated in code |
| 2 | `postgres_dsn` from a non-VPC Lambda | `OPERATOR DECISION REQUIRED` | **Yes**, if that mode is chosen |
| 3 | Meta WhatsApp | `LIVE CREDENTIAL REQUIRED` | **Yes** |
| 3b | Truncated template name | `BLOCKED BY BUSINESS CONFIGURATION` | **Yes** |
| 4 | Google Calendar | `LIVE CREDENTIAL REQUIRED` | **Yes** |
| 5 | Cashfree | `LIVE CREDENTIAL REQUIRED` | **Yes** |
| 6 | NXTutors gateway | `EXTERNAL CONTRACT NOT AVAILABLE` | **Yes** |
| 7 | OpenAI | `LIVE CREDENTIAL REQUIRED` | No — degrades honestly |
| 8 | EventBridge Scheduler | `LIVE CREDENTIAL REQUIRED` | **Yes** — reminders are inert |
| 9 | Aurora Data API | `EXTERNAL CONTRACT NOT AVAILABLE` | No — `postgres_dsn` works |
| 10 | Tutor integration tests | `NOT EXECUTED` | No |
| 11 | Deployed load testing | `NOT EXECUTED` | No — but no claims are made |
| 12 | Terraform state backend | `OPERATOR DECISION REQUIRED` | **Yes**, for team use |
| 13 | `terraform plan`/`apply` | `NOT EXECUTED` | **Yes** |
| 14 | Regional authorization | `KNOWN RISK` | No — accepted |
| 15 | Lead Intake setting | `EXTERNAL CONTRACT NOT AVAILABLE` | No — upstream |

**Eight gaps block production. Every one is an external credential, an external
contract or an operator decision. None is unfinished code.**
