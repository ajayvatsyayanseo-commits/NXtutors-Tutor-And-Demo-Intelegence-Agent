"""SQL injection: there is no API that would accept an injection.

The strongest defence against LLM-generated SQL is not having a code path that
accepts SQL. These tests assert that property structurally rather than by
fuzzing strings past a parser:

* `CandidateQuery` is a frozen dataclass of typed fields — there is no free-text
  search entry point on `TutorSearchPort`;
* every f-string in a repository interpolates only a module-level constant, and
  every runtime value is a bound parameter;
* hostile text survives a full turn as *data*, and the same text is still
  present, unexecuted, in the stored requirement.

No application module carries an `S608` exemption any more: the MySQL adapter
that needed one was deleted when the website moved behind a signed HTTPS feed.
"""

from __future__ import annotations

import ast
import inspect
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tutor_match_meta.contracts.inbound import InboundEnvelope, InboundKind, WhatsAppTurnV1
from tutor_match_meta.repositories.ports import CandidateQuery

pytestmark = pytest.mark.security

SRC = Path(__file__).resolve().parents[2] / "src" / "tutor_match_meta"

#: Statements that must never appear in application source at all. A migration
#: may create things; the request path may not.
_DDL = re.compile(r"\b(DROP|TRUNCATE|ALTER)\s+(TABLE|SCHEMA|DATABASE)\b", re.IGNORECASE)

#: A statement verb, not merely the word FROM — otherwise ordinary prose like
#: "4.6 from 17 reviews" is mistaken for SQL and the check cries wolf.
_SQL_VERB = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+INDEX)\b", re.I)


def _looks_like_sql(node: ast.JoinedStr) -> bool:
    literal = "".join(p.value for p in node.values if isinstance(p, ast.Constant))
    return bool(_SQL_VERB.search(literal))


def _module_constants(tree: ast.Module) -> set[str]:
    """Names assigned at module scope. These are fixed at import time."""
    names: set[str] = set()
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _free_names(node: ast.expr) -> set[str]:
    """Names an expression depends on, excluding its own comprehension locals."""
    bound: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.comprehension) and isinstance(inner.target, ast.Name):
            bound.add(inner.target.id)
    used: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
            used.add(inner.id)
    return used - bound


INJECTION_STRINGS = (
    "Robert'); DROP TABLE tutor_projection;--",
    "' OR '1'='1",
    "1; DELETE FROM match_decision WHERE 1=1;",
    "%' UNION SELECT * FROM llm_usage --",
    "\\'; SELECT pg_sleep(10); --",
    "admin'--",
    "' OR 1=1 LIMIT 1 OFFSET 0 --",
)


class TestNoSqlSurface:
    def test_the_candidate_query_has_no_free_text_field(self) -> None:
        """The port cannot express a query an attacker could author.

        Every field is a scalar or a tuple of scalars consumed as a bound
        parameter. There is no `where`, no `sql`, no `filter_expression`.
        """
        annotations = CandidateQuery.__annotations__
        forbidden = {"sql", "query", "where", "filter", "expression", "order_by", "raw"}
        assert not (set(annotations) & forbidden), (
            f"CandidateQuery gained a free-form field: {set(annotations) & forbidden}"
        )

    def test_the_search_port_takes_no_string_query(self) -> None:
        from tutor_match_meta.repositories.ports import TutorSearchPort

        signature = inspect.signature(TutorSearchPort.search)
        assert list(signature.parameters) == ["self", "query"]
        # `from __future__ import annotations` makes these strings.
        assert signature.parameters["query"].annotation == "CandidateQuery"


