"""Release identity.

Stamped onto every state transition, decision record and outbound event so a
rollback can be reasoned about after the fact: `docs/operations/rollback.md`
depends on being able to ask "which release produced this row".
"""

from __future__ import annotations

from typing import Final

#: The single agent id this process presents to every other NXTutors agent.
#: Matches `AgentId.DEMO_COMMAND_CENTER` in the Tutor Intelligence Agent's
#: envelope, so no upstream change is needed to route to us.
AGENT_ID: Final = "demo_command_center_agent"

RELEASE_VERSION: Final = "0.1.0"

#: Bumped whenever a persisted contract shape changes in a non-additive way.
CONTRACT_VERSION: Final = "1"

#: The register agents whose skills this one agent subsumes. Recorded on audit
#: events so capability lineage stays traceable even though the acting agent id
#: is always the single meta agent above.
COMPOSED_AGENT_IDS: Final[tuple[str, ...]] = (
    "018",  # Demo Success Forecast
    "025",  # Demo Scheduling
    "026",  # Demo Reminder
    "031",  # Demo Objection Extraction
    "032",  # Post-Demo Conversion
    "034",  # Discount Suggestion
    "036",  # Demo-to-Paid Transition
    "129",  # Demo Monitoring Regional
)
