# Final cost report

**Date:** 2026-08-17

**No cloud price appears in this document as a fact.** Prices change by region
and by account, and a number written here would be stale and quietly wrong.
`docs/operations/cost-model.md` holds the model as *parameters* an operator
fills in. What this report gives instead is the thing that actually determines
the bill: **what the system consumes per demo**, measured.

---

## 1. The measured unit cost

From `tests/load/test_profiles.py::test_per_demo_provider_and_llm_counters`:

```
per completed demo: llm=2 tutor_match=1 calendar_events=1 messages=7
```

| Resource consumed per completed demo | Measured | Enforced ceiling |
| --- | ---: | ---: |
| OpenAI calls | **2** | 4 |
| Tutor match invocations | **1** | 3 |
| Google Calendar events | **1** | 1 |
| WhatsApp messages | **7** | 10 |

These four numbers, multiplied by the operator's per-unit prices and the demo
volume, are the variable cost. Everything else is rounding.

A regression in any of them shows up in CI **before** it shows up on a bill.
That is the entire purpose of asserting them.

---

## 2. Structural cost decisions

Costs avoided by architecture rather than by tuning. These are the ones that do
not drift, because nothing has to be remembered:

| Avoided | How | Why it matters |
| --- | --- | --- |
| **NAT Gateway** | No `vpc_config` on any Demo function; every dependency is a public endpoint | A NAT Gateway bills hourly whether or not a single message is sent, plus per-GB. It is the classic fixed cost in a serverless design that should have none |
| **Always-running worker** | EventBridge Scheduler fires reminders; nothing polls | A polling worker bills 24/7 to do nothing most of the time |
| **Redis / ElastiCache** | `cache/layers.py` is an in-process L1 with TTL ceilings | A cache cluster is a fixed monthly cost for a workload with no shared-cache requirement |
| **New Aurora cluster** | The existing one, one schema inside it | A second cluster is the largest avoidable line item in this system |
| **S3 artifact bucket** | `filename` + `source_code_hash`, direct upload | Small saving, but it also removes a bucket to secure and a lifecycle policy to get wrong |
| **Idle compute** | Everything is Lambda | Zero demos in an hour costs zero compute |

Fixed monthly cost attributable to new Demo resources: **CloudWatch logs and
alarms only.** There is no other resource that bills while the system is idle.

---

## 3. LLM cost controls

The LLM is the only unbounded cost in the system, so it has four ceilings and a
circuit, all from settings — none is a literal in business code.

| Ceiling | Default | Stops |
| --- | ---: | --- |
| `calls_per_turn` | 2 | A retry loop inside one turn |
| `calls_per_conversation` | 30 | A long conversation becoming expensive |
| `reasoning_calls_per_conversation` | 3 | Expensive reasoning being used routinely |
| `tokens_per_conversation` | 60,000 | A growing context re-sent on every turn |
| `max_input_tokens` | 4,000 | One oversized prompt |
| `max_output_tokens` | 900 | One runaway generation |
| `timeout_seconds` | 20.0 | Paying for a call nobody is waiting for |
| `max_retries` | 2 | A retry storm against a failing provider |

`BudgetExceeded` makes the caller **degrade deterministically. It never waits.**
A conversation that hits a ceiling continues with the heuristic path; it does
not stall and it does not silently drop.

`DailyCostCircuit` opens on the daily ceiling and stops LLM calls entirely
rather than continuing to spend into an incident.

### `FORBIDDEN_USES` — the cheapest call is the one that never happens

More important than the ceilings. Every entry is a place where a deterministic
answer already exists, so paying a model for it is both slower and worse:

| Forbidden use | What does it instead |
| --- | --- |
| `arithmetic` | `Decimal` arithmetic in `domain/pricing.py` |
| `state_transition` | `state/transitions.py` — a table, not a judgement |
| …and the rest of the map | Each names its deterministic replacement |

Enforced by `assert_not_forbidden` and asserted by a security test, so the list
cannot rot into a comment.

### Current LLM spend in this environment

**Zero.** `DCC_LLM_PROVIDER` is unset, so the offline stub is in use and
objection extraction is heuristic. `make doctor` reports that as a gap. No
OpenAI call has been made by this system.

---

## 4. Where the cost model's parameters live

`docs/operations/cost-model.md` is parameterised. Nothing in Terraform hardcodes
a price, and `variables.tf` says so explicitly. To produce a real estimate an
operator supplies:

| Parameter | Source |
| --- | --- |
| Demos per month | The business |
| WhatsApp conversation pricing | Meta, by category and country |
| OpenAI per-token price | The chosen model |
| Lambda GB-second and request price | The region |
| SQS request price | The region |
| Aurora / RDS incremental cost | Marginal — the cluster already exists |
| CloudWatch logs ingestion + retention | `log_retention_days`, default 30 |

Multiply the §1 counters by those and the answer is the variable cost. There is
no hidden consumer: 13 functions, 5 queue lanes, 1 scheduler group, no idle
compute.

---

## 5. Knobs that change cost, and their defaults

| Knob | Default | Effect |
| --- | --- | --- |
| `tracing_mode` | `PassThrough` | `Active` traces every invocation and costs accordingly. Sampled by default, deliberately |
| `log_retention_days` | 30 | Finite by requirement. Indefinite retention is both a cost and a liability |
| `log_level` | `INFO` | `DEBUG` in production multiplies ingestion cost |
| Reserved concurrency per lane | 5–50 | Caps spend during a burst as well as capping load |
| `analytics_reserved_concurrency` | 30 | Largest pool, lowest priority — safe to starve, which is also the cheap direction |
| `payment_reserved_concurrency` | 5 | Smallest pool. Chosen for correctness, not cost; cheapness is incidental |
| `architectures` | `arm64` | Cheaper per millisecond; no build cost since every dependency is pure Python |

---

## 6. Cost as a security property

An unbounded LLM loop is a denial-of-wallet attack, which is why threat 31
(`LLM cost exhaustion`) and threat 32 (`notification abuse`) are in the threat
model and not only here.

| Attack | Control |
| --- | --- |
| Adversarial input driving repeated extraction | `calls_per_turn`, `calls_per_conversation` |
| A long conversation inflating context | `tokens_per_conversation`, bounded 50-row history read |
| Retry storm against a failing provider | Circuit breakers + `max_retries=2` + full-jitter backoff |
| Notification flooding | Capability 026 throttle; ≤10 messages per demo, 7 measured |
| Queue poisoning re-driving a batch | `batchItemFailures` — one bad message does not re-run its nine neighbours |

The last one is a real cost control as well as a correctness control: without
partial batch failure, one poisoned message causes its entire batch to be
reprocessed on every retry.

---

## 7. What is not costed

| Item | Why |
| --- | --- |
| Real per-demo cloud spend | Nothing is deployed. Any figure would be invented |
| Cold-start cost at real volume | Requires a deployed stack — `final-performance-report.md` §8 |
| Provider pricing tiers | Account-specific; the operator holds these |
| Cost at high volume | **No throughput claim is made anywhere in this work.** Extrapolating §1 to a user count would be exactly the unsupported claim that was prohibited |

The §1 counters are per-demo and were measured. Multiplying them by a volume the
system has never handled would produce a number with the appearance of evidence
and none of the substance.
