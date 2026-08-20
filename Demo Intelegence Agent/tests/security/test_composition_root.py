"""The composition root: what the running service is actually wired to.

Every bug this file pins was invisible to the rest of the suite, because the
suite builds its own stack with `build_local_stack` and never asks what
`build_dependencies` would have produced in production.

They are filed under `security` because each one loses or corrupts customer
data silently rather than raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from demo_command_center.config.settings import PersistenceMode, Settings

pytestmark = pytest.mark.security


class TestPersistenceRouting:
    """`postgres_dsn` must reach the PostgreSQL repositories.

    It did not. `_stores()` sent every non-MEMORY mode to the Data API builder,
    which implements three aggregates and returns **in-memory** objects for the
    other seven. A deployed service therefore held demos, reminders, the
    outbox, the message log, analysis, commerce and operations in RAM: a booked
    demo and its payment order vanished on the next container recycle, and
    nothing was logged.
    """

    def _stores_for(self, mode: PersistenceMode, monkeypatch: pytest.MonkeyPatch) -> dict:
        from demo_command_center import bootstrap

        built: dict = {}

        def fake_postgres(settings: object) -> dict:
            built["builder"] = "postgres"
            return dict.fromkeys(
                (
                    "pool",
                    "conversations",
                    "idempotency",
                    "demos",
                    "slots",
                    "reminders",
                    "outbox",
                    "messages",
                    "analysis",
                    "commerce",
                    "operations",
                ),
                object(),
            )

        def fake_data_api(settings: object) -> dict:
            built["builder"] = "data_api"
            return {}

        monkeypatch.setattr(
            "demo_command_center.storage.postgres.repositories.build_postgres_stores",
            fake_postgres,
        )
        monkeypatch.setattr(
            "demo_command_center.storage.data_api.repositories.build_data_api_stores",
            fake_data_api,
        )
        bootstrap.reset_singletons()
        settings = Settings(persistence_mode=mode, postgres_dsn="postgresql://u:p@h:5432/d")
        stores = bootstrap._stores(settings)
        bootstrap.reset_singletons()
        return {"which": built.get("builder"), "stores": stores}

    def test_postgres_dsn_builds_postgres_stores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._stores_for(PersistenceMode.POSTGRES_DSN, monkeypatch)["which"] == "postgres"

    def test_data_api_still_builds_data_api_stores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._stores_for(PersistenceMode.DATA_API, monkeypatch)["which"] == "data_api"

    def test_postgres_mode_covers_every_aggregate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A partial builder is how seven aggregates silently stayed in RAM."""
        stores = self._stores_for(PersistenceMode.POSTGRES_DSN, monkeypatch)["stores"]
        required = {
            "conversations",
            "idempotency",
            "demos",
            "slots",
            "reminders",
            "outbox",
            "messages",
            "analysis",
            "commerce",
            "operations",
        }
        assert required <= set(stores), f"missing: {sorted(required - set(stores))}"


class TestPolicyDirectoryIsolation:
    """Demo must load ITS policies whatever directory the process runs from.

    Both agents ship a `config/policies/`, and the sets are disjoint. Demo's
    resolver tried `Path.cwd()` first, so running from the repository root — as
    the combined single-agent deployment does — picked up the Tutor folder and
    died at cold start with `PolicyError: policy file unreadable`, naming a
    path that looks entirely plausible.
    """

    def test_resolves_to_a_directory_holding_demo_policies(self) -> None:
        from demo_command_center.bootstrap import _policy_dir
        from demo_command_center.config.settings import get_settings

        settings = get_settings()
        resolved = _policy_dir(settings)
        assert (resolved / f"{settings.reminder_policy}.yaml").is_file()
        assert (resolved / f"{settings.discount_policy}.yaml").is_file()

    def test_cwd_does_not_change_the_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from demo_command_center.bootstrap import _policy_dir
        from demo_command_center.config.settings import get_settings

        settings = get_settings()
        before = _policy_dir(settings)
        monkeypatch.chdir(tmp_path)
        assert _policy_dir(settings) == before

    def test_it_never_resolves_to_the_tutor_policy_directory(self) -> None:
        from demo_command_center.bootstrap import _policy_dir
        from demo_command_center.config.settings import get_settings

        resolved = _policy_dir(get_settings()).resolve()
        tutor = (Path(__file__).resolve().parents[3] / "config" / "policies").resolve()
        if tutor.is_dir():
            assert resolved != tutor


class TestOpenAiModelFamilies:
    """Swapping a model must stay a settings change, as the module promises.

    It was not: the newer families reject both of the older tuning parameters,
    so `DCC_MODEL_REASONING=gpt-5-mini` produced a 400 on every extraction.
    """

    def test_current_families_get_max_tokens_and_deterministic_temperature(self) -> None:
        from demo_command_center.integrations.openai.client import _tuning_for

        tuning = _tuning_for("gpt-4o-mini", 900)
        assert tuning["max_tokens"] == 900
        assert tuning["temperature"] == 0, "extraction must be reproducible for the audit trail"

    @pytest.mark.parametrize(
        "model", ["gpt-5-mini", "gpt-5", "gpt-5-mini-2025-08-07", "o1", "o3-mini", "o4-mini"]
    )
    def test_next_generation_families_get_the_renamed_parameter(self, model: str) -> None:
        from demo_command_center.integrations.openai.client import _tuning_for

        tuning = _tuning_for(model, 900)
        assert tuning == {"max_completion_tokens": 900}, (
            "these families reject max_tokens and any temperature but the default"
        )


class TestSharedCredentials:
    """One product, one account. A second copy of a credential drifts."""

    def test_demo_inherits_the_tutor_openai_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TMM_OPENAI_API_KEY", "sk-shared-test-value")
        monkeypatch.delenv("DCC_OPENAI_API_KEY", raising=False)
        settings = Settings(openai_api_key="")
        assert settings.openai_api_key.get_secret_value() == "sk-shared-test-value"

    def test_an_explicit_demo_key_is_never_overwritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TMM_OPENAI_API_KEY", "sk-shared-test-value")
        settings = Settings(openai_api_key="sk-demo-own-value")
        assert settings.openai_api_key.get_secret_value() == "sk-demo-own-value"

    def test_the_provider_is_not_promoted_without_a_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`openai` with no key fails the deployed-invariant check.

        `delenv` alone cannot express this: the fallback reads the shared
        `.env` file directly, which is the entire point of it, so the key is
        still found. Both sources have to be silenced to test the coupling.
        """
        monkeypatch.delenv("TMM_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("TMM_LLM_PROVIDER", "openai")
        from demo_command_center.config import settings as settings_module

        real = settings_module._read_env_value
        monkeypatch.setattr(
            settings_module,
            "_read_env_value",
            # Only the OpenAI lookups are silenced. Blanking every lookup would
            # also cut the shared DSN and the persistence validator would fail
            # first, hiding what this test is actually about.
            lambda name: "" if "OPENAI" in name else real(name),
        )
        settings = Settings(openai_api_key="", llm_provider="stub")
        assert settings.llm_provider == "stub"
        assert not settings.openai_api_key.get_secret_value()

    def test_the_provider_is_promoted_when_a_key_is_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TMM_OPENAI_API_KEY", "sk-shared-test-value")
        monkeypatch.setenv("TMM_LLM_PROVIDER", "openai")
        settings = Settings(openai_api_key="", llm_provider="stub")
        assert settings.llm_provider == "openai"
