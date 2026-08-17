# Handoff State Machine

Two state machines, deliberately separate.

## 1. Conversation FSM (inside TutorMatch)

`state/machine.py`. 12 states, an explicit transition table, optimistic locking.
Owns only the matching flow — it has no opinion about signup or demo scheduling.

```
NEW → COLLECTING_REQUIREMENTS → READY_TO_MATCH → MATCHING
        ↑            ↓                              ↓
        │       (ask one question)      SHORTLIST_READY → AWAITING_SELECTION
        │                                    ↓                   ↓
        └──────── REQUIREMENTS_CHANGED ──────┘          DEMO_REQUESTED → DEMO_HANDOFF
                                              NO_MATCH / HUMAN_REVIEW / ERROR_RECOVERABLE → CLOSED
```

An LLM may suggest intent but cannot force a transition: anything absent from
the table raises `InvalidTransition` and the state is preserved.

## 2. Handoff FSM (between agents)

```
                    ┌──────────────────────────────┐
   Meta webhook ───►│  LEAD INTAKE (owns inbound)  │
                    └──────────────┬───────────────┘
                                   │ deterministic intent route
              ┌────────────────────┼─────────────────────┐
              ▼                    ▼                     ▼
      ONBOARDING agent      TUTORMATCH (us)        other agents
              │                    │
              │           ┌────────┴────────┐
              │           │ HandoffStatus   │
              │           ├─────────────────┤
              │           │ HANDLED         │→ reply_text, Lead Intake sends
              │           │ ACCEPTED        │→ async; nothing to say now
              │           │ DECLINED        │→ not ours; Lead Intake keeps it
              │           │ NEEDS_HANDOFF   │→ + continuation_token
              │           │ HUMAN_REVIEW    │→ coordinator takes over
              │           │ ERROR           │→ Lead Intake uses its own fallback
              │           └─────────────────┘
              │                    │
              └────── continuation_token ──────┘
                   (MatchSession survives the detour)
```

**TutorMatch has no edge back to Lead Intake.** That single missing edge makes
`Lead Intake → TutorMatch → Lead Intake → …` structurally impossible rather than
merely discouraged. Verified by `find_cycles() == []`.

## The pause/resume journey

```
turn 1  "I need a maths tutor for class 8, I don't have an account"
        → route(): FIND_TUTOR, owned, account_required → NEEDS_HANDOFF
        → we return continuation_token, say nothing
        → Lead Intake forwards to onboarding

turn 2  onboarding completes, account created

turn 3  Lead Intake returns the token + lead_id
        → codec verifies (signature, conversation binding, TTL)
        → MatchSession resumes; the subject is NOT asked again
        → HANDLED with the shortlist
```

The token holds a session id, a conversation binding and an expiry — no PII. The
requirement lives in our database keyed by the session id.

## Loop prevention

Every envelope carries `hop_count` and an ordered `visited_agents` chain.

| Guard | Effect |
| --- | --- |
| Destination already in the chain | `LoopDetected` |
| `hop_count > MAX_HOPS` (6) | `LoopDetected` |
| Edge not in `ALLOWED_EDGES` | `ForbiddenHandoff` |
| Agent hands off to itself | Validation error |

## Idempotency chain

```
Meta wa_message_id
  → Lead Intake forwards it unchanged
    → our dedup_key = sha256("handoff", conversation_id, wa_message_id)
      → idempotency table claim (DB decides the winner)
        → SQS FIFO MessageDeduplicationId
          → outbox dedup_key (unique index)
```

A redelivery at any layer produces `duplicate=True` and **no second reply**.
