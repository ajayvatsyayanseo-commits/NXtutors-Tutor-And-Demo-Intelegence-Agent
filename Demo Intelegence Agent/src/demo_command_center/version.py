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

#: Which capability module implements each composed agent. Asserted by
#: `tests/contract/test_composed_agents.py`: a registry id with no module behind
#: it is a capability we claim to have and do not.
CAPABILITY_MODULES: Final[dict[str, str]] = {
    "018": "demo_command_center.capabilities.forecasting.service",
    "025": "demo_command_center.capabilities.scheduling.service",
    "026": "demo_command_center.capabilities.reminders.service",
    "031": "demo_command_center.capabilities.objection_extraction.service",
    "032": "demo_command_center.capabilities.conversion.service",
    "034": "demo_command_center.capabilities.discounts.service",
    "036": "demo_command_center.capabilities.paid_transition.service",
    "129": "demo_command_center.capabilities.monitoring.service",
}


def build_info() -> dict[str, str]:
    """What is deployed. Read by `/version`, the doctor and every audit row.

    The commit sha is read from the environment because it is injected at build
    time; a build with no sha reports `unknown` rather than failing, since a
    missing label must not stop a deploy from reporting its own identity.
    """
    import os

    return {
        "agent_id": AGENT_ID,
        "version": RELEASE_VERSION,
        "contract_version": CONTRACT_VERSION,
        "commit": os.environ.get("DCC_GIT_SHA", "unknown"),
        "composed_agents": ",".join(COMPOSED_AGENT_IDS),
    }
