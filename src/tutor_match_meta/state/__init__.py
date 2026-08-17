"""Deterministic conversation state with optimistic locking."""

from tutor_match_meta.state.machine import (
    FSM_VERSION,
    TERMINAL,
    TRANSITIONS,
    ConversationSnapshot,
    ConversationState,
    InvalidTransition,
    OptimisticLockError,
    TransitionRecord,
    Trigger,
    can_transition,
    next_state,
    reachable_states,
    transition,
    try_transition,
)

__all__ = [
    "FSM_VERSION",
    "TERMINAL",
    "TRANSITIONS",
    "ConversationSnapshot",
    "ConversationState",
    "InvalidTransition",
    "OptimisticLockError",
    "TransitionRecord",
    "Trigger",
    "can_transition",
    "next_state",
    "reachable_states",
    "transition",
    "try_transition",
]
