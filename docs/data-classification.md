# Data classification and handling

Five classes. Every field this service touches is assigned to exactly one, and
the assignment determines where it may go — not a regex run over its contents.

That distinction is the point of this document. A regex scrub is a last line of
defence for free text; it is the wrong primary control for a field whose type
already tells you what it is. `tutor.phone` is PII because it is
`tutor.phone`, not because it happens to match `\d{10}`.

---

## The classes

| Class | Definition | Default handling |
| --- | --- | --- |
| **PUBLIC** | Already published on nxtutors.com. | No restriction. |
| **INTERNAL** | Operational data with no personal content. | Logs, metrics, analytics. Not parent-facing. |
| **CONFIDENTIAL** | Commercially sensitive, or internal reasoning a parent must never see. | Never parent-facing, never in analytics. Logs only as an aggregate. |
| **PII** | Identifies a person, directly or in combination. | Pseudonymised or redacted before leaving the process. Never in a metric label, an analytics export, or a model payload. |
| **AUTH** | Credentials and one-time codes. | Never logged, never cached, never embedded, never in a model payload, `SecretStr` in memory. |

**Combination rule.** Pincode alone is not identifying. Pincode + class +
locality + gender preference, in a small locality, is. So the classification
of `location.pincode` is PII-BY-COMBINATION and it is redacted from model
payloads even though it is load-bearing for matching internally.

---

## Field register

### Parent and student

| Field | Class | At rest | In logs | In metrics | To a model | In analytics |
| --- | --- | --- | --- | --- | --- | --- |
| `wa_phone` (E.164) | **PII** | Never stored raw¹ | Never | Never | Never | Never |
| `phone_hash` | INTERNAL | Yes | As `ph_…` | Never (high cardinality) | Never | As a pseudonym |
| `conversation_id` | **PII** (contains the phone: `wa:+91…`) | Yes, as the partition key | **Never** — `cv_…` only | Never | Never | Never |
| `conversation_ref` (`cv_…`) | INTERNAL | Yes | Yes | Never (cardinality) | Never | Yes |
| Parent name | **PII** | Not collected | — | — | — | — |
| WhatsApp message body | **PII** | `match_requirement.payload` only as parsed fields | **Never** | Never | Redacted + sanitised, wrapped as untrusted data | **Never** |
| Student's class | INTERNAL | Yes | Yes | Yes | Yes | Banded (`class_band`) |
| Student's weak topics | **PII** (a minor's academic difficulty) | Yes | Never | Never | Yes, redacted | Never |
| Student name / age / DOB | **PII** | Not collected | — | — | — | — |
| `location.city` | INTERNAL | Yes | Yes | Yes | Yes | Yes |
| `location.locality` | **PII-BY-COMBINATION** | Yes | Never | Never | Redacted, truncated to 120 chars | **Never** |
| `location.pincode` | **PII-BY-COMBINATION** | Yes | Never | Never | **Never** (`redact_pincode=True`) | Never |
| Exact home address | **PII** | **Never collected** — assumption A5 | — | — | — | — |
| Budget amount | CONFIDENTIAL | Yes | Never | Never | As a boolean only² | Never |
| `lead_id` | INTERNAL | Yes | Yes | Never (cardinality) | Never | Never |

¹ One exception, deliberate and bounded: `outbox_event.payload.recipient`
holds a raw E.164 number, only under `outbound_ownership=tutor_match_sends`,
only while a message is undelivered. The Meta Cloud API addresses by phone;
there is no way to deliver without it. It is never logged, and the retention
job purges delivered rows. Under the deployed default (`caller_sends`) the
field is `None` and this service never holds a phone number at all.

² `RequirementView.has_budget` is a boolean. Sending the amount would invite
the model to negotiate against it, which is exactly the willingness-to-pay
inference §38 forbids.

### Tutor

| Field | Class | At rest | In logs | To a model | Parent-facing |
| --- | --- | --- | --- | --- | --- |
| `tutor_id` | INTERNAL | Yes | Yes | **Never** | **Never** — refused by `output_guard` |
| `public_ref` | PUBLIC | Yes | Yes | **Never**³ | Yes |
| Name | PUBLIC | Yes | Yes | **Never**³ | Yes |
| Profile URL | PUBLIC | Derived | Yes | Never | Yes, canonical only |
| Subjects / boards / classes | PUBLIC | Yes | Yes | Yes | Yes |
| Experience years | PUBLIC | Yes | Yes | Banded | Yes |
| Rating and review count | PUBLIC | Yes | Yes | Summarised | Yes, if quotable |
| Published fee band | PUBLIC | Yes | Yes | `fee_fit` label only | Yes |
| Locality label | PUBLIC | Yes | Yes | Yes | Yes |
| **Coordinates** | **PII** | Yes | **Never** | **Never** | **Never** — distance band only |
| Phone / email | **PII** | **Not projected** | Never | Never | **Never** |
| Document numbers | **AUTH** | **Not projected** | Never | Never | Never |
| `travel_radius_km` | INTERNAL | Yes | Yes | Never | Never |
| `active_students` | CONFIDENTIAL | Yes | Never | Never | Never |
| Replacement-risk score | **CONFIDENTIAL** | In the score vector | Aggregate only | Never | **Never** — refused by `output_guard` |
| `source_checksum` | INTERNAL | Yes | Never | Never | Never |

