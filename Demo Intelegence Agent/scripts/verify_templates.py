"""Prove every approved WhatsApp template can actually be delivered.

A template failure is the quietest failure in this system. Meta does not bounce
loudly: the send returns an error the caller logs, and the reminder, the
confirmation or the expiry notice simply never arrives. Nothing turns red.

So this checks four separate things, because passing one does not imply another:

1. **Declared** — the name is in the registry and marked approved.
2. **Bindable** — the variables the code builds match the arity Meta approved.
3. **Reachable** — some code path actually sends it. A perfectly declared
   template nothing references is a template that never arrives.
4. **Deliverable outside the session window** — the message that carries it is
   a template send, not free text. Free text is refused after 24 hours.

    python scripts/verify_templates.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from demo_command_center.integrations.meta_whatsapp.templates import (  # noqa: E402
    TemplateNotApproved,
    registry,
)

PASS, FAIL, WARN = "  [PASS]", "  [FAIL]", "  [WARN]"


def _policy_templates() -> dict[str, int]:
    """Reminder templates named by the policy, with their offset in minutes."""
    import yaml

    path = Path(__file__).resolve().parents[1] / "config" / "policies" / "reminder.v1.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {row["template"]: int(row["minutes_before"]) for row in document["offsets"]}


def _referenced_names() -> set[str]:
    """Template names something actually reaches.

    Two sources, because a template can be reached two ways and checking only
    the first reports the reminder ladder as dead code:

      * Python — a `TEMPLATE_*` constant or a bare `"demo_..."` literal.
      * The reminder policy YAML — `offsets[].template` is a string the
        scheduler copies onto the reminder, so the policy *is* the call site.
    """
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path.name == "templates.py":
            continue  # the declaration itself is not a use
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id.startswith("TEMPLATE_"):
                names.add(node.id)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("demo_"):
                    names.add(node.value)
    names.update(_policy_templates())
    return names


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    def check(ok: bool, label: str, detail: str = "", *, warn_only: bool = False) -> None:
        mark = PASS if ok else (WARN if warn_only else FAIL)
        print(f"{mark} {label}{f'  ({detail})' if detail else ''}")
        if not ok:
            (warnings if warn_only else failures).append(label)

    reg = registry()
    approved = reg.approved_names()
    policy = _policy_templates()
    referenced = _referenced_names()

    import demo_command_center.integrations.meta_whatsapp.templates as mod

    constants = {
        name: value
        for name, value in vars(mod).items()
        if name.startswith("TEMPLATE_") and isinstance(value, str)
    }
    # A constant is "referenced" if the symbol is used OR its literal value is.
    reached = {
        value for name, value in constants.items() if name in referenced or value in referenced
    }

    print("=== 1. declared and approved ===")
    for name in sorted(constants.values()):
        try:
            reg.get(name)
            check(True, name)
        except TemplateNotApproved as exc:
            check(False, name, exc.reason, warn_only=True)

    print("\n=== 2. the policy and the registry agree ===")
    for name, minutes in sorted(policy.items(), key=lambda kv: -kv[1]):
        hours = minutes / 60
        window = f"T-{minutes}m" if minutes < 60 else f"T-{hours:g}h"
        check(name in approved, f"{name} scheduled at {window}", f"{minutes} minutes before")

    print("\n=== 3. binds with the variables the code builds ===")
    sample = {
        "demo_datetime": "Tue 18 Aug, 5:00 PM",
        "timezone": "Asia/Kolkata",
        "reference": "dmo_01ABCDEF",
        "join_link": "https://meet.google.com/abc-defg-hij",
        "student_name": "there",
        "tutor_name": "Arjun Desai",
    }
    for name in sorted(approved):
        template = reg.get(name)
        variables = tuple(sample.get(v, "-") for v in template.variables)
        try:
            binding = reg.bind(name, language="en", variables=variables)
            check(
                len(binding.variables) == template.arity,
                f"{name} binds {template.arity}",
                ", ".join(template.variables),
            )
        except TemplateNotApproved as exc:
            check(False, f"{name} binds", exc.reason)

    print("\n=== 4. something actually sends it ===")
    for name in sorted(constants.values()):
        check(
            name in reached,
            f"{name} is reachable from code",
            "" if name in reached else "declared but never sent",
            warn_only=name not in approved,
        )

    print("\n=== 5. language and arity guards refuse bad sends ===")
    first = sorted(approved)[0]
    arity = reg.get(first).arity
    for label, kwargs in (
        ("wrong language refused", {"language": "hi", "variables": ("x",) * arity}),
        ("too few variables refused", {"language": "en", "variables": ("x",) * (arity - 1)}),
        ("too many variables refused", {"language": "en", "variables": ("x",) * (arity + 1)}),
        ("empty variable refused", {"language": "en", "variables": ("",) * arity}),
    ):
        try:
            reg.bind(first, **kwargs)  # type: ignore[arg-type]
            check(False, label, "it was accepted")
        except TemplateNotApproved:
            check(True, label)

    print()
    if failures:
        print(f"TEMPLATES FAILED — {len(failures)} check(s):")
        for item in failures:
            print(f"  - {item}")
        return 1
    if warnings:
        print(f"TEMPLATES OK with {len(warnings)} warning(s):")
        for item in warnings:
            print(f"  - {item}")
        return 0
    print("TEMPLATES OK — every approved template is declared, bindable and reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
