# Threat model — Demo Command Center Agent

Every threat below has an implemented control and a test that fails if the
control is removed. Where residual risk remains, it is stated plainly rather
than described as mitigated.

**Trust boundaries**, outermost first:

1. **Public internet** → Meta and Cashfree webhook Function URLs. Unauthenticated
   by design; the HMAC signature *is* the authentication.
2. **Other NXTutors agents** → the internal handoff endpoint. HMAC over method,
   path, timestamp and body hash.
3. **The customer** → message text. Data, never instruction.
4. **The model** → tool proposals. Requests, never commands.
5. **Providers** → responses. Untrusted until validated.

The single most important structural property: **the model has no authority.**
It proposes; `orchestration/authorisation.py` decides. Prompt-injection
detection is a signal for throttling and escalation, not the boundary.

---

## 1. Forged Meta webhook

| | |
|---|---|
| **Asset** | Conversation state; the ability to make the agent speak |
| **Attacker** | Anyone who finds the Function URL |
| **Entry** | `POST` to the Meta ingress |
| **Control** | `verify_meta` — HMAC-SHA256 over the **raw bytes** with the app secret, `hmac.compare_digest`. Body size checked before any hashing. |
| **Residual** | A leaked app secret defeats this. Rotation is the answer; the secret lives in Secrets Manager and only the ingress role may read it. |
| **Test** | `test_payment_path.py::test_a_forged_signature_is_rejected`, `test_resilience.py::TestSignatures::test_meta_signature_verification` |

The raw-bytes detail matters: re-serialising parsed JSON and signing *that* is
the classic way to make verification pass for a payload that is not the payload
that was signed. `_raw_body` base64-decodes API Gateway's encoding rather than
re-encoding the string form.

## 2. Replayed Meta event

