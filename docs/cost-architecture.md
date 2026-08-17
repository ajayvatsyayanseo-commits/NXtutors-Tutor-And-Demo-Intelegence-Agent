# Cost architecture

**No production usage is invented here.** This service has never run in
production, so every number below is a *formula* evaluated at three declared
traffic scenarios. Where a rate is a vendor price it is marked as of a date and
treated as approximate; where a figure is an engineering assumption it says so.

Region: `ap-south-1` (Mumbai). Currency: USD.

---

## The unit that matters

**Cost per completed match** — one conversation that reaches a delivered
shortlist. Not cost per message, because a conversation takes a variable
number of turns and per-message cost hides the difference between a service
that answers in one turn and one that takes six.

```
cost_per_match = (turns × per_turn_cost) + per_match_fixed
turns          = 1 + clarifying_questions        (assumption: mean 2.1)
```

The 2.1 figure is an **assumption**, derived from the deterministic parser's
coverage on the fixture corpus, not from live traffic. It is the single most
load-bearing input in this document. §Sensitivity shows what happens if it is
wrong.

---

## Traffic scenarios

| | LOW | EXPECTED | HIGH |
| --- | --- | --- | --- |
| Completed matches / month | 500 | 5,000 | 50,000 |
| Turns / month (× 2.1) | 1,050 | 10,500 | 105,000 |
| Peak turns / minute | 2 | 15 | 120 |
| Distinct tutors in the projection | 2,000 | 5,000 | 20,000 |
| RAG corpus (chunks) | 500 | 2,000 | 10,000 |

---

## Per-component formulas

### Lambda

Five functions. ARM64 (`arm64` is ~20% cheaper per ms and every dependency here
is pure Python).

```
cost = invocations × $0.0000002
     + Σ (GB-seconds × $0.0000133334)      # ap-south-1, arm64
```

| Function | Invocations / turn | Memory | Assumed duration |
| --- | --- | --- | --- |
| ingress | 1 | 512 MB | 60 ms |
| internal-api | 1 (handoff mode) | 1024 MB | 900 ms |
| match-worker | 1 | 1024 MB | 800 ms |
| outbound-worker | 0.5 (batched ×5) | 512 MB | 120 ms |
| scheduled | ~5,900/month (fixed) | 1024 MB | 4 s |

Durations are **assumptions from the local pipeline**, adjusted upward for
network latency to PostgreSQL. They are the second most load-bearing input, and
control 15 of the control matrix records that no load test has validated them.

| | LOW | EXPECTED | HIGH |
| --- | --- | --- | --- |
| Request-path Lambda | $0.03 | $0.28 | $2.78 |
| Scheduled Lambda | $0.32 | $0.32 | $0.32 |
| **Total** | **$0.35** | **$0.60** | **$3.10** |

### API Gateway (HTTP API)

`$1.00 per million requests` for the first 300M. Two requests per turn.

| LOW | EXPECTED | HIGH |
| --- | --- | --- |
| $0.002 | $0.02 | $0.21 |

### SQS

`$0.40 per million requests` after the free tier. FIFO is the same price.
Roughly 4 API calls per message (send, receive, delete, plus polling overhead).

| LOW | EXPECTED | HIGH |
| --- | --- | --- |
| <$0.01 | $0.02 | $0.17 |

### RDS PostgreSQL

**Shared with `demo_command_center`.** This service adds a schema, not an
instance, so its *marginal* cost is storage and IO, not the instance hour.

- Storage: ~2 GB at EXPECTED (projection + decisions + analytics) → `$0.115/GB-month` → **$0.23**
- The instance itself is **not attributed here**. If TutorMatch ever needs its own
  `db.t4g.medium` Multi-AZ, that is **~$120/month** and it would become the
  single largest line item — which is the reason the shared-schema design exists.

| LOW | EXPECTED | HIGH |
| --- | --- | --- |
| $0.06 | $0.23 | $2.30 |

### RDS Proxy

`$0.015 per vCPU-hour` of the underlying instance. For a 2-vCPU instance:
`2 × 0.015 × 730` = **$21.90/month**, flat, shared across every consumer.

This is the largest *always-on* cost in the architecture and it does not scale
to zero. It is justified by what it prevents: without it, Lambda concurrency
multiplies straight into the database connection limit and exhausts an instance
another service depends on.

| LOW | EXPECTED | HIGH |
| --- | --- | --- |
| $21.90 | $21.90 | $21.90 |

### Cache

**$0.00.** There is no ElastiCache. The shared tier is PostgreSQL
(`kv_entry`, `rate_bucket`), fronted by an in-process L1.

