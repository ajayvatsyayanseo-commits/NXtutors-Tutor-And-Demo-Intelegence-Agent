"""The NAT-less network boundary, asserted rather than assumed.

There is no NAT Gateway in this architecture. That single fact splits every
Lambda into one of two disjoint zones:

* **vpc** — attached to the private subnets. Reaches RDS Proxy and the VPC
  endpoints. Has *no route at all* to the public internet.
* **internet** — outside the VPC. Reaches the public internet. Has no route to
  PostgreSQL.

The failure this file exists to prevent is silent. A VPC-attached function told
to call `api.openai.com` does not error — it opens a socket that never connects
and blocks until the client timeout, on **every invocation**. In the logs it
looks like latency. In the product it looks like the model never helping. The
match worker shipped in exactly that state: it built an OpenAI client, a memory
client and a geocoder, and every one of them was unreachable.

So the rules below are enforced three ways, and each is tested here:

1. configuration refuses an impossible combination (`_enforce_network_boundary`);
2. the composition root does not *build* what a zone cannot reach;
3. Terraform's `vpc_config` matches the zone each function declares.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from tutor_match_meta import bootstrap
from tutor_match_meta.config.settings import Settings

pytestmark = pytest.mark.security

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra" / "terraform"
SRC = ROOT / "src" / "tutor_match_meta"


def settings_for(zone: str, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": "local",
        "network_zone": zone,
        "postgres_dsn": "postgresql+asyncpg://u:p@db.internal:5432/tmm",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestConfigurationRefusesTheImpossible:
    """A process may only be configured to reach what it can actually reach."""

    def test_a_vpc_function_cannot_enable_the_memory_service(self) -> None:
        with pytest.raises(ValidationError, match="cannot reach the public internet"):
            settings_for(
                "vpc",
                chitragupta_enabled=True,
                chitragupta_base_url="https://memory.example.com",
            )

    def test_a_vpc_function_cannot_enable_the_paid_geocoder(self) -> None:
        with pytest.raises(ValidationError, match="cannot reach the public internet"):
            settings_for("vpc", geocoder="http", geocoder_base_url="https://geo.example.com")

    def test_a_vpc_function_cannot_send_whatsapp(self) -> None:
        with pytest.raises(ValidationError, match="cannot reach the public internet"):
            settings_for("vpc", whatsapp_enabled=True, outbound_ownership="tutor_match_sends")

    def test_the_internet_zone_may_do_all_of_those(self) -> None:
        assert settings_for(
            "internet", whatsapp_enabled=True, outbound_ownership="tutor_match_sends"
        )
        assert settings_for("internet", geocoder="http", geocoder_base_url="https://g.example.com")
        assert settings_for(
            "internet", chitragupta_enabled=True, chitragupta_base_url="https://m.example.com"
        )

    def test_the_error_names_the_offender(self) -> None:
        """An operator has to be able to act on it without reading the source."""
        with pytest.raises(ValidationError) as caught:
            settings_for("vpc", whatsapp_enabled=True, outbound_ownership="tutor_match_sends")
        assert "whatsapp_enabled=true" in str(caught.value)


class TestTheCompositionRootHonoursTheZone:
    def setup_method(self) -> None:
        bootstrap.reset_singletons()

    def teardown_method(self) -> None:
        bootstrap.reset_singletons()

    def test_the_internet_zone_builds_no_database_session_factory(self) -> None:
        """An engine here would connect to a host with no route, and every cold
        container would pay the full socket timeout on its first query."""
        assert bootstrap.database_sessions(settings_for("internet")) is None

    def test_the_vpc_zone_does_build_one(self) -> None:
        assert bootstrap.database_sessions(settings_for("vpc")) is not None

    async def test_the_vpc_zone_wires_no_llm_provider(self) -> None:
        service, _ = await bootstrap.build_turn_service(
            settings_for("vpc", llm_provider="stub", postgres_dsn="")
        )
        assert service._d.llm is None, (
            "the match worker has no route to api.openai.com; a provider here "
            "hangs every turn until the client timeout"
        )

    async def test_the_all_zone_still_wires_one(self) -> None:
        """Local development and the test suite run one process for everything."""
        service, _ = await bootstrap.build_turn_service(
            settings_for("all", llm_provider="stub", postgres_dsn="")
        )
        assert service._d.llm is not None


class TestNoModuleCrossesTheBoundary:
    """The internet-side service must not acquire a database handle by import."""

    def test_the_enrichment_service_imports_no_repository(self) -> None:
        tree = ast.parse((SRC / "orchestration" / "enrichment.py").read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        forbidden = sorted(
            m
            for m in imported
            if ".repositories.postgres" in m or m.endswith("repositories.models")
        )
        assert forbidden == [], f"internet-side module imports a database module: {forbidden}"

    def test_the_enrichment_dependencies_take_no_session_factory(self) -> None:
        from tutor_match_meta.orchestration.enrichment import EnrichmentDependencies

        fields = {f for f in EnrichmentDependencies.__dataclass_fields__}
        assert not {"sessions", "engine", "session_factory", "tutors"} & fields


class TestTerraformMatchesTheDeclaredZones:
    """`vpc_config` in Terraform and `TMM_NETWORK_ZONE` must agree.

    They are two independent statements of the same fact, and a deployment where
    they disagree is exactly the bug this whole file is about.
    """

    @staticmethod
    def lambda_blocks() -> dict[str, str]:
        text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(TERRAFORM.glob("*.tf")))
        blocks: dict[str, str] = {}
        for match in re.finditer(r'resource\s+"aws_lambda_function"\s+"(\w+)"\s*\{', text):
            name = match.group(1)
            start = match.end()
            depth = 1
            index = start
            while index < len(text) and depth:
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                index += 1
            blocks[name] = text[start:index]
        return blocks

    def test_every_lambda_declares_a_zone(self) -> None:
        for name, body in self.lambda_blocks().items():
            assert "TMM_NETWORK_ZONE" in body, f"{name} does not declare its network zone"

    def test_the_zone_matches_whether_the_function_is_vpc_attached(self) -> None:
        for name, body in self.lambda_blocks().items():
            attached = "vpc_config" in body
            declared_vpc = re.search(r'TMM_NETWORK_ZONE\s*=\s*"vpc"', body) is not None
            assert attached == declared_vpc, (
                f"{name}: vpc_config={attached} but TMM_NETWORK_ZONE=vpc is "
                f"{declared_vpc}. One of the two is lying about the network path."
            )

    def test_at_least_one_function_lives_on_each_side(self) -> None:
        blocks = self.lambda_blocks()
        assert blocks, "no Lambda functions found in Terraform"
        zones = {
            name: ("vpc" if "vpc_config" in body else "internet") for name, body in blocks.items()
        }
        assert "vpc" in zones.values()
        assert "internet" in zones.values()


class TestForbiddenInfrastructure:
    """The architecture bans these outright. Grepping is the whole point."""

    @staticmethod
    def terraform_text() -> str:
        return "\n".join(
            # Comments explain *why* these are absent, so they must not count
            # as occurrences.
            re.sub(r"#.*", "", p.read_text(encoding="utf-8"))
            for p in sorted(TERRAFORM.glob("*.tf"))
        )

    @pytest.mark.parametrize(
        "resource",
        [
            "aws_nat_gateway",
            "aws_ecs_cluster",
            "aws_ecs_service",
            "aws_ecs_task_definition",
            "aws_instance",
            "aws_launch_template",
            "aws_autoscaling_group",
            "aws_eks_cluster",
            "aws_elasticache_cluster",
            "aws_elasticache_replication_group",
            "aws_docdb_cluster",
            "aws_dynamodb_table",
        ],
    )
    def test_the_resource_is_absent(self, resource: str) -> None:
        assert resource not in self.terraform_text(), (
            f"{resource} is present. This architecture is Lambda + PostgreSQL only: "
            "no NAT, no containers, no second datastore."
        )

    def test_no_egress_only_or_nat_route_exists(self) -> None:
        text = self.terraform_text()
        for pattern in ("nat_gateway_id", "egress_only_gateway_id"):
            assert pattern not in text, f"{pattern} would give the VPC a paid egress path"

    def test_the_only_database_engine_is_postgres(self) -> None:
        text = self.terraform_text().lower()
        for engine in ("mysql", "aurora-mysql", "mariadb", "sqlserver", "oracle"):
            assert f'"{engine}"' not in text, f"{engine} is not permitted"
