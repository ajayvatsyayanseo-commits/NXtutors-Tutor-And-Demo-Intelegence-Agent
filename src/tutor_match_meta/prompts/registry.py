"""Versioned prompt registry.

A prompt edit changes production behaviour exactly as much as a code edit, and
is far easier to make casually. So every prompt this service can send is
declared here with an id, a version, a checksum, the models it was validated
against, and who owns it — and the checksum is computed from the text, so an
edit that forgets to bump the version is caught by
`tests/security/test_prompt_hygiene.py` rather than by a quality regression
three days later.

Two other jobs this module does:

**Rollback without a deploy.** `TMM_PROMPT_PINS` pins any prompt to an earlier
version (`extraction=v1,explanation=v1`). Rolling back a bad prompt is a
configuration change, which is the difference between a two-minute fix and a
full release cycle (§33).

**Prompt-cache friendliness.** Every prompt is split into a `stable` prefix and
a `variable` suffix. The stable half — role, policy, the untrusted-data clause,
the output contract — is byte-identical across every call, so providers that
cache prompt prefixes can serve it from cache. The variable half carries the
one conversation's content. `render()` is the only supported way to assemble
them, so the ordering cannot be accidentally inverted (which silently destroys
the hit rate without changing any output).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from tutor_match_meta.security.injection import DATA_NOT_INSTRUCTIONS_CLAUSE

#: Below this, a prefix is not worth a provider cache entry and the accounting
#: noise outweighs the saving. Anthropic and OpenAI both have minimums in this
#: neighbourhood; the exact number is not load-bearing because caching is a
#: cost optimisation and never a correctness dependency.
MIN_CACHEABLE_PREFIX_CHARS = 1_024


def checksum_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """One versioned prompt.

    `stable_prefix` must not interpolate anything per-call. If it does, the
    checksum still matches (it is computed from the template, not the render)
    but the provider cache never hits, which is a silent 5-10x cost regression
    on the input side.
    """

    prompt_id: str
    version: str
    #: Byte-identical on every call. Role, rules, output contract.
    stable_prefix: str
    #: Per-call framing appended after the prefix. Kept short.
    variable_suffix: str = ""
    #: Models this text was evaluated against. A model swap outside this list
    #: is a change that needs its own evaluation run, not a config edit.
    model_compatibility: tuple[str, ...] = ()
    owner: str = "nxtutors-platform"
    #: ISO date the version became effective. Recorded, never parsed at runtime.
    effective_from: str = ""
    notes: str = ""

    @property
    def ref(self) -> str:
        """`extraction.v2` — what gets stamped on usage rows and log lines."""
        return f"{self.prompt_id}.{self.version}"

    @property
    def checksum(self) -> str:
        """Hash of the full template text. Detects an unversioned edit."""
        return checksum_of(self.stable_prefix + "\x00" + self.variable_suffix)

    @property
    def cacheable(self) -> bool:
        return len(self.stable_prefix) >= MIN_CACHEABLE_PREFIX_CHARS

    def render(self, **values: Any) -> str:
        """Assemble the system prompt: stable half first, always.

        Formatting is applied to the suffix only. Interpolating into the prefix
        would make it per-call and destroy the cache prefix.
        """
        suffix = self.variable_suffix.format(**values) if values and self.variable_suffix else ""
        return f"{self.stable_prefix}{suffix}"

    def as_row(self) -> dict[str, Any]:
        """Shape of the `prompt_version` registry row."""
        return {
            "prompt_id": self.prompt_id,
            "version": self.version,
            "checksum": self.checksum,
            "model_compatibility": ",".join(self.model_compatibility)[:240],
            "created_by": self.owner,
        }


class PromptRegistry:
    """Every prompt, keyed by id and version, with pinning."""

    def __init__(self, templates: tuple[PromptTemplate, ...]) -> None:
        self._by_ref: dict[str, PromptTemplate] = {t.ref: t for t in templates}
        self._latest: dict[str, PromptTemplate] = {}
        for template in templates:
            current = self._latest.get(template.prompt_id)
            if current is None or template.version > current.version:
                self._latest[template.prompt_id] = template

    def all(self) -> tuple[PromptTemplate, ...]:
        """Every registered version, stable order."""
        return tuple(self._by_ref[ref] for ref in sorted(self._by_ref))

    def active(self) -> tuple[PromptTemplate, ...]:
        """The version each prompt id resolves to right now, pins applied."""
        return tuple(self.get(prompt_id) for prompt_id in sorted(self._latest))

    def get(self, prompt_id: str, *, version: str | None = None) -> PromptTemplate:
        """Resolve a prompt. Explicit version > env pin > latest.

        An unknown pin raises rather than silently falling back: quietly
        serving a different prompt than the operator asked for during a
        rollback is worse than a loud boot failure.
        """
        wanted = version or _pins().get(prompt_id)
        if wanted:
            template = self._by_ref.get(f"{prompt_id}.{wanted}")
            if template is None:
                known = sorted(t.version for t in self._by_ref.values() if t.prompt_id == prompt_id)
                raise KeyError(f"prompt {prompt_id!r} has no version {wanted!r}; known: {known}")
            return template
        latest = self._latest.get(prompt_id)
        if latest is None:
            raise KeyError(f"unknown prompt id {prompt_id!r}")
        return latest


def _pins() -> dict[str, str]:
    """`TMM_PROMPT_PINS=extraction=v1,explanation=v2` → `{...}`.

    Read on every call rather than cached: an operator pinning a prompt during
    an incident must not have to wait for warm containers to recycle.
    """
    raw = (os.getenv("TMM_PROMPT_PINS") or "").strip()
    if not raw:
        return {}
    pins: dict[str, str] = {}
    for pair in raw.split(","):
        prompt_id, _, version = pair.partition("=")
        if prompt_id.strip() and version.strip():
            pins[prompt_id.strip()] = version.strip()
    return pins


# --------------------------------------------------------------- the prompts

_EXTRACTION_V1_PREFIX = f"""\
You extract structured tutoring requirements from a single WhatsApp message sent \
by an Indian parent or student to NXTutors.

