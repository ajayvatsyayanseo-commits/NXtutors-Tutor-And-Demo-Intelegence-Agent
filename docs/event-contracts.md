# Event Contracts

Every event is `<domain>.<name>.v<version>`. Defined in
`src/tutor_match_meta/contracts/events.py`.

## Rules

1. **A released version never changes meaning.** Adding an optional field is
   fine; changing what a field means requires `.v2`.
2. **Consumers ignore unknown fields.** Every payload model is `extra="allow"`.
   Strict models would turn every additive change into a coordinated multi-repo
   release.
3. **No PII in payloads.** `conversation_ref` is a peppered hash. A consumer that
   needs the phone number resolves it from the website under its own authority.
4. **`match_session_id` correlates one matching run**; `trace_id` correlates a
   whole conversation turn across agents.

## Catalogue

| Event | Emitted when | Key fields |
| --- | --- | --- |
| `match.requested.v1` | A matching intent is accepted | `match_session_id`, `intent` |
| `match.requirements.updated.v1` | The requirement changes | `known_fields`, `missing_fields`, `ready_to_match` |
| `match.ready.v1` | Enough is known to match | `match_session_id` |
| `match.shortlist.generated.v1` | A shortlist is produced | `tutor_ids`, `policy_id/version/checksum`, `weight_coverage` |
| `match.shortlist.sent.v1` | Delivered to the parent | `delivered_by` — exactly one agent may claim this |
| `match.candidate.selected.v1` | Parent picks a tutor | `tutor_id`, `rank` |
| `match.demo.requested.v1` | Demo requested | `tutor_id`, `preferred_time` |
| `match.no_candidate.v1` | Nothing cleared the bar | `reason`, `top_rejection_rule` |
| `match.human_handoff.requested.v1` | Escalation | `reason`, compact `summary` |
| `match.feedback.received.v1` | Outcome recorded | `outcome` |
| `match.replacement.requested.v1` | Replacement asked for | `previous_tutor_id` (excluded next time) |
| `match.closed.v1` | Conversation ends | `outcome`, `turns` |
| `outbound.message.requested.v1` | **Only** under `tutor_match_sends` | `body`, `idempotency_key` — no recipient |

## The envelope

Every event travels in `AgentEnvelopeV1`:

| Field | Purpose |
| --- | --- |
| `trace_id` | Constant across the whole chain |
| `correlation_id` | Business grouping (the conversation) |
| `causation_id` | The single event that produced this one |
| `hop_count` / `visited_agents` | Loop prevention, carried with the event |
| `idempotency_key` | Deterministic; diverges per destination |
| `purpose` | Scope for memory and data access |
| `conversation_ref` | Peppered hash, never a phone number |

Carrying loop state *in the envelope* rather than in a service means a cycle is
caught at the receiving edge even when the two agents in the loop know nothing
about each other.

## Outbound idempotency

`sha256(conversation_ref + source_event_id + purpose)`

Including the purpose means a shortlist and a follow-up question caused by the
same inbound event are distinct sends, while a **retry** of either is not.

## Compatibility testing

`tests/contract/test_agent_harmony_contracts.py` asserts that every catalogued
type is versioned, unknown additive fields parse cleanly, an unknown event
*type* raises rather than being silently dropped, and the outbound event carries
no recipient field.
