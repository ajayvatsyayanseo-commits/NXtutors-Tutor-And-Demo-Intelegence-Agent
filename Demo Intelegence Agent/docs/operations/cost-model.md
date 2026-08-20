# Cost model

Every figure here is a **parameter**, never a hardcoded price. Published cloud
and model prices change, and a rate baked into code silently makes every cost
report wrong from the day it does. Fill in the rate table for your account and
region; the structure is what this document contributes.

---

## 1. The unit that matters

**Cost per completed demo.** Not per request, not per Lambda invocation — a
demo that reaches `CONVERTED` is the thing the business gets paid for, and the
denominator has to match.

The current measured per-demo work, from `tests/load/test_profiles.py`:

```
per completed demo: llm=2  tutor_match=1  calendar_events=1  messages=7
```

Those four numbers are asserted as **ceilings** in the load suite. Crossing one
means a loop or a duplicate call, and it shows up in CI before it shows up on a
bill.

---

## 2. Rate parameters

Fill these from the AWS and OpenAI pricing pages for your account and region.
They are inputs to the arithmetic below, not claims about current prices.

| Parameter | Symbol | Unit |
|---|---|---|
| Lambda request | `P_req` | per 1M requests |
| Lambda duration (arm64) | `P_gbs` | per GB-second |
| SQS request | `P_sqs` | per 1M requests |
| CloudWatch Logs ingestion | `P_logs` | per GB |
| CloudWatch custom metric | `P_metric` | per metric per month |
| X-Ray trace recorded | `P_trace` | per 1M traces |
| Aurora Data API request | `P_dataapi` | per 1M requests |
| Secrets Manager secret | `P_secret` | per secret per month |
| KMS request | `P_kms` | per 10k requests |
| Model input tokens | `P_in` | per 1M input tokens |
| Model output tokens | `P_out` | per 1M output tokens |

`cost_control.budget.estimate_cost_micros` takes `input_per_million` and
`output_per_million` directly, in micro-units, and does **integer** arithmetic —
a float cost summed across a month drifts.

---

## 3. Work per completed demo

Measured from the lifecycle harness. Multiply by the rates above.

| Resource | Quantity | Driver |
|---|---|---|
| Lambda invocations | ~25 | ingress ×2, orchestrator ×14, capability workers ×6, outbound ×7 (batched) |
| Lambda GB-seconds | ~8 | mostly the 1024 MB orchestrator and objection worker |
| SQS requests | ~60 | send + receive + delete across five lanes |
| Data API requests | ~90 | state reads/writes, transitions, demo rows, payment rows |
| Log ingestion | ~40 KB | structured JSON, one line per meaningful event |
| KMS requests | ~30 | queue payload encryption |
| Model calls | 2 | objection extraction ×1, intent classification ×1 |
| Model tokens | ~2,500 in / ~600 out | bounded by `context_token_budget` and `llm_max_output_tokens` |

**Estimated cost per demo** =
`25·P_req/1M + 8·P_gbs + 60·P_sqs/1M + 90·P_dataapi/1M + 0.00004·P_logs + 30·P_kms/10k + (2500·P_in + 600·P_out)/1M`

Fixed monthly costs are independent of volume: `P_secret` × 1 secret, plus
CloudWatch alarms and custom metrics.

---

## 4. Implemented cost controls

Each is a real mechanism with a test, not an intention.

### No model call where a deterministic answer exists

`cost_control.budget.FORBIDDEN_USES` names ten of them, each with the
alternative:

| Never asked | Done instead by |
|---|---|
| arithmetic | `Decimal` in `domain/pricing.py` |
| state transition | the transition table |
| availability | the website gateway |
| payment validation | signature + exact amount reconciliation |
| regional aggregation | SQL rollups |
| discount amount | the deterministic band engine |
| authorization | actor sets on transitions |
| tutor eligibility | Tutor Intelligence hard filters |
| no-show determination | calendar/conference evidence |
| duplicate event | dropped before any model is reached |

### No LLM in the ingress path

The webhook verifies, dedupes, enqueues, returns. A model call there would put
token cost on every redelivery and every probe of a public URL.