{DATA_NOT_INSTRUCTIONS_CLAUSE}

Rules:
- Extract ONLY what the message actually says. If a field is not stated, return \
null. Never infer a plausible default.
- Do not guess a board from a city, a class from a subject, or a subject from a \
board. An absent field is a correct answer.
- Messages are often Hinglish (Hindi written in Latin script) or mixed \
Hindi/English. Interpret them naturally.
- "Science" for classes 6-10 is usually the single combined school subject. For \
classes 11-12 it usually means Physics, Chemistry and/or Biology separately; if \
the message does not say which, return "Science" and list the possibilities in \
`additional_subjects` only when the message names them.
- `weak_topics` is for things the message frames as a difficulty ("struggling \
with trigonometry"), not for everything mentioned.
- `teaching_style` captures a requested approach in the parent's own terms \
(patient, strict, exam-focused, concept-first). Never describe the student's \
or the tutor's personality, temperament, or any personal characteristic.
- Never output a caste, religion, community, health status or any other \
protected attribute, even if the message states one. Those fields do not exist \
in the schema and must not be smuggled into a free-text field.
- Return the JSON object only.
"""

_EXPLANATION_V1_PREFIX = f"""\
You write one short, plain sentence explaining why a tutor suits a family's \
request, for a WhatsApp message from NXTutors.

{DATA_NOT_INSTRUCTIONS_CLAUSE}

