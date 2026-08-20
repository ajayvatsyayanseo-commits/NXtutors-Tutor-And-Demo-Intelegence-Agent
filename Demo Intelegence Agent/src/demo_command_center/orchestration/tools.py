"""The closed tool registry — what a model may propose, and what happens next.

A model proposal is a *request*, never an instruction. `authorise()` runs the
full pipeline before anything executes:

    schema → ownership → state → authorization → policy
           → rate limit → idempotency → execution → result validation → audit

Every stage can only refuse. None can widen what a tool may do.

Two absences are the design:

* **There is no tool for arbitrary SQL, HTTP, URL fetch, shell, code execution,
  credential access or direct database mutation.** Not "blocked" — absent.
  `FORBIDDEN_TOOL_NAMES` exists purely so a test can assert none is ever added.
* **`EXCLUSIVE` tools cannot run in parallel.** Booking a slot, creating a
  calendar event, committing a discount, creating a payment order, activating a
  subscription and handing off ownership are all single-flight: two concurrent
  proposals for the same conversation produce one execution and one refusal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from demo_command_center.state.states import DemoState
from demo_command_center.state.triggers import Actor


class SideEffect(StrEnum):
    """How much damage a mis-execution does. Drives idempotency strictness."""

    #: No external effect. Safe to run twice, safe to run in parallel.
    READ_ONLY = "read_only"
    #: Writes Demo state only. Idempotent by key.
    LOCAL_WRITE = "local_write"
    #: Contacts a customer or a tutor. Duplicates are visible and embarrassing.
    CUSTOMER_VISIBLE = "customer_visible"
    #: Creates or moves an external booking.
    EXTERNAL_BOOKING = "external_booking"
    #: Money. A duplicate is a chargeback.
    FINANCIAL = "financial"


#: Side-effect levels that must never run concurrently for one conversation.
EXCLUSIVE: frozenset[SideEffect] = frozenset({SideEffect.EXTERNAL_BOOKING, SideEffect.FINANCIAL})

#: Names that must never appear in the registry. Asserted by
#: `tests/security/test_tool_registry.py`, so adding one fails a test rather
#: than review.
FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "execute_sql",
        "run_sql",
        "query_database",
        "http_request",
        "fetch_url",
        "browse",
        "run_shell",
        "execute_code",
        "eval",
        "read_secret",
        "get_credentials",
        "write_row",
        "update_table",
    }
)


class Refusal(StrEnum):
    """Why a proposal was refused. Every value is a distinct metric."""

    UNKNOWN_TOOL = "unknown_tool"
    SCHEMA_INVALID = "schema_invalid"
    NOT_OWNER = "not_owner"
    WRONG_STATE = "wrong_state"
    ACTOR_NOT_PERMITTED = "actor_not_permitted"
    POLICY_DENIED = "policy_denied"
    RATE_LIMITED = "rate_limited"
    ALREADY_EXECUTED = "already_executed"
    CONCURRENT_EXCLUSIVE = "concurrent_exclusive"
    RESULT_INVALID = "result_invalid"


class ToolRefused(Exception):
    def __init__(self, tool: str, refusal: Refusal, detail: str = "") -> None:
        super().__init__(f"{tool}: {refusal.value}{f' ({detail})' if detail else ''}")
        self.tool = tool
        self.refusal = refusal
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool's complete contract. Data, not behaviour."""

    name: str
    description: str
    #: JSON Schema for the model's proposal. `additionalProperties: false`
    #: everywhere — a permissive schema is how extra commentary gets stored.
    input_schema: dict[str, Any]
    #: States the tool may be invoked from. Empty means any non-terminal state.
    allowed_states: frozenset[DemoState]
    #: Actors permitted to trigger it.
    allowed_actors: frozenset[Actor]
    side_effect: SideEffect
    #: Seconds. A tool that cannot finish in this has failed.
    timeout_seconds: float
    #: Whether re-running with the same idempotency key is a no-op.
    idempotent: bool = True
    #: Fields written to `dcc_tool_executions` for audit.
    audit_fields: tuple[str, ...] = ()
    #: Result shape. Validated after execution — a provider that returns
    #: something unexpected must not become state.
    result_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def exclusive(self) -> bool:
        return self.side_effect in EXCLUSIVE

    @property
    def model_facing(self) -> bool:
        """Whether the model is even told this tool exists.

        Financial and booking tools are deliberately NOT model-facing: the model
        may say "they want to pay", and the deterministic layer decides whether
        that becomes a payment order.
        """
        return self.side_effect in (SideEffect.READ_ONLY, SideEffect.LOCAL_WRITE)


