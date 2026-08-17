# Fallback matrix

What happens when each dependency is unavailable.

Two rules run through every row, and they are the reason this table is short
rather than clever:

1. **Degrade the answer, never fail the turn.** A dependency being down is
   recorded in `degraded_sources` on the decision and the parent still gets an
   honest reply.
2. **Never fabricate to fill a gap.** Where a fallback cannot establish a fact,
   the fact is omitted. A shorter, true message beats a fuller, invented one.

The kill switches in `config/kill_switches.py` declare their safe behaviour as
**data**, with a rationale and a `lossless` flag, because the wrong safe
behaviour is worse than no switch: pausing the LLM should degrade quietly, but
pausing outbound must *hold* messages, and those two are one careless line
apart.

---

## Summary

| Dependency | Behaviour | Lossless? | Parent sees | Switch |
| --- | --- | --- | --- | --- |
| OpenAI | Deterministic parsing and scoring | Yes | A normal shortlist, or one clarifying question | `LLM_PAUSED` |
| RAG / pgvector | Structured matching only | Yes | A normal shortlist | `RAG_PAUSED` |
| Chitragupta | Session-bounded state; deeds spool | Yes | A normal shortlist | `MEMORY_WRITES_PAUSED` |
| Cache | Read the canonical store | Yes | A normal shortlist, slower | — |
| Website write-back | Queue or hand to a human | **No** | "A coordinator will confirm" | `WEBSITE_WRITEBACK_PAUSED` |
| PostgreSQL | Refuse to decide; retry | **No** | Nothing, then a retry | `MATCHING_PAUSED` |
| Geocoding | Stored coordinates or locality name | Yes | A shortlist without distance claims | — |
| Meta outbound | Hold in the outbox | **No** | Delayed reply | `OUTBOUND_PAUSED` |
| No exact tutor | Controlled relaxation, with consent | Yes | A question, then alternatives | — |

---

## OpenAI unavailable

**Detection.** `LLMTimeout`, `LLMRateLimited`, `LLMCircuitOpen`, or an
operator setting `LLM_PAUSED`. The circuit breaker opens after 5 failures and
half-opens after 30s with a single probe, so an outage costs microseconds per
turn rather than a 12-second timeout each — without that, the queue backs up.

**Behaviour.** Deterministic extraction handles the well-formed majority: a
message like *"class 10 cbse maths sector 57 after 6:30 home tuition around
900"* is fully parsed with **zero** model calls. The eight evaluators and the
ranking policy never used a model at all. The explanation layer falls back to
templates over guard-approved evidence.

**Where a model genuinely helped**, an ambiguous message gets one deterministic
clarifying question instead of a guess. That is a slightly worse experience,
not a failure.

**Never.** No fabricated extraction, no assumed subject, no assumed board.
`_should_escalate` requires an explicit trigger; "more intelligence sounds
better" is not one.

**Recorded.** `degraded_sources = ("llm",)`; `LlmPaused` or `CircuitOpen`.

**Test.** `tests/e2e/test_resilience.py::TestDependencyFailure` — four error
classes parametrised, each asserting the turn still matches.
`::test_llm_paused_degrades_to_deterministic_matching` injects a provider that
raises `AssertionError` if called at all.

---

## RAG unavailable

**Detection.** pgvector missing, retrieval raising, or `RAG_PAUSED`.

**Behaviour.** Retrieval is supplementary context only. The lexical BM25 tier
is the floor, not a fallback: it needs no embedding provider, no pgvector and
no network, so retrieval degrades to "slightly worse supporting text" rather
than "no results". If even that is unavailable, `knowledge` is empty and the
shortlist is unchanged.

**Critically:** exact tutor filtering is **not** affected. Availability, fees,
ratings and account status are structured facts read from the projection —
never from a vector index. Using retrieval for "is this tutor free on Tuesday"
is how a matcher starts inventing, and it is not how this one is built.

**Recorded.** `degraded_sources = ("rag",)`.

---

## Chitragupta unavailable

**Detection.** Timeout or non-2xx; `MemoryPacket.available = False`.

**Behaviour.** The turn proceeds on **bounded current-session state**: whatever
this conversation has established, plus what is already persisted in
`match_requirement`. Recall returning nothing is treated as "nothing known",
never as "no preference".

Deeds spool to a per-container WAL at `/tmp` and the relay job drains it. `/tmp`
is the only writable path in Lambda and is per-container, so the WAL is
best-effort by construction — which is correct, because memory is audit and
personalisation, never a matching input.

**Never.** No manufactured past preference. Recalled facts are already filtered
to `confidence >= 0.6 and not denied`, and memory ranks **below** anything this
conversation established — a remembered "Class 9" from last year must not
overwrite a parent saying "Class 10" today.

**Recorded.** `degraded_sources = ("chitragupta",)`; `ChitraguptaFailures`.

**Test.** `::test_a_memory_outage_is_recorded_not_raised`. The default local
stack uses `NullMemory`, which is *always* unavailable — so every e2e test is
already proof the match path does not depend on it.

---

## Cache unavailable

**Detection.** `PostgresKeyValueStore.failures`; `CachedTutorRepository.errors`.

**Behaviour.** A miss. The KV store fails **open** on read and swallows write
errors; the pool cache catches every exception and falls through to the
database. Every read path is written to work with the cache empty, and
`NullCache` in the test suite proves it rather than assuming it.

**The one exception:** the rate limiter fails **closed**. If the store is
unreachable we cannot prove a caller is under their limit, and a limiter that
fails open under database stress is precisely a limiter that does not work when
it is needed.

**Database protection.** Reserved concurrency bounds the number of workers,
so a cold cache means more queries but a bounded number of them.

**Recorded.** `CacheErrors`, `CacheHitRatio`.

