"""Feature flags, shadow mode and percentage rollout.

Rolling a new matcher into a live WhatsApp number is the risky part of this
project, so the controls are first-class rather than an afterthought.

**Shadow mode** is the important one. With `shadow_mode` on, TutorMatch does the
entire job — extraction, filtering, scoring, ranking, explanation — persists the
decision, emits the metrics, and then **declines the handoff**. Lead Intake
answers exactly as it does today. That gives real production comparison data
with zero parent-visible risk, and it is a genuinely different thing from
"disabled": disabled produces no data at all.

**Bucketing is by conversation, not by request.** A parent must not get the new
matcher on turn one and the old behaviour on turn two.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256


class Flag(StrEnum):
    ENABLED = "tutor_match_enabled"
    SHADOW_MODE = "tutor_match_shadow_mode"
    LLM_ENABLED = "tutor_match_llm_enabled"
    WRITEBACK_ENABLED = "tutor_match_writeback_enabled"
    RAG_ENABLED = "tutor_match_rag_enabled"
    MEMORY_ENABLED = "tutor_match_memory_enabled"
    AUTO_DEMO_ENABLED = "tutor_match_auto_demo_enabled"


class Mode(StrEnum):
    """What this conversation actually gets."""

    #: Not in the rollout bucket, or the kill switch is off. We decline and
    #: Lead Intake keeps the conversation.
    DISABLED = "disabled"
    #: Full processing, decision persisted, **no reply returned**.
    SHADOW = "shadow"
    #: Normal operation.
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class FeatureFlags:
    """Resolved flag state. Immutable per request."""

    enabled: bool = False
    shadow_mode: bool = True
    llm_enabled: bool = False
    writeback_enabled: bool = False
    rag_enabled: bool = False
    memory_enabled: bool = False
    auto_demo_enabled: bool = False
    #: 0 = nobody, 100 = everybody.
    percentage_rollout: int = 0
    #: Always live regardless of bucketing. For staff test conversations.
    always_on_refs: frozenset[str] = frozenset()

    def mode_for(self, conversation_ref: str) -> Mode:
        """Resolve the mode for one conversation.

        Defaults are deliberately conservative: a fresh deployment with no
        configuration is `DISABLED`, not silently live on a production number.
        """
        if not self.enabled:
            return Mode.DISABLED
        if conversation_ref in self.always_on_refs:
            return Mode.SHADOW if self.shadow_mode else Mode.LIVE
        if not in_rollout(conversation_ref, self.percentage_rollout):
            return Mode.DISABLED
        return Mode.SHADOW if self.shadow_mode else Mode.LIVE

    def is_live(self, conversation_ref: str) -> bool:
        return self.mode_for(conversation_ref) is Mode.LIVE

    def describe(self) -> dict[str, object]:
        """Flag state for the health endpoint. No secrets, no identifiers."""
        return {
            Flag.ENABLED.value: self.enabled,
            Flag.SHADOW_MODE.value: self.shadow_mode,
            Flag.LLM_ENABLED.value: self.llm_enabled,
            Flag.WRITEBACK_ENABLED.value: self.writeback_enabled,
            Flag.RAG_ENABLED.value: self.rag_enabled,
            Flag.MEMORY_ENABLED.value: self.memory_enabled,
            Flag.AUTO_DEMO_ENABLED.value: self.auto_demo_enabled,
            "tutor_match_percentage_rollout": self.percentage_rollout,
            "always_on_count": len(self.always_on_refs),
        }


def in_rollout(conversation_ref: str, percentage: int) -> bool:
    """Stable bucketing by conversation.

    Hashed rather than random so the same conversation always lands in the same
    bucket — a parent flipping between old and new behaviour mid-conversation
    would be worse than either behaviour alone. Hashed rather than modulo-ing a
    counter so buckets stay evenly distributed as conversation ids change shape.
    """
    if percentage <= 0:
        return False
    if percentage >= 100:
        return True
    digest = sha256(f"tutor_match:{conversation_ref}".encode()).digest()
    return (int.from_bytes(digest[:4], "big") % 100) < percentage


def from_settings(settings: object) -> FeatureFlags:
    """Build flags from `Settings`. Missing values fall back to safe defaults."""

    def get(name: str, default: object) -> object:
        return getattr(settings, name, default)

    raw_refs = str(get("flag_always_on_refs", "") or "")
    return FeatureFlags(
        enabled=bool(get("flag_enabled", False)),
        shadow_mode=bool(get("flag_shadow_mode", True)),
        llm_enabled=bool(get("flag_llm_enabled", False)),
        writeback_enabled=bool(get("flag_writeback_enabled", False)),
        rag_enabled=bool(get("flag_rag_enabled", False)),
        memory_enabled=bool(get("flag_memory_enabled", False)),
        auto_demo_enabled=bool(get("flag_auto_demo_enabled", False)),
        percentage_rollout=max(0, min(100, int(str(get("flag_percentage_rollout", 0) or 0)))),
        always_on_refs=frozenset(r.strip() for r in raw_refs.split(",") if r.strip()),
    )


@dataclass(slots=True)
class ShadowComparison:
    """What shadow mode records for offline comparison."""

    conversation_ref: str
    match_session_id: str
    would_have_matched: bool
    shortlist_tutor_ids: tuple[str, ...]
    policy_ref: str
    weight_coverage: float
    latency_ms: int
    fabrication_violations: int = 0

    def as_metric_properties(self) -> dict[str, object]:
        """Non-identifying properties for the EMF line."""
        return {
            "shadow": True,
            "would_have_matched": self.would_have_matched,
            "shortlist_size": len(self.shortlist_tutor_ids),
            "policy_ref": self.policy_ref,
            "weight_coverage": round(self.weight_coverage, 3),
            "latency_ms": self.latency_ms,
            "fabrication_violations": self.fabrication_violations,
        }
