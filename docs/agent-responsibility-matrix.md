# Agent Responsibility Matrix

Derived from source inspection of the checkouts in
`Ready In Production Agents/`, not from intent. Where the code and a reasonable
expectation disagreed, the code won.

**O** = owns · **C** = contributes · **R** = reads · **—** = no involvement

| Responsibility | Lead Intake | Onboarding | TutorMatch | Demo CC | Chitragupta | Website |
| --- | :-: | :-: | :-: | :-: | :-: | :-: |
| Public Meta WhatsApp webhook | **O** | — | — | — | — | — |
| WhatsApp signature / verify token | **O** | — | — | — | — | — |
| Outbound WhatsApp delivery | **O** | — | — | — | — | — |
| Inbound message dedup (provider id) | **O** | C | C | C | — | — |
| Top-level intent routing | **O** | — | C | — | — | — |
| Lead identity create/resolve | **O** | C | R | R | — | **O** |
| Signup / account creation | — | **O** | — | — | — | **O** |
| Tutor requirement extraction | C | — | **O** | — | — | — |
| Tutor candidate retrieval | — | — | **O** | — | — | **O** (source) |
| Tutor scoring and ranking | — | — | **O** | — | — | — |
| Scoring policy + version | — | — | **O** | — | — | — |
| Shortlist explanation | — | — | **O** | — | — | — |
| Canonical tutor profile link | — | — | C (derived) | — | — | **O** (route) |
| Demo scheduling lifecycle | — | — | C (request) | **O** | — | **O** (record) |
| Cross-agent memory | C | C | C | C | **O** | — |
| Memory RBAC / provenance | — | — | — | — | **O** | — |
| Website account/profile records | — | C | — | — | — | **O** |
| Tutor availability | — | — | **O** | — | — | — |
| Match decision audit trail | — | — | **O** | — | C | — |

## What each agent owns, in one line

- **Lead Intake** — the WhatsApp front door. Receives, verifies, classifies,
  routes, and **sends every outbound message**.
- **Onboarding** — signup and profile creation. Called by Lead Intake.
- **TutorMatch (this service)** — tutor discovery and matching only. Called as an
  internal handoff; returns text for Lead Intake to send.
- **Demo Command Center** — demo lifecycle after a tutor is chosen.
- **Chitragupta** — shared memory, provenance and governance. Infrastructure.
- **Website (Laravel/MySQL)** — canonical account, profile and enquiry records.

## What TutorMatch deliberately does *not* do

| Not ours | Why |
| --- | --- |
| Receive the Meta webhook | Lead Intake owns it. A second webhook double-delivers. |
| Send WhatsApp messages | Lead Intake owns the sender. Two senders = double reply. |
| Create accounts | Onboarding owns it. Duplicating gives the parent two flows. |
| Own the website schema | Laravel's service layer applies the business rules. |
| Re-implement global memory | Chitragupta owns provenance and RBAC. |
| Classify non-matching intents | Lead Intake's router decides; we only accept or decline. |

Enforced by `integrations/agents/graph.py` and
`tests/contract/test_agent_harmony_contracts.py`.