| | |
|---|---|
| **Control** | Durable dedup on the Meta message id (`dcc_inbound_events` PK, and the orchestrator's idempotency claim). A replay returns the first outcome and sends nothing. |
| **Residual** | Bounded by the claim TTL. A replay after expiry is treated as new — correct, since Meta does not redeliver on that timescale. |
| **Test** | `test_lifecycle.py::test_a_redelivered_event_changes_nothing` |

## 3. Forged Cashfree webhook

| | |
|---|---|
| **Asset** | Money. A forged success activates a subscription nobody paid for. |
| **Control** | `verify_cashfree` — base64 HMAC-SHA256 over `timestamp + raw_body`, constant-time, plus a replay window. |
| **Residual** | Leaked secret key. Same rotation answer. |
| **Test** | `test_payment_path.py::test_a_forged_signature_is_rejected`, `::test_a_signature_for_different_bytes_does_not_verify` |

## 4. Replayed payment event

| | |
|---|---|
| **Control** | Two independent layers. Cheap: the signed timestamp must be inside `cashfree_webhook_tolerance_seconds`. Durable: `dcc_payment_events` has the provider event id as its **primary key**, so a replay cannot insert. |
| **Residual** | None material. The durable layer holds even if the tolerance is misconfigured. |
| **Test** | `::test_a_replayed_webhook_outside_the_window_is_rejected`, `::test_a_duplicate_provider_event_is_ignored` |

## 5. Modified payment amount

| | |
|---|---|
| **Control** | `PaymentEvent.reconcile()` compares order ref, currency and amount for **exact** equality against our stored order. An overpayment is refused too. |
| **Why exact** | "At least the expected amount" would let a ₹1 order settle a ₹4,800 plan if the order ref were ever confused. |
| **Residual** | None. The amount originates from `ApprovedOffer`, which the model cannot produce. |
| **Test** | `::test_an_amount_mismatch_is_refused`, `::test_an_overpayment_is_also_refused` |

## 6. Duplicate payment confirmation

| | |
|---|---|
| **Control** | `reconcile()` refuses an order already `PAID`; `dcc_activation_success_uidx` permits at most one successful activation per order, forever. |
| **Test** | `::test_a_successful_activation_is_never_repeated` |

## 7. Fake browser redirect "success"

| | |
|---|---|
| **Attacker** | The customer, or anyone who can craft a URL |
| **Control** | The return URL is **not an input to state**. `Trigger.PAYMENT_PAID` is authorised only for `Actor.PAYMENT_PROVIDER`, and its guard requires `signature_verified`. There is no code path from a redirect to a paid state. |
| **Test** | `test_state_machine.py::test_user_cannot_declare_payment` |

## 8. Internal handoff forgery

| | |
|---|---|
| **Control** | `verify_internal` — HMAC over `METHOD\npath\ntimestamp\nsha256(body)`. The **path is signed**, so a signature minted for `/internal/handoff` cannot be replayed against `/ops/demos`. |
| **Residual** | A shared secret is symmetric: any holder can mint. Per-purpose scopes limit what a mint achieves; there is no generic "admin" scope. |
| **Test** | `test_resilience.py::test_a_signature_minted_for_another_path_does_not_replay` |

## 9. Replayed agent envelope

| | |
|---|---|
| **Control** | Timestamp tolerance, plus `idempotency_key` on every envelope. `HttpAgentBus.dispatch` **refuses to send** an envelope without one, rather than sending something that cannot be retried safely. |
| **Test** | `test_hardening.py` (dispatch refusal), `test_tutor_integration.py` (envelope shape) |

## 10. Prompt injection

| | |
|---|---|
| **Asset** | The agent's behaviour |
| **Control (real)** | The model has no authority. Every proposal passes schema → ownership → state → authorization → policy → rate limit → idempotency → single-flight before anything executes. |
| **Control (defence in depth)** | NFKC normalisation, invisible/bidi stripping, injection-pattern flagging, untrusted-data fencing. |
| **Residual** | An injection can still make the model produce a *wrong but permitted* proposal — e.g. selecting option 2 instead of 1. Bounded by the ordinal-only schema: the worst case is the wrong tutor from the list we chose, not an arbitrary one. |
| **Test** | `test_guardrails.py::TestInputGuardrails` (7 patterns), `::test_an_injected_instruction_does_not_select_a_tutor` |

## 11. Tool-call injection

| | |
|---|---|
| **Control** | Closed registry. `FORBIDDEN_TOOL_NAMES` are **absent**, not blocked. Financial and booking tools are not model-facing at all. `additionalProperties: false` on every schema. |
| **Test** | `test_hardening.py::TestToolRegistry` (6 tests), `::TestAuthorisation::test_an_extra_argument_is_refused` |

## 12. SQL injection

| | |
|---|---|
| **Control** | Every runtime value is a named Data API parameter. The only f-string interpolation in SQL is the schema name, fixed at construction and validated against `^[a-z_][a-z0-9_]{0,62}$`. There is no `execute_raw`. |
| **Test** | `test_boundaries.py::test_every_sql_string_interpolates_only_the_validated_schema` — walks the AST, not a grep |

## 13. SSRF

| | |
|---|---|
| **Asset** | The Lambda execution role's credentials via `169.254.169.254` |
| **Control** | `security/urls.validate` before any socket: scheme allowlist, host allowlist, **literal-IP rejection**, userinfo rejection, no redirect following. |
| **Why literal-IP** | The metadata endpoint is an IP, so host-name allowlisting alone would not catch it. |
| **Test** | `test_guardrails.py::TestOutputGuardrails::test_an_unapproved_host_is_blocked` |

## 14. Malicious URL reaching a customer

| | |
|---|---|
| **Control** | The output guard checks every URL in a body, button and template variable against `customer_safe_url_policy` — Meet, Cashfree hosted pages, and the NXTutors site. Notably absent: `graph.facebook.com` (we call it; we never link to it). |
| **Test** | `::test_a_message_containing_an_unapproved_link_is_blocked` |

## 15. Malicious tutor ID

| | |
|---|---|
| **Attacker** | The customer, or an injected instruction |
| **Control** | A tutor reference is **looked up**, never accepted. `resolve()` maps an ordinal or a name onto the persisted candidate snapshot; the commands layer reads `tutor_ref` from facts only, and re-asserts `tutor_in_snapshot` before placing a hold. |
| **History** | A payload fallback existed in Phase 1 and was removed as a tampering vector (Phase 1 bug #6). |
| **Test** | `test_guardrails.py::TestTutorSelection` (12 tests), `test_state_machine.py::test_tutor_selection_must_come_from_the_stored_snapshot` |

## 16. IDOR

| | |
|---|---|
| **Control** | Every identifier is an opaque ULID or a peppered hash; none is sequential. Every read is scoped by `conversation_ref`, which the caller cannot choose — it is derived from the verified webhook. |
| **Residual** | The ops API is the one surface where an operator supplies an identifier. Region authorization (below) is the control there. |

## 17. Regional authorization bypass

| | |
|---|---|
| **Control** | `MonitoringCapability.assert_authorised` runs **server-side** on every read, against regions fetched from the gateway. `compare()` omits unauthorised regions rather than erroring, so the response does not leak which regions exist. |
| **Not a control** | Frontend filtering. The API never returns rows the operator may not see. |
| **Test** | `test_capabilities.py::TestMonitoring::test_an_unauthorised_region_is_refused_server_side` |

## 18. Sub-admin privilege escalation

| | |
|---|---|
| **Control** | No generic admin scope exists. The ops-api IAM role has no `lambda:InvokeFunction`, no `sqs:SendMessage` and no secret beyond its own. Discount approval accepts approve/decline on a **computed** percentage — an operator cannot type a different number. |
| **Test** | `test_capabilities.py::TestDiscounts::test_a_human_cannot_type_a_different_number` |

## 19. Duplicate calendar event

| | |
|---|---|
| **Control** | `dcc_demos_calendar_event_uidx`, plus a stable Google `requestId`. A reschedule **patches** the existing event (`_patch_event`) rather than creating a second one. |
| **History** | Phase 1 bug #2 — the reschedule path created a second event until the E2E test caught it. |
| **Test** | `test_lifecycle.py::test_only_one_logical_calendar_event_exists_after_a_reschedule` |

## 20. Duplicate WhatsApp message

| | |
|---|---|
| **Control** | `MessageLogRepository.claim_send` inserts the idempotency key and returns False if it existed. One winner, decided by the database. |
| **Test** | `test_failure_paths.py::test_the_same_message_is_never_sent_twice` |

## 21. Double booking

| | |
|---|---|
| **Control** | `dcc_slot_holds_active_uidx` — a **partial unique index** on `(conflict_key) WHERE status = 'active'`. The exclusion is the index, not a read-then-write. |
| **Test** | `test_failure_paths.py::test_concurrent_holds_produce_exactly_one_winner` (10-way), `test_profiles.py` (50-way) |

## 22. Race during slot hold

| | |
|---|---|
| **Control** | Optimistic locking on conversation state (`UPDATE ... WHERE version = :expected`), single-flight on exclusive tools, and re-validation against the gateway immediately before booking. |
| **Residual** | The tutor's own calendar can change between re-validation and creation. Google's conflict response is handled; the window is milliseconds. |
| **Test** | `test_failure_paths.py::test_two_concurrent_turns_do_not_both_advance_the_state` |

## 23. Secret leakage

| | |
|---|---|
| **Control** | `SecretStr` everywhere; the PII filter is attached to the log *handler*, not applied at call sites; provider error bodies are never logged; `Outcome.detail` is the exception **type name** only. |
| **Test** | `test_pii_and_secrets.py` (below), `test_hardening.py::test_the_error_detail_never_carries_a_message_body` |

## 24. PII leakage

| | |
|---|---|
| **Control** | The domain works in opaque refs; a phone number exists only inside `GatewayContactResolver` at send time. Metric labels are refused if identifying. HITL packets are redacted and excerpt-capped. LLM payloads are redacted with pincodes on. |
| **Residual** | The gateway holds the real PII, by design. Demo's tables contain none. |
| **Test** | `test_boundaries.py::test_no_module_stores_a_raw_phone_field`, `test_pii_and_secrets.py` |

## 25. Log injection

| | |
|---|---|
| **Control** | One JSON object per line via `json.dumps` — a newline in a value is escaped, not emitted. Extras are prefixed `dcc_` so they cannot collide with `LogRecord` attributes. |
| **Residual** | A log *consumer* that parses naively could still be confused. Out of scope here. |

## 26. Dependency compromise

| | |
|---|---|
| **Control** | Seven pinned runtime dependencies, all pure Python. `pip-audit --strict` in CI. `scan_prohibited.py` fails on a Redis/MySQL/driver import. Package contents are asserted by the build script. |
| **Residual** | A compromised upstream release of a pinned version. Pinning bounds the window; audit catches known advisories. |

## 27. Oversized payload

| | |
|---|---|
| **Control** | `_check_size` runs **first** on every signature path, before any hashing — a flood of 10 MB posts costs almost nothing. `max_body_bytes` is configurable. Provider responses over 2 MB are refused. |
| **Test** | `test_payment_path.py::test_an_oversized_body_is_rejected_before_any_hashing` |

## 28. Deeply nested JSON

| | |
|---|---|
| **Control** | `assert_depth_ok` — one linear pass before validation, capped at 12. A recursive validator blowing the stack is a cheap DoS otherwise. |
| **Test** | `test_guardrails.py::test_a_deeply_nested_body_is_rejected` |

## 29. Queue poisoning

| | |
|---|---|
| **Control** | Per-item `batchItemFailures`, bounded `maxReceiveCount`, a DLQ per lane. An **unparseable** record is dropped rather than re-reported — redelivering it only burns the redrive count to reach the same DLQ. A poison event opens a HITL case. |
| **Test** | `test_hardening.py::TestErrorClassification` |

## 30. Provider retry storm

| | |
|---|---|
| **Control** | Per-provider circuit breakers with bounded half-open probes; full-jitter backoff; provider `Retry-After` honoured but capped; WhatsApp sends **never** auto-retried (no API-level idempotency key). |
| **Why jitter** | Equal backoff across a fleet re-synchronises every retry into one spike against a provider that is already struggling. |
| **Test** | `test_hardening.py::TestProviderResilience`, `test_resilience.py::TestCircuitBreaker` |

## 31. LLM cost exhaustion

| | |
|---|---|
| **Control** | Four ceilings (per turn, per conversation, reasoning-tier, tokens) plus an environment-wide daily circuit. `FORBIDDEN_USES` names ten things a model must never be asked, each with the deterministic alternative. No LLM in the webhook path, and none for a duplicate event. |
| **Test** | `test_hardening.py::TestCostControl` (11 tests) |

## 32. Notification abuse

| | |
|---|---|
| **Control** | Per-demo reminder ceiling across reschedules, per-identity daily cap, quiet hours, opt-out with a deliberately tiny transactional exemption, per-recipient hourly rate limit at the outbound boundary. |
| **Test** | `test_capabilities.py::TestReminders`, `test_failure_paths.py::test_an_opted_out_recipient_...` |

## 33. Discount manipulation

| | |
|---|---|
| **Attacker** | The customer, by repetition or by injected instruction |
| **Control** | Deterministic band engine. The model never sees the policy and never proposes an amount. Bands require **all** their triggers. Price floor enforced in the engine *and* as a database CHECK. Repeat requests escalate rather than increase. |
| **History** | Phase 1 bug #7 — any-trigger matching overpaid every price-sensitive customer by ~5%. |
| **Test** | `test_capabilities.py::TestDiscounts` (13 tests) |

## 34. Onboarding replay

| | |
|---|---|
| **Control** | An idempotency claim keyed on `(order_ref, subscription_ref)`; `dcc_handoffs.idempotency_key` is UNIQUE. A retried handoff is a recorded no-op. |
| **Test** | `test_lifecycle.py::test_the_subscription_was_activated_exactly_once` |

---

## Controls that are structural rather than procedural

These are the ones worth knowing about, because they cannot be forgotten:

| Property | Enforced by |
|---|---|
| Only one module can send a WhatsApp message | AST test over every import of `WhatsAppPort` |
| No domain module can open a socket | AST test over imports in five layers |
| No SQL is built by interpolation | AST test over every f-string containing SQL |
| A payment order cannot carry an unauthorised amount | `from_offer` is the only constructor and takes `ApprovedOffer` |
| A tutor cannot be booked from an unpresented reference | facts-only read, plus `tutor_in_snapshot` re-assertion |
| A discount cannot breach the floor | engine check **and** a database CHECK constraint |
| Drift cannot auto-deploy | `AUTO_APPLY_ENABLED = False`, asserted by a test |
| No capability can write another's tables | `WRITES` map with a no-shared-owner test |

## Known residual risks

1. **Symmetric internal secret.** Any holder can mint a valid handoff. Scoped
   per purpose; asymmetric signing is a Phase 3 option.
2. **Model choosing a permitted-but-wrong action.** Bounded by ordinal-only
   schemas and the authorisation pipeline; not eliminated.
3. **Gateway compromise.** The gateway is authoritative for price, contact and
   activation. A compromised gateway can mis-price. Out of Demo's control.
4. **Function URL enumeration.** Both webhook URLs are public and
   unauthenticated by design. The signature is the control; the URLs themselves
   are not secret and are not treated as such.
5. **In-process circuit breakers.** Per container, so a cold fleet re-discovers
   a dead provider. Accepted: a shared breaker costs a round trip per call.
