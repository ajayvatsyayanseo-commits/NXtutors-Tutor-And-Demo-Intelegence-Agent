"""The migrations and the ORM must describe the same database.

Two ways they can silently disagree, both of which produce a "relation does not
exist" at runtime rather than at deploy time:

* a table created without `schema=SCHEMA` lands in the connection's default
  schema, while the ORM qualifies it as `tutor_match.<table>`. `env.py` sets no
  search_path, so nothing papers over it. This is exactly what happened to
  `sync_checkpoint`;
* a model with no migration, or a migration with no model.

Both are checkable statically, so neither needs a database.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "migrations" / "versions"
MODELS = ROOT / "src" / "tutor_match_meta" / "repositories" / "models.py"

#: Tables created by a migration that deliberately have no ORM model, because
#: they are only ever touched by raw SQL. Each needs a reason.
NO_MODEL_BY_DESIGN = {
    # Written by `rag/embeddings.py` with a bare INSERT … ON CONFLICT; the
    # ledger is a checksum lookup, not a domain object.
    "embedding_ledger",
    # Append-only audit trail. Deliberately has no model, so no code path can
    # accidentally update or delete a row.
    "approval_audit",
}


def _migration_tables() -> dict[str, tuple[str, bool]]:
    """`table -> (migration file, has an explicit schema= argument)`."""
    found: dict[str, tuple[str, bool]] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_table"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                continue
            name = str(node.args[0].value)
            qualified = any(kw.arg == "schema" for kw in node.keywords)
            found[name] = (path.name, qualified)
    return found


def _model_tables() -> set[str]:
    return set(re.findall(r'__tablename__ = "(\w+)"', MODELS.read_text(encoding="utf-8")))


class TestSchemaQualification:
    def test_every_created_table_names_its_schema(self) -> None:
        """The `sync_checkpoint` bug, stated as a rule.

        A table without `schema=SCHEMA` is created wherever the migration
        connection happens to point, which is not where the ORM will look for
        it.
        """
        unqualified = sorted(
            f"{migration}:{table}"
            for table, (migration, qualified) in _migration_tables().items()
            if not qualified
        )
        assert unqualified == [], (
            f"tables created without schema=SCHEMA: {unqualified}. "
            "migrations/env.py sets no search_path, so these land in the "
            "default schema while the ORM qualifies them with tutor_match."
        )

    def test_the_alembic_version_table_lives_in_our_schema(self) -> None:
        """A single `public.alembic_version` would have this service and
        demo_command_center overwriting each other's migration head."""
        env = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        assert env.count("version_table_schema=SCHEMA") == 2, (
            "both the offline and online paths must pin the version table"
        )


class TestMigrationsMatchModels:
    def test_every_model_has_a_migration(self) -> None:
        missing = sorted(_model_tables() - set(_migration_tables()))
        assert missing == [], f"models with no migration: {missing}"

    def test_every_migration_table_has_a_model_or_a_reason(self) -> None:
        orphans = sorted(set(_migration_tables()) - _model_tables() - NO_MODEL_BY_DESIGN)
        assert orphans == [], (
            f"tables created with no ORM model: {orphans}. Add a model, or add "
            "the table to NO_MODEL_BY_DESIGN with the reason it is raw-SQL only."
        )

    def test_the_expected_table_count_is_stable(self) -> None:
        """A tripwire, not a rule.

        Adding a table is fine; adding one *without noticing* is what this
        catches. Update the number in the same commit as the migration.
        """
        assert len(_migration_tables()) == 21


class TestMigrationChain:
    def test_the_revisions_form_one_unbroken_chain(self) -> None:
        revisions: dict[str, str | None] = {}
        for path in sorted(VERSIONS.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            revision = re.search(r'^revision = "(\w+)"', source, re.M)
            down = re.search(r'^down_revision = (?:"(\w+)"|None)', source, re.M)
            assert revision, f"{path.name} has no revision id"
            assert down, f"{path.name} has no down_revision"
            revisions[revision.group(1)] = down.group(1)

        roots = [rev for rev, down in revisions.items() if down is None]
        assert len(roots) == 1, f"expected exactly one root migration, got {roots}"

        # Every non-root parent must exist, and no two migrations may share one.
        parents = [down for down in revisions.values() if down is not None]
        assert len(parents) == len(set(parents)), f"branched history: {parents}"
        assert all(parent in revisions for parent in parents)

    @pytest.mark.parametrize("expected", ["0001", "0002", "0003"])
    def test_the_known_revisions_are_present(self, expected: str) -> None:
        assert any(path.stem.startswith(expected) for path in VERSIONS.glob("*.py"))

    def test_the_head_matches_what_the_build_reports(self) -> None:
        """`/version` claims a schema revision; it must be the real head."""
        from tutor_match_meta.version import EXPECTED_SCHEMA_REVISION

        revisions = {p.stem.split("_")[0] for p in VERSIONS.glob("*.py")}
        parents = set()
        for path in VERSIONS.glob("*.py"):
            down = re.search(r'^down_revision = "(\w+)"', path.read_text(encoding="utf-8"), re.M)
            if down:
                parents.add(down.group(1))
        head = (revisions - parents).pop()
        assert EXPECTED_SCHEMA_REVISION == head
