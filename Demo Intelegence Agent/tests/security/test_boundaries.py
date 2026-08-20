"""Structural security invariants, checked by walking the source.

These are the properties that a code review is bad at and a grep is good at:
"nothing outside this module sends", "no SQL is built by interpolation", "no
domain module opens a socket". Each is asserted against the AST rather than
against a convention, so violating one fails a test instead of passing review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

SRC = Path(__file__).resolve().parents[2] / "src" / "demo_command_center"

#: The only module permitted to hold a WhatsApp sender or call `.send(`.
SENDER_MODULES = {
    SRC / "orchestration" / "outbound.py",
    SRC / "integrations" / "meta_whatsapp" / "sender.py",
    SRC / "integrations" / "fakes.py",
    SRC / "bootstrap.py",
    SRC / "contracts" / "ports.py",
}

#: Layers that must never touch the network or AWS directly.
DOMAIN_LAYERS = ("domain", "capabilities", "state", "guardrails", "orchestration")

NETWORK_MODULES = {"httpx", "requests", "urllib.request", "socket", "aiohttp", "boto3", "botocore"}


def python_files(*relative: str) -> list[Path]:
    roots = [SRC / part for part in relative] if relative else [SRC]
    return [path for root in roots for path in root.rglob("*.py")]


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


# ------------------------------------------------------ the single sender


def imported_names(tree: ast.Module) -> set[str]:
    """Names actually bound by an import. Not text — a docstring may name a
    type it is explaining, and flagging that would make the check unusable."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.asname or alias.name for alias in node.names)
    return found


def test_only_the_outbound_boundary_imports_a_whatsapp_port() -> None:
    """One send path. Eight capability Lambdas cannot produce eight replies."""
    offenders = [
        str(path.relative_to(SRC))
        for path in python_files()
        if path not in SENDER_MODULES and "WhatsAppPort" in imported_names(parse(path))
    ]
    assert offenders == [], f"modules importing WhatsAppPort outside the boundary: {offenders}"


def test_no_capability_constructs_an_outbound_send() -> None:
    """Capabilities queue messages; they never deliver them."""
    offenders: list[str] = []
    for path in python_files("capabilities"):
        source = path.read_text(encoding="utf-8")
        if ".send(" in source or "MetaWhatsAppSender" in source:
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


# ---------------------------------------------------- the network boundary


def test_domain_layers_never_import_a_network_client() -> None:
    """No `httpx`, no `boto3`, no SDK below the adapter layer.

    This is what makes the whole test suite and `make demo` runnable with no
    credentials: the layers that hold the business rules physically cannot
    reach a network.
    """
    offenders: list[str] = []
    for path in python_files(*DOMAIN_LAYERS):
        leaked = imported_modules(parse(path)) & NETWORK_MODULES
        if leaked:
            offenders.append(f"{path.relative_to(SRC)}: {sorted(leaked)}")
    assert offenders == []


def test_no_domain_module_contains_a_hardcoded_url() -> None:
    offenders: list[str] = []
    for path in python_files(*DOMAIN_LAYERS):
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(("http://", "https://")):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []


# ------------------------------------------------------ sql parameterisation


def test_every_sql_string_interpolates_only_the_validated_schema() -> None:
    """No runtime value reaches SQL by interpolation.

    Walks every f-string in `storage/` that looks like SQL and asserts each
    interpolated expression is `self._schema` or `schema` — both of which are
    validated identifiers fixed at construction. Every other value is a named
    Data API parameter.
    """
    allowed = {"self._schema", "schema", "self._config.schema"}
    offenders: list[str] = []

    for path in python_files("storage"):
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.JoinedStr):
                continue
            literal = "".join(
                part.value for part in node.values if isinstance(part, ast.Constant)
            ).upper()
            if not any(
                word in literal for word in ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE")
            ):
                continue
            for part in node.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                expression = ast.unparse(part.value)
                if expression not in allowed:
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: {expression}")

    assert offenders == [], f"SQL interpolates a non-schema value: {offenders}"


def test_there_is_no_arbitrary_sql_execution_entry_point() -> None:
    """An LLM-reachable SQL tool cannot exist if no such function does."""
    banned = {"execute_raw", "raw_sql", "run_sql", "query_raw"}
    offenders: list[str] = []
    for path in python_files():
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in banned:
                offenders.append(f"{path.relative_to(SRC)}:{node.name}")
    assert offenders == []


# ---------------------------------------------------------- prompt boundary


def test_the_llm_port_exposes_no_free_text_completion() -> None:
    """Every model call declares a schema, so nothing unvalidated gets in."""
    from demo_command_center.contracts.ports import LlmPort

    methods = {name for name in dir(LlmPort) if not name.startswith("_")}
    assert methods == {"structured"}


def test_no_model_id_appears_as_a_literal_in_business_logic() -> None:
    """Model ids come from settings so swapping one is a config change."""
    offenders: list[str] = []
    for path in python_files(*DOMAIN_LAYERS):
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                if lowered.startswith(("gpt-", "claude-", "gemini-", "o1-", "o3-")):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []


# ------------------------------------------------------------- pii boundary


def test_no_module_stores_a_raw_phone_field() -> None:
    """The domain works in opaque refs; a phone number exists only at send time."""
    offenders: list[str] = []
    for path in python_files("domain", "contracts", "repositories"):
        source = path.read_text(encoding="utf-8")
        for field in ("phone_number:", "wa_phone:", "email_address:", "msisdn:"):
            if field in source:
                offenders.append(f"{path.relative_to(SRC)}: {field}")
    assert offenders == []


def test_metric_labels_reject_identifying_dimensions() -> None:
    from demo_command_center.security.pii import assert_label_safe

    assert_label_safe({"region": "north", "outcome": "sent"})
    with pytest.raises(ValueError, match="identifying metric labels"):
        assert_label_safe({"phone": "9876543210"})
    with pytest.raises(ValueError, match="identifying metric labels"):
        assert_label_safe({"conversation_id": "cv_1"})