³ A tutor's name and public ref are public *on the website*, but they are
withheld from model payloads anyway: including them would re-identify the
`cand_1` pseudonym and defeat the point of having one. The name is attached
back to the shortlist entry after generation, by code, from the projection.

### Credentials

| Field | Class | Handling |
| --- | --- | --- |
| `openai_api_key`, `whatsapp_access_token`, `chitragupta_api_key`, `website_api_signing_key`, `ingress_signing_key`, `internal_secret`, `continuation_signing_key`, `hash_pepper`, `mysql_dsn` | **AUTH** | `SecretStr`, so an accidental `repr` prints `**********`. Sourced from Secrets Manager. Terraform references the secret **by name**, so no value enters state. Never cached (`NEVER_CACHE`), never embedded (`rag/embeddings.py::refuse_reason`), never in a model payload. |
| OTPs, passwords | **AUTH** | Never handled by this service. Refused by the embedding pipeline in case they arrive in an imported corpus. |

---

## Where PII must never appear, and what enforces it

| Destination | Enforcement | Test |
| --- | --- | --- |
| CloudWatch metric dimensions | `assert_label_safe` raises at runtime | `test_invariants.py` |
| Exception traces | `RequestContext` has no field for a phone or a body; message text is never passed to a logger | `test_analytics_privacy.py` |
| Analytics exports | `ALLOWED_DIMENSIONS` — an unlisted key raises `UnsafeDimension` at construction | `test_analytics_privacy.py` (16) |
| Prompt / model payloads | `ModelContext` is a positive projection; `assert_no_forbidden_fields` runs on every serialisation | `test_model_payload.py` (15) |
| DLQ dashboards | The DLQ carries the envelope; operators read `trace_id` and `conversation_ref`, never the body | `docs/runbooks/dlq-replay.md` |
| Model-usage logs | `UsageLedger` holds provider, model, tokens, latency, cost — no prompt, no completion, no conversation id | `test_cost_controls.py::test_the_ledger_holds_no_prompt_or_conversation_id` |
| Cache keys | Keys are hashes and canonical labels | `test_cache_hygiene.py` |
| Vector index | `refuse_reason` blocks credentials, unredacted PII and conversation turns | `test_cost_controls.py::TestEmbeddingCostControl` |

---

## Privileged audit access

Some support work genuinely needs the raw value — confirming a parent's phone
number when they call in, for instance.

**Current position: not implemented.** There is no privileged read path, no
break-glass role, and no audited unmasking. `mask_phone` and `mask_email`
exist (`•••••3210`) for a support console that does not exist yet.

This is deliberate for the first release: the safe version of this feature is
more work than the release needs, and a half-built one is worse than none. If
support requires it, the design is:

1. A separate IAM role, assumed with MFA, never held by the service.
2. Every unmask writes an `approval_audit` row: who, what, why, trace id.
3. The masked form is the default in every view; unmasking is an explicit action.
4. An alarm on unmask rate, because the failure mode is habitual use.

Recorded as an open item in `docs/production-readiness-final.md` §14.

---

## Retention

| Data | Window | Job | Reason |
| --- | --- | --- | --- |
| `outbox_event` (delivered) | 7 days | `retention_cleanup` | Holds the one raw phone number in the system. Purged aggressively. |
| `outbox_event` (dead) | 30 days | manual | A human has to see it first. |
| `idempotency_record` | 7 days | `retention_cleanup` | The redelivery window is far shorter. |
| `match_requirement` | 180 days | `retention_cleanup` | Long enough to resume a paused conversation and to investigate a dispute. |
| `match_decision` | 400 days | `retention_cleanup` | The audit record. Outlives a full academic year. |
| `llm_usage` | 90 days | `retention_cleanup` | Cost analysis. No PII. |
| `kv_entry`, `rate_bucket` | TTL (≤1h) | `retention_cleanup` | Ephemeral by construction. |
| `analytics_event` | 400 days in PostgreSQL, then S3 | Glue | Sanitised at write time. |
| S3 `exports/` | 400 days | lifecycle | |
| S3 `curated/` | 730 days, IA at 90 | lifecycle | Drift comparison across versions needs history. |
| CloudWatch Logs | 30 days | retention policy | |

---

## Applicable obligations

India's **DPDP Act 2023** is the governing regime. The relevant duties, and
how the design meets them:

| Duty | How |
| --- | --- |
| Purpose limitation | Chitragupta recall is purpose-scoped (`purpose="tutor_matching"`); analytics dimensions are allowlisted. |
| Data minimisation | `ModelContext` is a positive projection; exact addresses are never collected; the budget is a boolean to the model. |
| Storage limitation | The retention table above, enforced by a scheduled job. |
| Children's data | A student's class and weak topics are classified **PII** precisely because they describe a minor, and are excluded from analytics entirely. |
| Erasure | **Not implemented.** There is no erasure endpoint. Named as an open item. |
| Breach notification | `docs/runbooks/privacy-incident.md` includes the notification decision, but there is no legal sign-off on the timeline. Named as an open item. |