def _schema(**properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


_STRING = {"type": "string", "maxLength": 256}
_ORDINAL = {"type": "integer", "minimum": 1, "maximum": 3}


#: The complete registry. Adding a tool is a deliberate act with a spec.
TOOLS: tuple[ToolSpec, ...] = (
    # ---------------------------------------------------- model-facing reads
    ToolSpec(
        name="record_requirement",
        description="Store a requirement field the parent stated.",
        input_schema=_schema(
            field={
                "type": "string",
                "enum": [
                    "service",
                    "board",
                    "student_class",
                    "subject",
                    "mode",
                    "region",
                    "locality",
                    "timezone",
                ],
            },
            value=_STRING,
        ),
        allowed_states=frozenset({DemoState.COLLECTING_REQUIREMENTS}),
        allowed_actors=frozenset({Actor.USER, Actor.SYSTEM}),
        side_effect=SideEffect.LOCAL_WRITE,
        timeout_seconds=5.0,
        audit_fields=("field",),
    ),
    ToolSpec(
        name="select_tutor_option",
        description="The parent chose one of the presented options, by ordinal.",
        # Note: an ordinal, never a tutor reference. The reference is looked up
        # in the stored snapshot — see guardrails/tutor_selection.py.
        input_schema=_schema(ordinal=_ORDINAL),
        allowed_states=frozenset({DemoState.AWAITING_TUTOR_SELECTION}),
        allowed_actors=frozenset({Actor.USER}),
        side_effect=SideEffect.LOCAL_WRITE,
        timeout_seconds=5.0,
        audit_fields=("ordinal",),
    ),
    ToolSpec(
        name="request_alternative_tutors",
        description="The parent rejected all presented options.",
        input_schema={"type": "object", "additionalProperties": False, "properties": {}},
        allowed_states=frozenset({DemoState.AWAITING_TUTOR_SELECTION}),
        allowed_actors=frozenset({Actor.USER}),
        side_effect=SideEffect.LOCAL_WRITE,
        timeout_seconds=5.0,
    ),
    ToolSpec(
        name="propose_time",
        description="Interpret a time the parent stated. Re-validated deterministically.",
        input_schema=_schema(phrase={"type": "string", "maxLength": 120}),
        allowed_states=frozenset(
            {DemoState.TUTOR_SELECTED, DemoState.NEGOTIATING_SLOT, DemoState.SCHEDULED}
        ),
        allowed_actors=frozenset({Actor.USER}),
        side_effect=SideEffect.LOCAL_WRITE,
        timeout_seconds=5.0,
        audit_fields=("phrase",),
    ),
    ToolSpec(
        name="request_human",
        description="The parent asked for a person, or the model is out of its depth.",
        input_schema=_schema(reason={"type": "string", "maxLength": 200}),
        allowed_states=frozenset(),
        allowed_actors=frozenset({Actor.USER, Actor.SYSTEM}),
        side_effect=SideEffect.LOCAL_WRITE,
        timeout_seconds=5.0,
        audit_fields=("reason",),
    ),
    # ------------------------------------------- deterministic, not model-facing
    ToolSpec(
        name="hold_slot",
        description="Claim a tutor/time exclusively. Deterministic caller only.",
        input_schema=_schema(tutor_ref=_STRING, starts_at=_STRING),
        allowed_states=frozenset({DemoState.NEGOTIATING_SLOT, DemoState.TUTOR_SELECTED}),
        allowed_actors=frozenset({Actor.SYSTEM}),
        side_effect=SideEffect.EXTERNAL_BOOKING,
        timeout_seconds=10.0,
        audit_fields=("tutor_ref", "starts_at"),
    ),
    ToolSpec(
        name="create_calendar_event",
        description="Create the one logical calendar event and its conference.",
        input_schema=_schema(hold_id=_STRING),
        allowed_states=frozenset({DemoState.CALENDAR_CREATION_PENDING}),
        allowed_actors=frozenset({Actor.SYSTEM}),
        side_effect=SideEffect.EXTERNAL_BOOKING,
        timeout_seconds=25.0,
        audit_fields=("hold_id",),
        result_schema=_schema(event_id=_STRING, meet_url={"type": "string"}),
    ),
    ToolSpec(
        name="commit_discount",
        description="Persist a discount the deterministic engine already approved.",
        input_schema=_schema(demo_id=_STRING),
        allowed_states=frozenset({DemoState.POST_DEMO_ANALYSIS, DemoState.FOLLOWUP_PENDING}),
        allowed_actors=frozenset({Actor.SYSTEM, Actor.OPERATOR}),
        side_effect=SideEffect.FINANCIAL,
        timeout_seconds=10.0,
        audit_fields=("demo_id",),
    ),
    ToolSpec(
        name="create_payment_order",
        description="Create a Cashfree order from an approved offer.",
        input_schema=_schema(demo_id=_STRING),
        allowed_states=frozenset({DemoState.FOLLOWUP_PENDING, DemoState.POST_DEMO_ANALYSIS}),
        allowed_actors=frozenset({Actor.SYSTEM}),
        side_effect=SideEffect.FINANCIAL,
        timeout_seconds=20.0,
        audit_fields=("demo_id",),
    ),
    ToolSpec(
        name="activate_subscription",
        description="Idempotently activate the website subscription.",
        input_schema=_schema(order_ref=_STRING),
        allowed_states=frozenset({DemoState.PAYMENT_CONFIRMED, DemoState.SUBSCRIPTION_ACTIVATING}),
        allowed_actors=frozenset({Actor.SYSTEM}),
        side_effect=SideEffect.FINANCIAL,
        timeout_seconds=25.0,
        audit_fields=("order_ref",),
    ),
    ToolSpec(
        name="transfer_ownership",
        description="Hand the conversation to another owner.",
        input_schema=_schema(
            to={"type": "string", "enum": ["onboarding_agent", "human_operator", "released"]}
        ),
        allowed_states=frozenset(),
        allowed_actors=frozenset({Actor.SYSTEM, Actor.OPERATOR}),
        side_effect=SideEffect.EXTERNAL_BOOKING,
        timeout_seconds=10.0,
        audit_fields=("to",),
    ),
    ToolSpec(
        name="send_message",
        description="Queue a customer-facing message for the outbound boundary.",
        input_schema=_schema(kind=_STRING),
        allowed_states=frozenset(),
        allowed_actors=frozenset({Actor.SYSTEM}),
        side_effect=SideEffect.CUSTOMER_VISIBLE,
        timeout_seconds=15.0,
        audit_fields=("kind",),
    ),
)

_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


def get(name: str) -> ToolSpec:
    spec = _BY_NAME.get(name)
    if spec is None:
        raise ToolRefused(name, Refusal.UNKNOWN_TOOL)
    return spec


def model_facing_tools() -> tuple[ToolSpec, ...]:
    """What the model is told exists. Never the financial or booking tools."""
    return tuple(tool for tool in TOOLS if tool.model_facing)


def registry_invariants() -> list[str]:
    """Structural problems with the registry. Must be empty.

    Run by the tests and by `dcc-doctor`, so a bad registry fails a check
    rather than one conversation.
    """
    problems: list[str] = []

    forbidden = {t.name for t in TOOLS} & FORBIDDEN_TOOL_NAMES
    if forbidden:
        problems.append(f"forbidden tools present: {sorted(forbidden)}")

    for tool in TOOLS:
        if tool.input_schema.get("additionalProperties") is not False:
            problems.append(f"{tool.name}: input schema must set additionalProperties=false")
        if tool.side_effect is not SideEffect.READ_ONLY and not tool.idempotent:
            problems.append(f"{tool.name}: a side-effectful tool must be idempotent")
        if tool.side_effect in (SideEffect.FINANCIAL, SideEffect.EXTERNAL_BOOKING):
            if Actor.USER in tool.allowed_actors:
                problems.append(
                    f"{tool.name}: a user may not trigger a {tool.side_effect.value} tool"
                )
            if tool.model_facing:
                problems.append(
                    f"{tool.name}: a {tool.side_effect.value} tool must not be model-facing"
                )
        if tool.timeout_seconds <= 0:
            problems.append(f"{tool.name}: timeout must be positive")

    names = [t.name for t in TOOLS]
    if len(set(names)) != len(names):
        problems.append("duplicate tool names")
    return problems