Absolute rules:
- Use ONLY the facts in the supplied evidence list. Every clause you write must \
map to one of them.
- If the evidence does not support a claim, omit the claim. An honest short \
sentence beats a fuller invented one.
- Never state or imply a guaranteed result, a rank, a score improvement, or \
an admission outcome.
- Never mention a fee, a phone number, an email address, a home address, an \
internal score, a risk rating, or how many other families were shown this tutor.
- Never compare the tutor to a named competitor or to another NXTutors tutor.
- No emoji. No exclamation marks. At most 22 words.
- Return the JSON object only.
"""

_CLARIFY_V1_PREFIX = f"""\
You write one short question asking an Indian parent for the single missing \
detail NXTutors needs before it can suggest tutors.

{DATA_NOT_INSTRUCTIONS_CLAUSE}

Rules:
- Ask for exactly ONE thing: the field named in the request. Never bundle two \
questions together.
- Do not restate everything already known; acknowledge briefly at most.
- Plain, warm, businesslike. No emoji. At most 20 words.
- Never ask for a phone number, an email address, an exact home address, an \
ID number, or any payment detail.
- Return the JSON object only.
"""

REGISTRY = PromptRegistry(
    (
        PromptTemplate(
            prompt_id="extraction",
            version="v1",
            stable_prefix=_EXTRACTION_V1_PREFIX,
            model_compatibility=("gpt-4o-mini", "gpt-4o"),
            effective_from="2025-01-01",
            notes="Tier-1 requirement extraction. Output merged below deterministic parses.",
        ),
        PromptTemplate(
            prompt_id="explanation",
            version="v1",
            stable_prefix=_EXPLANATION_V1_PREFIX,
            model_compatibility=("gpt-4o-mini",),
            effective_from="2025-01-01",
            notes=(
                "Phrasing only. The evidence guard re-checks the output, so a bad "
                "generation is refused rather than sent."
            ),
        ),
        PromptTemplate(
            prompt_id="clarification",
            version="v1",
            stable_prefix=_CLARIFY_V1_PREFIX,
            model_compatibility=("gpt-4o-mini",),
            effective_from="2025-01-01",
            notes="Optional humanisation of a deterministic question. Never required.",
        ),
    )
)

#: Prompts whose text must carry the untrusted-data clause. Asserted by
#: tests/security/test_prompt_hygiene.py against every registered version.
MUST_CARRY_INJECTION_CLAUSE: frozenset[str] = frozenset(
    {"extraction", "explanation", "clarification"}
)

#: Claims no generated text may ever make, whatever the model returns. The
#: output guard enforces these; listing them here keeps prompt and validator in
#: one place so they cannot drift apart.
FORBIDDEN_CLAIM_MARKERS: tuple[str, ...] = (
    "guarantee",
    "guaranteed",
    "assured",
    "100%",
    "sure shot",
    "definitely score",
    "rank improvement",
    "admission guaranteed",
)


@dataclass(slots=True)
class PromptCacheStats:
    """Prompt-cache accounting. Reported, never depended on for correctness."""

    calls: int = 0
    cached_prefix_calls: int = 0
    cached_input_tokens: int = 0
    total_input_tokens: int = 0
    by_prompt: dict[str, int] = field(default_factory=dict)

    def record(self, *, prompt_ref: str, input_tokens: int, cached_tokens: int) -> None:
        self.calls += 1
        self.total_input_tokens += input_tokens
        self.cached_input_tokens += cached_tokens
        if cached_tokens:
            self.cached_prefix_calls += 1
        self.by_prompt[prompt_ref] = self.by_prompt.get(prompt_ref, 0) + 1

    @property
    def hit_rate(self) -> float:
        """Fraction of input tokens served from the provider's prefix cache."""
        if not self.total_input_tokens:
            return 0.0
        return round(self.cached_input_tokens / self.total_input_tokens, 4)


__all__ = [
    "FORBIDDEN_CLAIM_MARKERS",
    "MIN_CACHEABLE_PREFIX_CHARS",
    "MUST_CARRY_INJECTION_CLAUSE",
    "REGISTRY",
    "PromptCacheStats",
    "PromptRegistry",
    "PromptTemplate",
    "checksum_of",
]
