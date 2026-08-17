"""The schema guard, and the config-drift guards around it.

The guard exists to convert "relation does not exist, mid-conversation" into a
deployment that refuses to become ready. These tests prove it actually detects
each failure shape, using a fake session rather than a database — the point is
the comparison logic, not PostgreSQL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tutor_match_meta.repositories import schema_check
from tutor_match_meta.repositories.schema_check import (
    REQUIRED_COLUMNS,
    REQUIRED_TABLES,
    SchemaVerifier,
)
from tutor_match_meta.version import EXPECTED_SCHEMA_REVISION

ROOT = Path(__file__).resolve().parents[2]


class FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class FakeSession:
    """Answers the three queries `SchemaVerifier` makes, in order."""

    def __init__(self, tables: set[str], columns: set[tuple[str, str]], revision: str | None):
        self._tables = tables
        self._columns = columns
        self._revision = revision

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: object, params: dict | None = None) -> FakeResult:
        sql = str(statement)
        if "information_schema.tables" in sql:
            return FakeResult([(t,) for t in sorted(self._tables)])
        if "information_schema.columns" in sql:
            return FakeResult(sorted(self._columns))
        raise AssertionError(f"unexpected query: {sql[:80]}")

    async def scalar(self, statement: object) -> str | None:
        return self._revision


def sessions_for(
    *,
    tables: set[str] | None = None,
    columns: set[tuple[str, str]] | None = None,
    revision: str | None = EXPECTED_SCHEMA_REVISION,
):
    healthy_tables = set(REQUIRED_TABLES) if tables is None else tables
    healthy_columns = (
        {(t, c) for t, required in REQUIRED_COLUMNS.items() for c in required}
        if columns is None
        else columns
    )

    def factory() -> FakeSession:
        return FakeSession(healthy_tables, healthy_columns, revision)

    return factory


class TestHealthySchema:
    async def test_a_complete_schema_reports_ok(self) -> None:
        report = await schema_check.verify(sessions_for(), schema="tutor_match")
        assert report.ok
        assert report.problems() == []
        assert report.revision == EXPECTED_SCHEMA_REVISION


class TestDetection:
    async def test_a_missing_table_is_named(self) -> None:
        report = await schema_check.verify(
            sessions_for(tables=set(REQUIRED_TABLES) - {"outbox_event"}), schema="tutor_match"
        )
        assert not report.ok
        assert report.missing_tables == ["outbox_event"]
        assert any("missing_tables:outbox_event" in p for p in report.problems())

    async def test_a_missing_column_is_named(self) -> None:
        """The `claimed_at` case: the table exists, the lease column does not,
        so the outbox lease silently does nothing and two relays double-send."""
        columns = {(t, c) for t, required in REQUIRED_COLUMNS.items() for c in required}
        columns.discard(("outbox_event", "claimed_at"))
        report = await schema_check.verify(sessions_for(columns=columns), schema="tutor_match")
        assert not report.ok
        assert report.missing_columns == ["outbox_event.claimed_at"]

    async def test_an_old_migration_head_is_reported(self) -> None:
        report = await schema_check.verify(sessions_for(revision="0002"), schema="tutor_match")
        assert not report.ok
        assert any("schema_revision_mismatch" in p for p in report.problems())

    async def test_an_unreachable_database_is_not_reported_as_healthy(self) -> None:
        def exploding() -> FakeSession:
            raise ConnectionRefusedError("database is down")

        report = await schema_check.verify(exploding, schema="tutor_match")
        assert not report.ok
        assert not report.reachable
        assert report.problems() == ["database_unreachable:ConnectionRefusedError"]

    async def test_an_empty_schema_reports_everything_missing(self) -> None:
        """What a forgotten `alembic upgrade head` looks like."""
        report = await schema_check.verify(
            sessions_for(tables=set(), columns=set(), revision=None), schema="tutor_match"
        )
        assert not report.ok
        assert len(report.missing_tables) == len(REQUIRED_TABLES)


class TestIdentifierSafety:
    @pytest.mark.parametrize(
        "hostile",
        ["public; DROP SCHEMA tutor_match CASCADE", 'x" OR "1"="1', "tutor-match", "1abc", ""],
    )
    def test_an_invalid_schema_identifier_is_refused(self, hostile: str) -> None:
        """The one place a name must be interpolated validates it first."""
        with pytest.raises(ValueError, match="invalid schema identifier"):
            SchemaVerifier(sessions_for(), schema=hostile)

    def test_a_normal_identifier_is_accepted(self) -> None:
        assert SchemaVerifier(sessions_for(), schema="tutor_match") is not None


class TestTheListMatchesReality:
    def test_every_required_table_is_created_by_a_migration(self) -> None:
        """The guard must not demand a table no migration builds — it would
        make every deployment permanently un-ready."""
        import ast

        created: set[str] = set()
        for path in (ROOT / "migrations" / "versions").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create_table"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    created.add(str(node.args[0].value))
        created.add("alembic_version")  # Alembic creates its own

        phantom = sorted(REQUIRED_TABLES - created)
        assert phantom == [], f"required but never created: {phantom}"

    def test_every_created_table_is_required(self) -> None:
        """And the reverse: a table nothing checks for can go missing unnoticed."""
        import ast

        created: set[str] = set()
        for path in (ROOT / "migrations" / "versions").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create_table"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    created.add(str(node.args[0].value))

        unchecked = sorted(created - REQUIRED_TABLES)
        assert unchecked == [], f"created but not verified at startup: {unchecked}"


class TestEnvironmentFileHygiene:
    """`.env.example` is the only documentation of what can be configured."""

    def test_the_example_is_in_sync_with_settings(self) -> None:
        import subprocess

        result = subprocess.run(  # noqa: S603
            [sys.executable, str(ROOT / "scripts" / "sync_env_example.py"), "--check"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_example_contains_no_value_for_any_secret(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        offenders = [
            line
            for line in text.splitlines()
            if "=" in line
            and line.split("=", 1)[1].strip()
            and any(
                hint in line.split("=", 1)[0].lower()
                for hint in ("key", "token", "secret", "pepper", "dsn", "password")
            )
        ]
        assert offenders == [], f"committed example carries a value: {offenders}"

    def test_no_redis_setting_survives_anywhere(self) -> None:
        """`TMM_REDIS_URL` was not a Settings field, so it was silently ignored
        while holding a live ElastiCache credential. There is no Redis in this
        architecture; see cache/postgres_store.py."""
        for name in (".env.example",):
            text = (ROOT / name).read_text(encoding="utf-8")
            assert "REDIS" not in text.upper(), f"{name} still references Redis"
