"""Requirement-extraction prompt (Tier 1).

The model's only job here is to read one message and fill a strict schema. It
never sees tutor data, never chooses a policy, and never decides anything — the
output is merged in at LLM provenance, below anything the parsers found.

`PROMPT_VERSION` is recorded on every call so a quality regression can be traced
to a prompt change rather than guessed at.
"""

from __future__ import annotations

from typing import Any

from tutor_match_meta.prompts.registry import REGISTRY

PROMPT_ID = "extraction"


def extraction_prompt() -> tuple[str, str]:
    """`(system_prompt, prompt_ref)` for this call.

    Resolved per call, not at import, so an operator pinning an earlier version
    with `TMM_PROMPT_PINS` during an incident does not have to wait for warm
    Lambda containers to recycle. The text itself lives in
    `prompts/registry.py` beside its checksum and model-compatibility record —
    a second copy of a prompt is how a rollback silently rolls back only one of
    them.
    """
    template = REGISTRY.get(PROMPT_ID)
    return template.render(), template.ref


#: Strict schema. `additionalProperties: false` plus `required` on every key
#: means a malformed response is rejected by the provider wrapper rather than
#: silently producing partial data.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "subject",
        "additional_subjects",
        "board",
        "student_class",
        "exam",
        "mode",
        "learning_goal",
        "teaching_style",
        "language_preference",
        "topics",
        "weak_topics",
    ],
    "properties": {
        "subject": {
            "type": ["string", "null"],
            "maxLength": 60,
            "description": "Primary subject, or null if not stated.",
        },
        "additional_subjects": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 60},
        },
        "board": {
            "type": ["string", "null"],
            "maxLength": 40,
            "description": "CBSE, ICSE, ISC, IB, IGCSE, Cambridge, State Board, NIOS, or null.",
        },
        "student_class": {
            "type": ["string", "null"],
            "maxLength": 30,
            "description": "e.g. 'Class 10', 'LKG'. Null if not stated.",
        },
        "exam": {
            "type": ["string", "null"],
            "maxLength": 40,
            "description": "JEE, NEET, NTSE, CUET, SAT, Olympiad, Board Exam, or null.",
        },
        "mode": {
            "type": ["string", "null"],
            "enum": ["home", "online", "hybrid", None],
        },
        "learning_goal": {"type": ["string", "null"], "maxLength": 200},
        "teaching_style": {
            "type": ["string", "null"],
            "maxLength": 200,
            "description": "Requested teaching approach only. Never a personality judgement.",
        },
        "language_preference": {"type": ["string", "null"], "maxLength": 40},
        "topics": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 60}},
        "weak_topics": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 60},
        },
    },
}
