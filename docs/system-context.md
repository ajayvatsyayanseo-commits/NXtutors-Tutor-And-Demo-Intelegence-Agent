# System Context

Where TutorMatch sits, verified by reading the checkouts in
`Ready In Production Agents/` rather than assumed.

```
                          ┌──────────────┐
                          │  Parent on   │
                          │   WhatsApp   │
                          └──────┬───────┘
                                 │ Meta Cloud API
                                 ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  LEAD INTAKE AGENT                                          │
   │  POST /webhook/whatsapp  (verify token + app-secret sig)    │
   │  app/services/whatsapp_client.py   <- the ONLY sender       │
   │  app/lead_manager_v2/intent_router.py  <- routes            │
   └───┬──────────────────────┬──────────────────────┬───────────┘
       │ signed internal      │ signed internal      │
       │ handoff              │ handoff              │
       ▼                      ▼                      ▼
 ┌───────────┐        ┌───────────────┐      ┌──────────────┐
 │ONBOARDING │        │  TUTORMATCH   │      │  DEMO CC     │
 │  signup   │        │  (this repo)  │      │ demo cycle   │
 └─────┬─────┘        └───┬───────┬───┘      └──────┬───────┘
       │                  │       │                 │
       └──────────┬───────┘       │                 │
                  ▼               ▼                 ▼
        ┌──────────────────┐  ┌─────────────────────────────┐
        │  CHITRAGUPTA     │  │  LARAVEL / MySQL WEBSITE    │
        │  shared memory   │  │  register, teacher_*,       │
        │  provenance,RBAC │  │  student_enquiry, demo_leads│
        └──────────────────┘  └─────────────────────────────┘

 TutorMatch-owned: PostgreSQL (state, decisions, projection, outbox, RAG)
```

## Verified facts

| Fact | Source |
| --- | --- |
| Lead Intake owns the public webhook | `app/api/whatsapp.py` — `POST /webhook/whatsapp`, verify token, HMAC |
| Lead Intake owns outbound | `app/services/whatsapp_client.py` |
| The house handoff pattern | `app/services/onboarding_router.py` — POST + `X-NXTUTORS-INTERNAL-SECRET`, response `{status, reply_text}`, 2s timeout |
| `tutor_matching_agent` is already a known target | `app/services/integrations/router.py` |
| `TUTOR_MATCHING_AGENT_WEBHOOK_URL` already exists | `app/core/config.py:84` |
| Live WhatsApp number | `+91 78360 34313` (`WHATSAPP_DISPLAY_PHONE_NUMBER`) |
| Shared RDS PostgreSQL | `database-1.…ap-south-1.rds.amazonaws.com` |
| Shared region | `ap-south-1` |
| Tutor profile route | `/tutor/{city}/{base64url(user_id + '-nxt')}/{name}` |

## Data domains

| Domain | Store | Owner | TutorMatch access |
| --- | --- | --- | --- |
| Website accounts/profiles | MySQL | Website | read-only projection, typed write commands |
| Cross-agent memory | Chitragupta | Chitragupta | purpose-scoped read, deed write |
| Lead/conversation intake | Lead Intake Postgres | Lead Intake | none (receives handoffs) |
| Match state and decisions | TutorMatch Postgres (`tutormatch` db) | TutorMatch | full |

Same RDS instance as Lead Intake, **separate database**, so schemas cannot
collide while networking and cost stay simple. Requires one
`CREATE DATABASE tutormatch;` before first deploy.

## Request path (live mode)

1. Parent messages `+91 78360 34313`.
2. Meta → Lead Intake webhook; signature verified, message deduped.
3. Lead Intake classifies intent.
4. Matching intent → `POST TUTOR_MATCHING_AGENT_WEBHOOK_URL` with the shared secret.
5. TutorMatch: flags → idempotency → routing → match → `{status, reply_text}`.
6. Lead Intake sends `reply_text` on the existing number.

One webhook, one sender, one decision owner.
