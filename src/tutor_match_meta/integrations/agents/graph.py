"""The allowed handoff graph.

Derived from the code, not from a whiteboard:

* Lead Intake owns the Meta webhook (`app/api/whatsapp.py`) and the outbound
  sender (`app/services/whatsapp_client.py`), so it is the only node with an
  edge to a parent.
* Lead Intake already forwards to onboarding
  (`app/services/onboarding_router.py`) and lists `tutor_matching_agent` as an
  integration target (`app/services/integrations/router.py`).
* **TutorMatch has no edge back to Lead Intake.** We answer the request we were
  given; we do not call our caller. That single missing edge is what makes
  `Lead Intake -> TutorMatch -> Lead Intake -> ...` structurally impossible
  rather than merely discouraged.

`ALLOWED_EDGES` is the whole policy. Adding an edge is a deliberate change that
has to be justified against the cycle check below.
"""

from __future__ import annotations

from tutor_match_meta.contracts.envelope import AgentId, LoopDetected

#: `source -> {permitted destinations}`. Absent means forbidden.
ALLOWED_EDGES: dict[AgentId, frozenset[AgentId]] = {
    # The router. Owns inbound and outbound; may dispatch to any business agent.
    AgentId.LEAD_INTAKE: frozenset(
        {
            AgentId.ONBOARDING,
            AgentId.TUTOR_MATCH,
            AgentId.DEMO_COMMAND_CENTER,
            AgentId.COUNSELOR,
            AgentId.CRM,
            AgentId.NOTIFICATION,
            AgentId.SCHEDULING,
            AgentId.CHITRAGUPTA,
            AgentId.HUMAN,
        }
    ),
    # Onboarding returns control by replying, not by calling anyone back.
    AgentId.ONBOARDING: frozenset({AgentId.CHITRAGUPTA, AgentId.WEBSITE, AgentId.HUMAN}),
    # TutorMatch may read/write its own dependencies and escalate to a human,
    # but must never call a conversational agent. See the module docstring.
    AgentId.TUTOR_MATCH: frozenset(
        {
            AgentId.CHITRAGUPTA,
            AgentId.WEBSITE,
            AgentId.DEMO_COMMAND_CENTER,
            AgentId.NOTIFICATION,
            AgentId.HUMAN,
        }
    ),
    AgentId.DEMO_COMMAND_CENTER: frozenset(
        {AgentId.CHITRAGUPTA, AgentId.WEBSITE, AgentId.NOTIFICATION, AgentId.HUMAN}
    ),
    # Infrastructure and terminal nodes originate nothing.
    AgentId.CHITRAGUPTA: frozenset(),
    AgentId.WEBSITE: frozenset(),
    AgentId.COUNSELOR: frozenset({AgentId.HUMAN}),
    AgentId.CRM: frozenset(),
    AgentId.NOTIFICATION: frozenset(),
    AgentId.SCHEDULING: frozenset({AgentId.NOTIFICATION}),
    AgentId.HUMAN: frozenset(),
}

#: The only agent permitted to talk to a parent on WhatsApp. Asserted by
#: tests/security so a second sender cannot be introduced quietly.
WHATSAPP_OWNER: AgentId = AgentId.LEAD_INTAKE


class ForbiddenHandoff(LoopDetected):
    """The graph does not permit this edge."""


def is_edge_allowed(source: AgentId, destination: AgentId) -> bool:
    return destination in ALLOWED_EDGES.get(source, frozenset())


def assert_edge_allowed(source: AgentId, destination: AgentId) -> None:
    if not is_edge_allowed(source, destination):
        raise ForbiddenHandoff(
            f"handoff {source.value} -> {destination.value} is not in the allowed graph",
            (source.value, destination.value),
        )


def find_cycles() -> list[tuple[str, ...]]:
    """Every directed cycle in the graph. Must be empty.

    A depth-first search rather than a hand-maintained list, so adding an edge
    that closes a loop fails the test immediately instead of at 2am.
    """
    cycles: list[tuple[str, ...]] = []

    def walk(node: AgentId, path: list[AgentId], seen: set[AgentId]) -> None:
        for nxt in sorted(ALLOWED_EDGES.get(node, frozenset()), key=lambda a: a.value):
            if nxt in seen:
                start = path.index(nxt)
                cycles.append(tuple(a.value for a in [*path[start:], nxt]))
                continue
            walk(nxt, [*path, nxt], seen | {nxt})

    for origin in ALLOWED_EDGES:
        walk(origin, [origin], {origin})
    return cycles


def reachable_from(source: AgentId) -> set[AgentId]:
    seen: set[AgentId] = set()
    frontier = [source]
    while frontier:
        node = frontier.pop()
        for nxt in ALLOWED_EDGES.get(node, frozenset()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen
