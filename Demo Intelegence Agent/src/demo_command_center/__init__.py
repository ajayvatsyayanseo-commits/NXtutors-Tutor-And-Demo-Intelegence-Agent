"""NXTutors Demo Intelegence Agent (`demo-command-center-agent`).

One logical agent owning the NXTutors demo lifecycle. Eight capability modules
live under `capabilities/`, each the home of one register agent's skills; a
single deterministic orchestrator owns conversation state and is the only thing
allowed to move it.
"""

from demo_command_center.version import AGENT_ID, RELEASE_VERSION

__all__ = ["AGENT_ID", "RELEASE_VERSION"]
