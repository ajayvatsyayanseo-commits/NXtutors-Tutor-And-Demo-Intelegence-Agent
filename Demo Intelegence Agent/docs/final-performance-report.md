# Final performance report

**Date:** 2026-08-17

**Read this first.** Every number in this document was measured **in process,
against simulated providers, on one developer machine**. Nothing has been
deployed. These are not production latencies and they are not an SLO. Where a
number could be mistaken for a production figure, it is labelled.

There are no throughput claims about user counts anywhere in this document.

---

## 1. What was measured, and what could not be

| Measurable here | Not measurable here |
| --- | --- |
| Work the orchestrator does per conversation | Lambda cold start |
| Provider calls per completed demo | SQS delivery and visibility behaviour |
| Slot contention under concurrency | Real provider latency (Meta, Google, Cashfree) |
| Whether per-turn cost grows with history | Aurora / RDS latency under real load |
| Whether the lifecycle degrades or collapses | Concurrency limits and throttling in a real account |

The second column requires a deployed stack. It is `NOT EXECUTED` and is listed
as such in `final-e2e-report.md` §7 and in the release gate.

---

## 2. Load profile results

`pytest tests -m load -q -s` → **4 passed**

```
baseline: 10/10 completed, p50=7.93ms p95=9.72ms p99=9.72ms errors=0  wall=0.17s
per completed demo: llm=2 tutor_match=1 calendar_events=1 messages=7
50-way slot contention resolved in 1.2ms
turn latency after 60 turns: 0.0ms
```

### The `baseline` profile

10 full lifecycles at concurrency 2. Every one completed, zero errors, in 0.17s
wall clock.

| Metric | Value |
| --- | ---: |
| Completed | 10 / 10 |
| Errors | 0 |
| p50 | 7.93 ms |
| p95 | 9.72 ms |
| p99 | 9.72 ms |
| Wall clock | 0.17 s |
| Budget | 30 s |

**7.93ms is not a production p50.** It is the cost of the orchestrator's own
work for a complete 30-step lifecycle with every provider call returning
instantly. In production the same lifecycle is dominated by Meta, Google and
Cashfree round-trips plus Lambda cold starts, which together are three orders of
magnitude larger.

Its real value is as a **regression detector**: this number is stable, so if it
triples, the orchestrator started doing something it did not do before.

The p95 and p99 are identical because there are only 10 samples — at that size
both indices select the same element. That is a property of the sample size, not
a flat tail.

### Profiles defined but not run in CI

`test_profiles.py` defines five shapes; only `baseline` is parametrised into the
CI run, to keep the suite fast:

| Profile | Conversations | Concurrency | Budget | Run here |
| --- | ---: | ---: | ---: | --- |
| `baseline` | 10 | 2 | 30 s | **Yes** |
| `peak` | 30 | 10 | 60 s | No |
| `burst` | 40 | 40 | 60 s | No |
| `stress` | 60 | 60 | 90 s | No |
| `soak` | 80 | 5 | 120 s | No |

The four unrun profiles are available by parametrising `profile_name`. They are
**not** reported as passing.

---

## 3. Per-demo work — the numbers that decide the bill

These are ceilings, not targets. Crossing one means a loop or a duplicate call,
and each maps to a real failure that was caught this way.

| Counter | Measured | Ceiling | Crossing it means |
| --- | ---: | ---: | --- |
| LLM calls per demo | **2** | 4 | Objection extraction running more than once — directly the OpenAI bill |
| Tutor match calls per demo | **1** | 3 | Re-matching on every turn instead of once (+ at most one re-match after a decline) |
| Calendar events per demo | **1** | 1 | A reschedule creating a second event instead of patching. **This was a real bug.** |
| Outbound messages per demo | **7** | 10 | A message loop, which to a parent is a spam incident |

The calendar ceiling is exactly 1 rather than a range, deliberately. There is no
legitimate reason for a second event, so an assertion is better than a budget.

---

## 4. Contention

```
50-way slot contention resolved in 1.2ms
```

50 coroutines attempting to hold the same `(tutor, minute)`:

| Outcome | Count |
| --- | ---: |
| Won | **1** |
| Refused cleanly with `SlotConflict` | **49** |
| Unclassified exception | **0** |
| Time to resolve all 50 | 1.2 ms |

The test asserts *degrades rather than collapses*: the losers are refused with a
typed domain exception the caller can handle, not a driver error or a timeout.

The same exclusion was then confirmed **against the real database** — see
`final-e2e-report.md` §4, step 6 — so this is not only an in-memory property of
the fake store. Both paths enforce it, one by a Python check and one by a
partial unique index.

---

## 5. Per-turn cost does not grow with history

```
turn latency after 60 turns: 0.0ms
```

`0.0ms` means below the timer's resolution, not zero work. The assertion is
`< 500ms`, and the meaningful result is that turn 61 costs the same as turn 2.

The specific regression this guards: building context by loading the full
transcript, or every transition. Both are fine at ten turns and fatal at a
thousand — and the failure appears in production, months in, on the
conversations that matter most.

The bound is structural: the default history read is capped at 50 rows
regardless of conversation length.

---

## 6. Suite execution time

| Suite | Tests | Time |
| --- | ---: | ---: |
| Demo full suite (with coverage) | 389 | 21.93 s |
| Demo load profiles | 4 | 3.05 s |
| Tutor regression | 772 (+18 skipped) | 12.39 s |
| **Combined** | **1161** | **~37 s** |

Fast enough to run on every change, which is the property that matters — a suite
nobody runs has no value regardless of its coverage.

---

## 7. Design decisions with a performance consequence

| Decision | Consequence |
| --- | --- |
| `arm64` Lambda | Cheaper per millisecond; every dependency is pure Python so there is no build cost |
| One package for all 13 functions | One build; no version skew between workers exchanging typed events. Larger ZIP per function |
| Container-lifetime connection pool | A pool per request would exhaust the database's connection limit under any real concurrency |
| `statement_cache_size=0` | Server-side prepared statements break under RDS Proxy and PgBouncer transaction mode. Re-planning these small statements is negligible |
| `setup=` per acquisition | `search_path` survives asyncpg's `RESET ALL` on release. Two extra `SET` statements per acquire, which is the correct trade against silent `UndefinedTableError` |
| Statement timeout | One pathological query cannot hold a connection for the whole Lambda duration |
| Reserved concurrency per lane | Payment holds the smallest pool: low volume, high consequence, and a wide pool only widens the race window |
| Bounded history read (50) | Turn cost is flat in conversation length |
| Transactional outbox | The database is never held open across a provider call |

---

## 8. What must be measured before production

Ordered by how likely each is to be the thing that actually breaks:

1. **Lambda cold start** with the full package on `arm64`. The single largest
   contributor to a parent's perceived latency on the first message.
2. **Real provider latency** — Meta, Google Calendar (conference creation is
   asynchronous, which is why the scheduling worker has a 90s timeout),
   Cashfree.
3. **Database latency and connection behaviour** from Lambda, in whichever
   persistence mode is chosen. The pool assumptions are untested against a real
   account.
4. **Queue depth under burst**, and whether the `payment` lane's deliberately
   small pool becomes a bottleneck at real volume.
5. **The `peak` / `burst` / `stress` / `soak` profiles**, which exist and have
   never been run.

Until 1–5 are done, no latency or throughput claim about this system in
production is supportable, and none is made here.