For contrast: the smallest sensible ElastiCache deployment
(`cache.t4g.micro`, 2 nodes) is **~$25/month, always on** — comparable to
everything else in this table combined at LOW and EXPECTED traffic, for a
workload that scales to zero between messages. It also lives in a VPC, which
with no NAT Gateway means more networking, not less.

The measurement that would change this decision: sustained `CacheHitRatio`
above ~60% **and** a p95 `StageCandidateSqlMs` that a shared cache would
materially improve. Neither is established.

### S3 + Glue

- S3 storage: ~50 MB/month sanitised exports at EXPECTED → negligible
- Glue crawler: `$0.44/DPU-hour`, minimum 10 minutes, 2 DPU, daily
  → `2 × 0.44 × (10/60) × 30` = **$4.40/month**

| LOW | EXPECTED | HIGH |
| --- | --- | --- |
| $4.40 | $4.42 | $4.60 |

Glue runs **daily, offline**. Running it per conversation would be both
pointless and the most expensive way to use it.

### CloudWatch

EMF means metrics are emitted as log lines — no `PutMetricData` call, no extra
latency, no extra IAM. Cost is ingestion plus custom metrics.

- Ingestion `$0.57/GB`; ~2 KB per turn
- Custom metrics `$0.30/metric/month`; ~60 metrics × ~3 dimension sets = **$54/month**

**This is the second-largest line item, and it is a design decision worth
revisiting.** 60 metric names is thorough; it is also $54/month at *any*
traffic level. Reducing to the ~20 that drive alarms would save ~$12/month —
recorded as an optimisation, not done, because during a first production
rollout the diagnostic value is worth more than the saving.

| LOW | EXPECTED | HIGH |
| --- | --- | --- |
| $54.00 | $54.02 | $54.20 |

### Embeddings

`text-embedding-3-small` at **$0.02 / 1M tokens** (as of early 2025).

The content-hash ledger means an unchanged corpus costs **nothing** to re-run.
Only the initial ingestion and subsequent edits are billed.

```
initial   = chunks × ~220 tokens × $0.00000002
steady    = changed_chunks_per_month × ~220 × $0.00000002
```

At EXPECTED: 2,000 chunks initial = **$0.01 once**; ~5% monthly churn =
**$0.0004/month**. Effectively free — which is the point of the ledger.

### LLM calls

The dominant *variable* cost, and the one with a hard ceiling.

Model routing (§15) means: SQL hard filter → bounded pool → **deterministic**
scoring → at most **one** bounded explanation call. There is never a call per
tutor.

Per turn, at most `TMM_LLM_MAX_CALLS_PER_TURN = 2`:

| Call | When | Model | In / out tokens |
| --- | --- | --- | --- |
| Extraction | Only when triggered (ambiguity, or no subject found) | `gpt-4o-mini` | ~900 / ~150 |
| Explanation | Once per shortlist | `gpt-4o-mini` | ~700 / ~120 |
| Escalation | ≤1 per *conversation* | `gpt-4o` | ~1,200 / ~200 |

`gpt-4o-mini`: **$0.15 / 1M input**, **$0.60 / 1M output**.
`gpt-4o`: **$2.50 / 1M input**, **$10.00 / 1M output**.

**Assumption:** deterministic parsing fully handles ~60% of turns, so
extraction fires on ~40%. Escalation fires on ~5% of conversations.

```
per_turn  = 0.40 × (900×0.15 + 150×0.60)/1e6      = $0.000090
          + 0.48 × (700×0.15 + 120×0.60)/1e6      = $0.000085   (explanation, ~1 per match)
per_conv  = 0.05 × (1200×2.50 + 200×10.00)/1e6    = $0.000250
```

Prompt caching reduces the input half where the provider supports it; the
stable prefix is ~1.2 KB and is byte-identical across calls. Tracked as
`PromptCacheHitRate`, **not** relied on for correctness or for this model.

| | LOW | EXPECTED | HIGH |
| --- | --- | --- | --- |
| Per completed match | $0.00062 | $0.00062 | $0.00062 |
| **Monthly** | **$0.31** | **$3.10** | **$31.00** |

### Geocoding

**$0.00 in the steady state.** Tutor coordinates are pre-geocoded offline and
stored on the projection. The family's location is looked up at most
`geocode_max_calls_per_turn = 1` time per turn, cached for 24 hours, and served
from the `geo_point` table thereafter.

If a paid provider is enabled (`geocoder = http`), the ceiling is:

```
max = distinct_new_localities_per_day × 30 × provider_rate
```

At EXPECTED with ~200 distinct localities and a $5/1000 rate: **~$0.03/month**
after the first month. The cap and the cache are what make this a rounding
error instead of a per-candidate bill.

---

## Totals

