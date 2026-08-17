# Integration Inventory — tutor-match-meta

Every external boundary this service touches, the adapter that owns it, the
verification status, and what is needed to move it to VERIFIED.

Status vocabulary:

- **VERIFIED** — exercised against the real dependency in this environment.
- **LOCAL_VERIFIED** — exercised against a faithful local double; wire contract
  asserted by contract tests.
- **UNVERIFIED_EXTERNAL** — code complete behind an adapter, but the live
  dependency was unavailable here (no credential / no network / owned by another
  team). Never reported as working.

---

## Inbound

### 1. Lead Intake Agent → ingress (`lead.captured` / `lead.updated`)

| | |
| --- | --- |
| Adapter | `handlers/ingress.py`, `contracts/inbound.py::LeadEventV1` |
| Transport | HTTPS POST, HMAC-SHA256 signed (`X-Nxt-Signature`, `X-Nxt-Timestamp`) |
| Contract source | `nxtutors-lead-intake-agent/app/services/integrations/events.py` |
| Status | **LOCAL_VERIFIED** (contract test pins the payload shape) |
| To verify | Lead intake must enable its webhook client; it currently returns `webhook_mode_not_enabled_for_external_calls`. |

### 2. WhatsApp inbound (via internal handoff, not direct Meta webhook)

| | |
| --- | --- |
| Adapter | `handlers/ingress.py`, `contracts/inbound.py::WhatsAppHandoffV1` |
| Transport | HTTPS POST from the WhatsApp-owning agent, HMAC signed |
| Dedup | `provider_message_id` → idempotency table + SQS FIFO `MessageDeduplicationId` |
| Status | **LOCAL_VERIFIED** |
| To verify | Requires the onboarding/lead-intake WhatsApp router to point at this endpoint. |

This service deliberately does **not** own the Meta Cloud API webhook. The
lead-intake agent already owns signature verification for Meta
(`app/integrations/whatsapp/signature.py`); duplicating it would create two
sources of truth for the same webhook.

---

## Outbound

### 3. WhatsApp outbound (Meta Cloud API)

| | |
| --- | --- |
| Adapter | `integrations/whatsapp/outbound.py::WhatsAppSender` (Protocol) |
| Implementations | `MetaCloudSender` (real), `LoggingSender` (local), `RecordingSender` (tests) |
| Isolation | Separate outbound SQS queue + worker; delivery failure never re-runs matching |
| Secrets | `WHATSAPP_ACCESS_TOKEN` via Secrets Manager |
| Status | **UNVERIFIED_EXTERNAL** — no Meta token available in this environment |
| To verify | Provide `WHATSAPP_PHONE_NUMBER_ID` + token; run `scripts/smoke_outbound.py` |

### 4. Website — read path (tutor candidates)

Two interchangeable adapters behind `repositories/website_tutor.py::WebsiteTutorRepository`:

| Adapter | Transport | Status |
| --- | --- | --- |
| `LaravelIntegrationApiRepository` (**preferred**) | signed HTTPS to an internal Laravel endpoint | **UNVERIFIED_EXTERNAL** — endpoint not yet built by the Laravel team; request/response contract is specified in `contracts/website_api.py` and pinned by contract tests |
| `ReadOnlyMySQLRepository` (compatibility) | `aiomysql`, read-only DB user | **UNVERIFIED_EXTERNAL** — no MySQL host reachable here; SQL is validated against the real DDL extracted from `127_0_0_1.sql` and exercised in `tests/integration/test_mysql_adapter.py` (skipped without `TMM_MYSQL_DSN`) |
| `ProjectionTutorRepository` (**runtime default**) | PostgreSQL search projection | **LOCAL_VERIFIED** |

The runtime match path reads the **PostgreSQL projection**, never MySQL directly.
MySQL/Laravel is used only by the sync job.

Required MySQL grants (nothing wider):

```sql
GRANT SELECT ON nxtutors.register              TO 'tutormatch_ro'@'%';
GRANT SELECT ON nxtutors.teacher_courses       TO 'tutormatch_ro'@'%';
GRANT SELECT ON nxtutors.teacher_course_managment TO 'tutormatch_ro'@'%';
GRANT SELECT ON nxtutors.teacher_review        TO 'tutormatch_ro'@'%';
GRANT SELECT ON nxtutors.category              TO 'tutormatch_ro'@'%';
GRANT SELECT ON nxtutors.city_managment        TO 'tutormatch_ro'@'%';
GRANT SELECT ON nxtutors.city_area_list_managment TO 'tutormatch_ro'@'%';
```

Column-level restriction is applied in SQL (explicit SELECT lists) **and** at the
grant level where the MySQL version supports it. `register.password`,
`otp`, `document_number`, `frount_image`, `back_image`, `email`, `phone`, `dob`
are never selected.