**Test.** `tests/security/test_cache_hygiene.py::TestEveryPathWorksCold` — a
full turn against `NullCache`, and a pool cache whose store raises on every call.

---

## Website integration unavailable

**Detection.** Laravel API 5xx or timeout; `MysqlFailures`.

**Behaviour, read side.** The matching path does **not** call the website
synchronously. It reads the PostgreSQL projection, refreshed every 15 minutes.
A website outage is invisible to a parent until the projection ages past
`projection_aging_hours` (24h), at which point rows stop entering the pool and
the freshness alarm has already fired.

**Behaviour, write side.** A demo request is a real commitment to a family.
It is held durably in the outbox and replayed on recovery — never dropped, and
never claimed as done.

**Never.** No claim of a live fact that cannot be established. If the projection
is stale the decision records `oldest_source_data_at` and the evidence guard
refuses to quote a dimension whose data is stale.

**Recorded.** `WebsiteFailures`, `ProjectionStalenessHours`.

**Runbook.** `docs/runbooks/website-sync-stale.md`.

---

## PostgreSQL unavailable

**This is the one dependency with no graceful degradation, and that is deliberate.**

**Behaviour.** The turn fails and SQS redelivers. We do **not** produce a
recommendation whose decision cannot be recorded: an unauditable shortlist is
worse than a delayed one, because a family acts on it and nobody can later
explain it.

**Sequence.** `decisions.save` raises → the idempotency claim is released → the
exception propagates → the record is reported as a batch-item failure → SQS
redelivers → after `maxReceiveCount = 3`, the DLQ and its alarm.

**Why releasing the claim matters:** without it, the retry would be swallowed
as a duplicate and the parent would never be answered.

**Recorded.** DLQ depth; `-match-dlq-not-empty` (threshold 0).

**Test.** `::test_a_failed_turn_releases_its_claim_for_a_genuine_retry`.

**Runbook.** `docs/runbooks/db-outage.md`.

---

## Geocoding unavailable

**Behaviour, in order:**

1. Stored coordinates on the tutor projection (pre-geocoded offline) — this is
   the steady state, so a geocoder outage usually changes nothing;
2. The family's stored locality centroid from `geo_point`;
3. Locality-name equality — coarse, and **labelled** as coarse;
4. Nothing.

**For online tuition, distance is ignored entirely** — the proximity evaluator
returns a neutral score, because distance is not a real constraint.

**For home tuition**, if feasibility cannot be established, the parent is asked
naturally: *"which area are you in, so I can find someone who travels there?"*
Not an error, not a guess.

**Never.** No guessed city-centre coordinate. `resolve()` returns `None` for an
unknown input, because a guessed distance would be presented as a real one.

**Test.** `::test_an_unknown_location_returns_none_not_a_guess`.

---

## No exact tutor

The most product-sensitive fallback, and the one where "helpful" most easily
becomes "dishonest".

**Never:** return random tutors, or quietly drop a constraint the family stated.

**Relaxation ladder** — each rung asks or notifies before violating an explicit
requirement:

| Rung | Relaxation | Consent |
| --- | --- | --- |
| 0 | Exact match | — |
| 1 | Nearby time slot (±1h) | Notify: *"…though they'd suit 6pm rather than 7"* |
| 2 | Nearby locality (adjacent sector, within travel radius) | Notify with the locality named |
| 3 | Online instead of home | **Ask** — a different mode of learning |
| 4 | Different board, same subject and class | **Ask** — often a real mismatch |
| 5 | No relaxation possible | Honest no-match, naming the single blocking constraint |

`_diagnose()` identifies the *dominant* rejection reason, so the parent is told
which one constraint to relax rather than being asked to start over.

**Status.** Rung 5 (honest no-match with a named blocking rule) is implemented
and tested. **Rungs 1–4 are not implemented** as an automatic ladder — the
service currently goes straight from "exact" to "no match with a reason". The
`constraint_relaxed` analytics event and the `relaxation` dimension exist in
anticipation. Named as an open item in
`docs/production-readiness-final.md` §14.

---

## Kill switch reference

| Switch | Behaviour | Lossless | Rationale (from the code) |
| --- | --- | --- | --- |
| `MATCHING_PAUSED` | `DECLINE_TO_CALLER` | Yes | Lead Intake owns the conversation and can still answer. Declining is invisible to the parent; erroring is not. |
| `LLM_PAUSED` | `DEGRADE_SILENTLY` | Yes | Deterministic extraction and scoring cover the well-formed majority. |
| `RAG_PAUSED` | `DEGRADE_SILENTLY` | Yes | Supplementary context only; exact filtering is unaffected. |
| `WEBSITE_WRITEBACK_PAUSED` | `HOLD_IN_OUTBOX` | **No** | A demo request is a real commitment. Hold and replay, do not drop. |
| `OUTBOUND_PAUSED` | `HOLD_IN_OUTBOX` | **No** | The decision is already persisted; regenerating later could produce a *different* shortlist. |
| `MEMORY_WRITES_PAUSED` | `DEGRADE_SILENTLY` | Yes | Memory is audit and personalisation, never a matching input. |
| `AUTO_DEMO_PAUSED` | `ESCALATE_TO_HUMAN` | **No** | Booking a demo unsupervised is the highest-commitment action the agent takes. |

`any_lossy_pause()` lists the switches currently *holding work* — the ones an
operator must remember to unpause. A lossless degradation can sit paused for a
week; a held outbox cannot.

**The ordering property that makes `MATCHING_PAUSED` genuinely lossless:** the
switch is read **before** the idempotency claim. Claiming first and then
declining would burn the dedup key, so the caller's redelivery after unpause
would be swallowed as a duplicate and the parent would never be answered.
Asserted by `::test_matching_paused_declines_without_consuming_the_dedup_key`.