| Component | LOW | EXPECTED | HIGH |
| --- | --- | --- | --- |
| Lambda | $0.35 | $0.60 | $3.10 |
| API Gateway | $0.00 | $0.02 | $0.21 |
| SQS | $0.00 | $0.02 | $0.17 |
| RDS storage (marginal) | $0.06 | $0.23 | $2.30 |
| **RDS Proxy** | **$21.90** | **$21.90** | **$21.90** |
| Cache | $0.00 | $0.00 | $0.00 |
| S3 + Glue | $4.40 | $4.42 | $4.60 |
| **CloudWatch** | **$54.00** | **$54.02** | **$54.20** |
| Embeddings | $0.00 | $0.00 | $0.01 |
| **LLM** | **$0.31** | **$3.10** | **$31.00** |
| Geocoding | $0.00 | $0.03 | $0.30 |
| KMS | $1.00 | $1.00 | $1.10 |
| **Total / month** | **~$82** | **~$85** | **~$119** |
| **Cost per completed match** | **$0.164** | **$0.017** | **$0.0024** |

---

## Top cost drivers

Ranked, with what to do about each:

1. **CloudWatch custom metrics — $54/mo, flat.** Independent of traffic.
   The largest line item at LOW and EXPECTED. Reducible to ~$18 by cutting to
   the ~20 alarm-driving metrics. **Deliberately not done** for the first
   rollout: diagnostic value beats $36/month while learning how the service
   behaves. Revisit after 60 days.
2. **RDS Proxy — $21.90/mo, flat.** Also traffic-independent, also
   unavoidable: it is what stops Lambda concurrency exhausting a database
   another service shares. Already amortised across consumers.
3. **LLM — the only line that scales with traffic**, and the only one that can
   run away in a day. Four independent ceilings plus a fast alarm
   (`-llm-spend-rate`, hourly) rather than relying on AWS Budgets, which
   reports too late to matter.
4. **Glue — $4.40/mo.** Fixed by the daily schedule and the 10-minute minimum
   billing. Would only grow if someone put it on the request path, which §23
   forbids.

**The shape of this bill is unusual and worth stating plainly: at LOW and
EXPECTED traffic, ~89% of the cost is fixed overhead that would be identical if
the service handled zero messages.** The variable cost of actually matching a
family is under two cents. That is the intended consequence of a scale-to-zero
design, and it means the honest way to reduce cost is to remove always-on
components, not to optimise the request path.

---

## Sensitivity

The two assumptions that would move the answer:

| If wrong | Effect at EXPECTED |
| --- | --- |
| Turns per match is 4 rather than 2.1 (deterministic parsing weaker than modelled) | LLM $3.10 → $5.90; total $85 → $88. Absorbed. |
| Extraction fires on 100% of turns rather than 40% | LLM $3.10 → $6.20; total → $88. Absorbed. |
| Worker duration is 3s rather than 800ms | Lambda $0.60 → $2.00; total → $86. Absorbed. |
| Traffic is 10× HIGH (500k matches/month) | LLM → $310; total → ~$400. **The LLM line becomes dominant** and prompt caching stops being optional. |

The design is insensitive to the assumptions at realistic volumes, because it
is dominated by fixed costs. That is a comfortable position for a first release
and an uncomfortable one at ten times HIGH traffic, where the trade reverses.

---

## Budgets and alarms

| Control | Threshold | Where |
| --- | --- | --- |
| AWS Budgets — actual | 80% of `monthly_budget_usd` ($200 default) | `cost.tf` |
| AWS Budgets — forecast | 100% | `cost.tf` |
| LLM spend rate | `llm_monthly_budget_micros / 730 × 3` per hour | `cost.tf` |
| LLM budget exhaustion | >10 conversations per 15 min | `cost.tf` |
| DB connections | `match_worker + internal_api` reserved concurrency | `cost.tf` |

Two clocks on purpose. AWS Budgets is slow (hours) and catches a *structural*
change — a new always-on resource. The CloudWatch alarm is fast (minutes) and
catches a runaway *rate*. Model spend needs the fast one: a budget alert that
arrives the next morning is a post-mortem, not a control.

The immediate response to a spend alarm is the `LLM_PAUSED` kill switch, which
degrades to deterministic matching rather than stopping the service.
Runbook: `docs/runbooks/high-llm-spend.md`.

---

## What is not modelled

- **Data transfer** — negligible within a region; the outbound worker's calls to
  `graph.facebook.com` are single-KB payloads.
- **Support and human review time** — a real cost, out of scope for an
  infrastructure model.
- **The RDS instance hour** — attributed to `demo_command_center`, which
  provisioned it. If TutorMatch ever needs its own, add ~$120/month.
- **NAT Gateway** — not provisioned (~$32/month + per-GB, avoided by design).
- **Fargate/ECS** — not used.