### 5. Website — write path (write-back)

| | |
| --- | --- |
| Adapter | `integrations/website/commands.py` + `WebsiteCommandGateway` |
| Preferred transport | signed internal Laravel API (`POST /internal/agent/commands`) |
| Fallback | direct MySQL writer, **feature-flagged off by default** (`WEBSITE_DIRECT_WRITE_ENABLED=false`), restricted to `student_enquiry_managment` and `demo_leads` |
| Commands | `CreateTutorMatchCommand`, `CreateDemoRequestCommand`, `PublishTutorLeadCommand`, `RecordParentSelectionCommand` |
| Reliability | transactional outbox in PostgreSQL → relay worker; idempotency key per command |
| Status | **UNVERIFIED_EXTERNAL** |
| To verify | Laravel team implements `/internal/agent/commands`; then run `scripts/smoke_writeback.py` |

### 6. Chitragupta Memory Gateway

| | |
| --- | --- |
| Adapter | `integrations/chitragupta/client.py` (vendored, wire-compatible with the official SDK) |
| Reads | `POST /v1/memory/query` — purpose-scoped, field-scoped |
| Writes | `POST /v1/memory/events` — deed events with WAL spool fallback |
| Deed types emitted | `MATCH_REQUIREMENT_CAPTURED`, `MATCH_SHORTLIST_GENERATED`, `MATCH_CANDIDATE_SELECTED`, `DEMO_REQUESTED`, `MATCH_FAILED_NO_CANDIDATES`, `HUMAN_HANDOFF_REQUESTED` |
| Degradation | circuit breaker + local WAL; memory unavailability never blocks a match |
| Status | **LOCAL_VERIFIED** against `FakeChitraguptaGateway`; contract test asserts event validity using the real SDK's rules |
| To verify | `CHITRAGUPTA_BASE_URL` + `CHITRAGUPTA_API_KEY`; run `scripts/smoke_chitragupta.py` |

### 7. OpenAI

| | |
| --- | --- |
| Adapter | `integrations/openai/provider.py::LLMProvider` Protocol |
| Implementations | `OpenAIProvider`, `DeterministicStubProvider` (local/tests) |
| Controls | strict JSON schema output, per-tier model config, timeout, bounded retries, token budget per conversation, circuit breaker, usage + cost telemetry |
| PII | pseudonymous IDs only; message text is redacted through `security/pii.py` before it leaves the process |
| Status | **UNVERIFIED_EXTERNAL** — no API key in this environment |
| To verify | `OPENAI_API_KEY`; run `scripts/smoke_llm.py` |

### 8. Geocoding

| | |
| --- | --- |
| Adapter | `integrations/geo/provider.py::GeocodingProvider` |
| Implementations | `PincodeTableGeocoder` (offline, from `city_area_list_managment` pincodes), `HttpGeocoder` (optional, allowlisted host) |
| Privacy | only `pincode` + `locality` + `city` are ever sent; the raw `register.address` never leaves the process |
| Status | **LOCAL_VERIFIED** (offline geocoder); HTTP geocoder **UNVERIFIED_EXTERNAL** |

### 9. AWS

| Service | Use | Status |
| --- | --- | --- |
| Lambda ×4 | ingress, match worker, outbound worker, scheduled jobs | **UNVERIFIED_EXTERNAL** (Terraform written, not applied) |
| SQS FIFO ×2 + DLQ ×2 | match queue (`MessageGroupId = conversation_id`), outbound queue | **UNVERIFIED_EXTERNAL** |
| RDS PostgreSQL + RDS Proxy | durable state | **LOCAL_VERIFIED** against local Postgres; Proxy **UNVERIFIED_EXTERNAL** |
| Secrets Manager | all secrets | **UNVERIFIED_EXTERNAL** |
| EventBridge ×5 rules | projection sync, reconciliation, embedding refresh, stale check, retention | **UNVERIFIED_EXTERNAL** |
| S3 | sanitized analytics exports, document ingestion | **UNVERIFIED_EXTERNAL** |
| Glue | offline catalog/ETL only — never on the WhatsApp path | **UNVERIFIED_EXTERNAL** |
| CloudWatch | logs, metrics (EMF), alarms | **UNVERIFIED_EXTERNAL** |
| ElastiCache Redis | optional L2 cache | **UNVERIFIED_EXTERNAL**; `InMemoryCache` is **LOCAL_VERIFIED** |

No NAT Gateway and no Fargate are provisioned, per the explicit requirement.
Lambdas reach AWS services through **VPC interface/gateway endpoints**; the
optional outbound HTTP egress (OpenAI, Meta, geocoder) is documented in
`infra/terraform/README.md` as requiring either a customer-managed egress path
or the functions being placed outside the VPC — the trade-off is stated there
rather than silently adding a NAT Gateway.
