"""Emergency kill switches.

Distinct from feature flags. A flag is a rollout decision made at deploy time; a
kill switch is an operator stopping something *right now*, without a deploy,
during an incident.

Every switch declares its **safe behaviour** as data rather than leaving it to
whoever writes the call site. That matters because the wrong safe behaviour is
worse than no switch: pausing the LLM should quietly degrade to deterministic
matching, but pausing outbound must *hold* messages, not drop them — and those
two are one careless line apart.

The rule: a paused subsystem never fabricates and never silently loses work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class Switch(StrEnum):
    MATCHING_PAUSED = "MATCHING_PAUSED"
    LLM_PAUSED = "LLM_PAUSED"
    RAG_PAUSED = "RAG_PAUSED"
    WEBSITE_WRITEBACK_PAUSED = "WEBSITE_WRITEBACK_PAUSED"
    OUTBOUND_PAUSED = "OUTBOUND_PAUSED"
    MEMORY_WRITES_PAUSED = "MEMORY_WRITES_PAUSED"
    AUTO_DEMO_PAUSED = "AUTO_DEMO_PAUSED"


class SafeBehaviour(StrEnum):
    """What a paused subsystem does. Each is a deliberate, documented choice."""

    #: Decline the handoff; Lead Intake keeps answering. No parent-visible error.
    DECLINE_TO_CALLER = "decline_to_caller"
    #: Skip the optional enrichment and continue deterministically.
    DEGRADE_SILENTLY = "degrade_silently"
    #: Keep the work durably and replay when unpaused. Never drop.
    HOLD_IN_OUTBOX = "hold_in_outbox"
    #: Escalate to a human rather than proceeding unsupervised.
    ESCALATE_TO_HUMAN = "escalate_to_human"


@dataclass(frozen=True, slots=True)
class SwitchPolicy:
    switch: Switch
    behaviour: SafeBehaviour
    #: Why this behaviour and not another. Read during an incident.
    rationale: str
    #: True when pausing this loses nothing — purely a degradation.
    lossless: bool


POLICIES: dict[Switch, SwitchPolicy] = {
    Switch.MATCHING_PAUSED: SwitchPolicy(
        Switch.MATCHING_PAUSED,
        SafeBehaviour.DECLINE_TO_CALLER,
        "Lead Intake owns the conversation and can still answer. Declining is "
        "invisible to the parent; erroring is not.",
        lossless=True,
    ),
    Switch.LLM_PAUSED: SwitchPolicy(
        Switch.LLM_PAUSED,
        SafeBehaviour.DEGRADE_SILENTLY,
        "Deterministic extraction and scoring cover the well-formed majority. "
        "Ambiguous messages get one clarifying question instead of a guess.",
        lossless=True,
    ),
    Switch.RAG_PAUSED: SwitchPolicy(
        Switch.RAG_PAUSED,
        SafeBehaviour.DEGRADE_SILENTLY,
        "RAG is supplementary context only. Structured matching is unaffected, "
        "so exact tutor filtering still works.",
        lossless=True,
    ),
    Switch.WEBSITE_WRITEBACK_PAUSED: SwitchPolicy(
        Switch.WEBSITE_WRITEBACK_PAUSED,
        SafeBehaviour.HOLD_IN_OUTBOX,
        "A demo request is a real commitment to a parent. Hold it durably and "
        "replay on unpause rather than dropping it.",
        lossless=False,
    ),
    Switch.OUTBOUND_PAUSED: SwitchPolicy(
        Switch.OUTBOUND_PAUSED,
        SafeBehaviour.HOLD_IN_OUTBOX,
        "The decision is already made and persisted. Holding the message keeps "
        "it deliverable; regenerating later could produce a different shortlist.",
        lossless=False,
    ),
    Switch.MEMORY_WRITES_PAUSED: SwitchPolicy(
        Switch.MEMORY_WRITES_PAUSED,
        SafeBehaviour.DEGRADE_SILENTLY,
        "Memory is audit and personalisation, never a matching input. Events "
        "spool to the WAL and replay.",
        lossless=True,
    ),
    Switch.AUTO_DEMO_PAUSED: SwitchPolicy(
        Switch.AUTO_DEMO_PAUSED,
        SafeBehaviour.ESCALATE_TO_HUMAN,
        "Booking a demo without supervision is the highest-commitment action "
        "the agent takes. A coordinator confirms instead.",
        lossless=False,
    ),
}


@runtime_checkable
class KillSwitchReader(Protocol):
    async def paused(self, switch: str) -> bool: ...

    async def all_paused(self) -> dict[str, bool]: ...


class NoKillSwitches:
    """Nothing paused. Local development and tests."""

    async def paused(self, switch: str) -> bool:
        return False

    async def all_paused(self) -> dict[str, bool]:
        return {}


class KillSwitches:
    """Reads switches and reports the documented safe behaviour."""

    def __init__(self, reader: KillSwitchReader) -> None:
        self._reader = reader

    async def is_paused(self, switch: Switch) -> bool:
        return await self._reader.paused(switch.value)

    async def behaviour_for(self, switch: Switch) -> SafeBehaviour | None:
        """The safe behaviour when paused, or None when running normally."""
        if not await self.is_paused(switch):
            return None
        return POLICIES[switch].behaviour

    async def snapshot(self) -> dict[str, bool]:
        """Every switch, for the health endpoint. Unset switches read False."""
        active = await self._reader.all_paused()
        return {switch.value: active.get(switch.value, False) for switch in Switch}

    async def any_lossy_pause(self) -> list[str]:
        """Paused switches that are holding real work.

        These are the ones an operator must remember to unpause — a lossless
        degradation can sit paused for a week, a held outbox cannot.
        """
        active = await self.snapshot()
        return sorted(
            name
            for name, paused in active.items()
            if paused and not POLICIES[Switch(name)].lossless
        )