### No LLM on a duplicate event

The idempotency claim runs **first** in `orchestrator.handle`, before facts are
assembled or any capability runs. A redelivery storm costs one database read
each, not one model call each.

### Four independent ceilings

Tokens alone do not bound cost — the expensive failure is a loop making
thousands of cheap calls. So: calls per turn, calls per conversation,
reasoning-tier calls per conversation, tokens per conversation. Plus a daily
environment-wide circuit that degrades every capability to its deterministic
path rather than stopping the service.

### Model routing by purpose

Only objection extraction uses the expensive tier — it is the one task where a
shallow answer produces a wrong business decision. Everything else uses the
cheap tier. Asserted by
`test_hardening.py::test_only_objection_extraction_uses_the_expensive_tier`.

### Bounded context

`ContextBuilder` fills a token budget in priority order and stops, and
stage-scopes facts first so a tight budget spends its tokens on what matters
here. There is no code path that loads an unbounded transcript — asserted by
`test_profiles.py::test_no_unbounded_scan_on_the_conversation_path`.

### Small package, warm clients

Seven pure-Python runtime dependencies; `boto3` excluded because the runtime
already has it. Provider clients and policy documents are built once per
container. arm64 for a lower per-millisecond rate.

### Batching where it is safe

Outbound sends batch at 5 with a 2-second window; reminders at 10 with 5
seconds; analytics at 10 with 10 seconds. The decision lanes deliberately do
**not** batch: `batch_size = 1` keeps unrelated conversations from serialising
behind each other, and the latency that would cost is worth more than the SQS
request it would save.

### Finite retention and sampled tracing

`log_retention_days` defaults to 30 and is validated to be finite.
`tracing_mode` defaults to `PassThrough` — `Active` on every function traces
everything and costs accordingly.

### Reminder ceilings

Per demo across reschedules, and per identity per day. Without them a parent
who reschedules four times receives four full ladders.

### Provider retry caps

Bounded attempts with full jitter, plus per-provider circuit breakers. A dead
provider costs one probe per reset window, not one call per request.

### Capability kill switches

`flag_scheduling_enabled`, `flag_reminders_enabled`, `flag_payments_enabled`,
`flag_discounts_enabled`, plus `CircuitRegistry.disable()` per provider. Each
disables a capability's *side effects* without taking the conversation down.

---

## 5. Cost alarms

| Alarm | Fires on | Action |
|---|---|---|
| `llm-cost` | hourly spend above `hourly_llm_cost_alarm_micros` | notify |
| `llm-budget-exhausted` | >20 conversations hitting a ceiling in 15 min | investigate a loop |
| `circuit-opened` | any provider circuit opening | check the provider |
| `*-throttles` | reserved concurrency exhausted | raise it or shed load |

Cost alarms **notify**; they never page. A cost spike at 3am is not worth waking
someone for, and treating it as if it were is how genuine pages get muted.

Drift adds a `COST_PER_DEMO` finding when observed spend exceeds
`cost_multiplier` × baseline over a window with a real sample size.

---

## 6. What drives cost up, in order

1. **A redelivery loop.** Caught by `llm-budget-exhausted` and the DLQ alarms.
2. **Reasoning-tier calls escaping their ceiling.** Caught by the per-conversation
   reasoning budget.
3. **Context growth.** Caught by the bounded builder and the load assertion.
4. **Tracing left on `Active`.** A deploy-time choice; the default is sampled.
5. **Log verbosity at DEBUG in production.** `log_level` is a setting; the
   alarm is the ingestion bill.

---

## 7. Deliberately not done

* **No provisioned concurrency.** It is a fixed hourly cost for latency this
  workload does not need — a parent waiting two seconds for a WhatsApp reply
  notices nothing.
* **No NAT Gateway.** A fixed hourly cost, avoided entirely by keeping every
  function outside the VPC and reaching Postgres over the Data API.
* **No always-on worker.** Everything is invoked by a queue or a schedule.
* **No S3.** No artifact bucket, no state bucket, no analytics bucket. Rollups
  live in `dcc` tables that already exist.