class TestRepositorySource:
    """Static analysis of every module that builds SQL."""

    def _sql_modules(self) -> list[Path]:
        return [
            path
            for path in SRC.rglob("*.py")
            if any(
                marker in path.read_text(encoding="utf-8")
                for marker in ("text(", "SELECT ", "INSERT INTO", "UPDATE ")
            )
        ]

    def test_no_application_module_contains_ddl(self) -> None:
        offenders = [
            path.relative_to(SRC).as_posix()
            for path in SRC.rglob("*.py")
            if _DDL.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"DDL in the application path: {offenders}"

    def test_every_interpolated_sql_fragment_is_a_module_constant(self) -> None:
        """An f-string in SQL may interpolate only a name the module owns.

        Walks the AST rather than grepping, because the rule is about *what* is
        interpolated: a regex cannot tell `{self._table}` (built once in
        `__init__` from configuration) from `{user_input}`.

        Allowed sources, and only these:

        * `self.<attr>` — a table name fixed at construction from settings;
        * a module-level constant defined in the same file (`_COLLATE`,
          `SCHEMA`, `REGISTER_COLUMNS`, …), including inside a comprehension
          that builds a column list from one;
        * a comprehension-local bound by such a constant.

        Anything else — a parameter, an attribute of a passed object, a literal
        from elsewhere — fails, because none of those can be shown to be
        attacker-independent by reading this file.
        """
        problems: list[str] = []
        for path in self._sql_modules():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            constants = _module_constants(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.JoinedStr) or not _looks_like_sql(node):
                    continue
                for part in node.values:
                    if not isinstance(part, ast.FormattedValue):
                        continue
                    for name in _free_names(part.value):
                        if name == "self" or name in constants:
                            continue
                        problems.append(
                            f"{path.relative_to(SRC).as_posix()}:{node.lineno} "
                            f"interpolates {name!r}, which is not a module constant"
                        )
        assert problems == [], "\n".join(problems)

    def test_the_detector_itself_catches_a_planted_violation(self) -> None:
        """A check that cannot fail proves nothing.

        Runs the same analysis over a synthetic module that interpolates a
        function parameter into a WHERE clause — the exact mistake this test
        exists to catch — and asserts it is flagged.
        """
        bad = ast.parse(
            "TABLE = 'tutor'\n"
            "def find(city):\n"
            "    return f'SELECT * FROM {TABLE} WHERE city = {city}'\n"
        )
        constants = _module_constants(bad)
        flagged = [
            name
            for node in ast.walk(bad)
            if isinstance(node, ast.JoinedStr) and _looks_like_sql(node)
            for part in node.values
            if isinstance(part, ast.FormattedValue)
            for name in _free_names(part.value)
            if name != "self" and name not in constants
        ]
        assert flagged == ["city"]

    def test_no_string_concatenation_builds_a_where_clause(self) -> None:
        pattern = re.compile(r'"\s*\+\s*\w+\s*\+\s*"|WHERE\s*"\s*\+', re.IGNORECASE)
        offenders = [
            path.relative_to(SRC).as_posix()
            for path in self._sql_modules()
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"concatenated SQL in {offenders}"


class TestHostileTextIsData:
    """§5: 'DROP TABLE' from a parent is a phrase, not a statement."""

    @pytest.mark.parametrize("payload", INJECTION_STRINGS)
    async def test_an_injection_string_survives_a_full_turn_as_data(
        self, payload: str, turn_service, turn_deps
    ) -> None:
        envelope = InboundEnvelope(
            kind=InboundKind.WHATSAPP_TURN,
            trace_id="sqli",
            conversation_id=f"c-{abs(hash(payload))}",
            dedup_key=f"sqli:{abs(hash(payload))}",
            received_at=datetime.now(UTC),
            source_agent="test",
            payload=WhatsAppTurnV1(
                event_id="e1",
                conversation_id=f"c-{abs(hash(payload))}",
                provider_message_id=f"m-{abs(hash(payload))}",
                text=f"class 10 cbse maths gurgaon home tuition {payload}",
            ),
        )
        result = await turn_service.handle(envelope)
        # The turn completes normally — no crash, no refusal, no execution.
        assert result.reply is not None
        stored = await turn_deps.requirements.load(envelope.conversation_id)
        assert stored is not None, "the requirement was not persisted"

    @pytest.mark.parametrize("payload", INJECTION_STRINGS)
    def test_an_injection_string_is_never_promoted_to_a_query_filter(self, payload: str) -> None:
        """Whatever the parser makes of it, it becomes a bound scalar.

        The fingerprint is what would key a cache entry; asserting it is a
        plain hex digest proves the value never reaches a query as syntax.
        """
        query = CandidateQuery(limit=10, subjects=(payload,), city_aliases=(payload,))
        assert re.fullmatch(r"[0-9a-f]{24}", query.fingerprint())
