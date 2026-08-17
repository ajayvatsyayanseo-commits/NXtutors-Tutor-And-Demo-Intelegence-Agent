"""Typed runtime configuration, validated at cold start.

Two rules this module exists to enforce:

* **Nothing environment-specific is hardcoded anywhere else.** Every URL,
  timeout, threshold and model id resolves through here.
* **Business numbers do not live here.** Reminder offsets, discount bands,
  forecast weights and underperformance thresholds live in versioned YAML under
  `config/policies/`, so changing one is a reviewable data change whose version
  is stamped onto every decision it produced. A discount ceiling as a Python
  constant is a discount ceiling nobody can audit after the fact.

Secrets arrive from Secrets Manager in deployed environments and from the
environment locally; `SecretStr` keeps them out of reprs and logs either way.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


class PersistenceMode(StrEnum):
    """How this process reaches Aurora.

    `data_api` is the default and the reason the orchestrator Lambda can live
    outside the VPC and still call Meta, Google, Cashfree and OpenAI without a
    NAT Gateway. `lambda_proxy` is the documented fallback for a region or
    engine version where the Data API is unavailable: a dedicated persistence
    Lambda inside the DB VPC, invoked over the AWS API, which itself makes no
    internet calls. `memory` is local development and the test suite.
    """

    DATA_API = "data_api"
    LAMBDA_PROXY = "lambda_proxy"
    MEMORY = "memory"


_PLACEHOLDER_PREFIXES = ("replace-with", "changeme", "your-", "xxx", "todo")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DCC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ------------------------------------------------------------- runtime
    environment: Environment = Environment.LOCAL
    service_name: str = "demo-command-center-agent"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    metrics_enabled: bool = True
    #: IANA identifier. Every stored instant is UTC; this is what a bare
    #: "tomorrow at 6" is resolved against and what a confirmation renders in.
    default_timezone: str = "Asia/Kolkata"

    # --------------------------------------------------------------- aurora
    persistence_mode: PersistenceMode = PersistenceMode.MEMORY
    aurora_cluster_arn: str = ""
    aurora_secret_arn: str = ""
    aurora_database: str = "demo_command_center"
    aurora_schema: str = "dcc"
    #: Invoked only when `persistence_mode=lambda_proxy`.
    persistence_lambda_arn: str = ""
    db_statement_timeout_ms: Annotated[int, Field(ge=100, le=30_000)] = 5_000
    db_max_retries: Annotated[int, Field(ge=0, le=5)] = 2

    # ----------------------------------------------------------------- aws
    aws_region: str = "ap-south-1"
    work_queue_url: str = ""
    outbound_queue_url: str = ""
    scheduler_group_name: str = "demo-command-center"
    scheduler_role_arn: str = ""
    event_bus_name: str = "default"
    kms_key_arn: str = ""
    secrets_prefix: str = "/demo-command-center/"

    # ---------------------------------------------------------------- meta
    meta_enabled: bool = False
    meta_app_id: str = ""
    meta_app_secret: SecretStr = SecretStr("")
    meta_verify_token: SecretStr = SecretStr("")
    meta_access_token: SecretStr = SecretStr("")
    meta_phone_number_id: str = ""
    meta_waba_id: str = ""
    meta_graph_version: str = "v21.0"
    meta_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 10.0
    #: Meta's free-form window. Configurable rather than a constant because Meta
    #: has changed it before and will again; when it closes we must use an
    #: approved template or stay silent.
    meta_session_window_hours: Annotated[int, Field(ge=1, le=168)] = 24

    # ------------------------------------------------------------ nxtutors
    gateway_base_url: str = ""
    gateway_audience: str = "demo-command-center"
    gateway_source_id: str = "demo_command_center_agent"
    gateway_signing_key_id: str = "v1"
    gateway_signing_secret: SecretStr = SecretStr("")
    gateway_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 8.0
    gateway_max_retries: Annotated[int, Field(ge=0, le=5)] = 2

    # -------------------------------------------------------------- google
    google_enabled: bool = False
    google_auth_mode: Literal["service_account", "oauth_refresh"] = "service_account"
    #: The mailbox that owns every demo event. Domain-wide delegation subject in
    #: service-account mode; the authenticated account in OAuth mode.
    google_organizer_email: str = ""
    google_calendar_id: str = "primary"
    google_credentials_secret: str = ""
    google_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 12.0
    #: Google may return a conference asynchronously. How long we re-read the
    #: event before treating Meet creation as failed.
    google_conference_poll_attempts: Annotated[int, Field(ge=1, le=10)] = 4
    google_conference_poll_delay_seconds: Annotated[float, Field(ge=0.1, le=10)] = 1.5

    # ------------------------------------------------------------ cashfree
    cashfree_enabled: bool = False
    cashfree_env: Literal["sandbox", "production"] = "sandbox"
    cashfree_app_id: SecretStr = SecretStr("")
    cashfree_secret_key: SecretStr = SecretStr("")
    #: Cashfree pins behaviour to a dated API version. Configuration, not a
    #: constant, because upgrading is a deliberate act with a test run behind it.
    cashfree_api_version: str = "2023-08-01"
    cashfree_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 12.0
    cashfree_webhook_tolerance_seconds: Annotated[int, Field(ge=30, le=3600)] = 300
    cashfree_return_url: str = ""

    # -------------------------------------------------------------- openai
    llm_provider: Literal["stub", "openai"] = "stub"
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = ""
    #: Purpose-routed. No model id appears as a literal in application code, so
    #: swapping one is a config change with an evaluation behind it.
    model_classifier: str = "gpt-4o-mini"
    model_extraction: str = "gpt-4o-mini"
    model_reasoning: str = "gpt-4o"
    llm_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 20.0
    llm_max_retries: Annotated[int, Field(ge=0, le=5)] = 2
    llm_max_output_tokens: Annotated[int, Field(ge=64, le=8192)] = 900

    # ------------------------------------------------------- token budgeting
    #: Four independent ceilings. Tokens alone does not bound cost — the
    #: expensive failure mode is a redelivery loop making many cheap calls.
    llm_conversation_token_budget: Annotated[int, Field(ge=1_000)] = 60_000
    llm_max_calls_per_turn: Annotated[int, Field(ge=1, le=10)] = 2
    llm_max_calls_per_conversation: Annotated[int, Field(ge=1, le=200)] = 30
    llm_max_reasoning_calls_per_conversation: Annotated[int, Field(ge=0, le=20)] = 3
    context_token_budget: Annotated[int, Field(ge=500, le=32_000)] = 4_000

    # ------------------------------------------------------ ingress security
    internal_signing_secret: SecretStr = SecretStr("")
    internal_timestamp_tolerance_seconds: Annotated[int, Field(ge=30, le=900)] = 300
    max_body_bytes: Annotated[int, Field(ge=1024, le=1_048_576)] = 131_072
    hash_pepper: SecretStr = SecretStr("")

    # ------------------------------------------------------- rate limiting
    rate_limit_per_conversation_per_minute: Annotated[int, Field(ge=1, le=240)] = 20
    rate_limit_per_identity_per_minute: Annotated[int, Field(ge=1, le=240)] = 30
    rate_limit_global_per_minute: Annotated[int, Field(ge=1)] = 2_000
    rate_limit_llm_per_conversation_per_minute: Annotated[int, Field(ge=1, le=60)] = 6
    rate_limit_whatsapp_per_identity_per_hour: Annotated[int, Field(ge=1, le=200)] = 20
    rate_limit_payment_orders_per_conversation_per_hour: Annotated[int, Field(ge=1, le=50)] = 5
    rate_limit_ops_api_per_minute: Annotated[int, Field(ge=1)] = 120

    # ----------------------------------------------------------- resilience
    circuit_failure_threshold: Annotated[int, Field(ge=2, le=50)] = 5
    circuit_reset_seconds: Annotated[int, Field(ge=5, le=3600)] = 60
    circuit_half_open_probes: Annotated[int, Field(ge=1, le=5)] = 1

    # ---------------------------------------------------------------- cache
    cache_default_ttl_seconds: Annotated[int, Field(ge=1, le=86_400)] = 300
    #: Authorization decisions are cached, but briefly. A revoked sub-admin who
    #: keeps regional access for five minutes is a real, bounded exposure; the
    #: bound is what makes it acceptable.
    cache_authorization_ttl_seconds: Annotated[int, Field(ge=1, le=300)] = 60
    cache_max_entries: Annotated[int, Field(ge=16, le=10_000)] = 512

    # --------------------------------------------------------------- policy
    policy_dir: Path = Path("config/policies")
    reminder_policy: str = "reminder.v1"
    discount_policy: str = "discount.v1"
    forecast_model: str = "forecast.v1"
    monitoring_policy: str = "monitoring.v1"

    # ------------------------------------------------------------- ownership
    #: Only the owning agent may send business messages. `caller_sends` means an
    #: upstream agent (Lead Intake) delivers the reply we return; `self_sends`
    #: means we own delivery through the Meta Cloud API. Both at once is a
    #: double send, and it is rejected below rather than discovered in support.
    outbound_ownership: Literal["caller_sends", "self_sends"] = "self_sends"
    lead_intake_webhook_url: str = ""
    onboarding_webhook_url: str = ""
    handoff_ttl_seconds: Annotated[int, Field(ge=60, le=604_800)] = 3_600

    # ------------------------------------------------------------ behaviour
    supported_languages: str = "en,hi,hinglish"
    default_language: Literal["en", "hi", "hinglish"] = "en"
    #: Kill switches. Each disables one capability's *side effects* without
    #: taking the conversation down: the state machine keeps working.
    flag_scheduling_enabled: bool = True
    flag_reminders_enabled: bool = True
    flag_payments_enabled: bool = True
    flag_discounts_enabled: bool = True
    flag_auto_followup_enabled: bool = False

    # ------------------------------------------------------------ validation
    @field_validator(
        "gateway_base_url", "openai_base_url", "cashfree_return_url", mode="after"
    )
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("default_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _require_real_secrets_when_deployed(self) -> Settings:
        """Fail at cold start rather than run production on dev defaults."""
        if self.environment is Environment.LOCAL:
            return self

        missing: list[str] = []
        if _is_placeholder(self.internal_signing_secret):
            missing.append("internal_signing_secret")
        if _is_placeholder(self.hash_pepper):
            missing.append("hash_pepper")
        if self.meta_enabled:
            for name in ("meta_app_secret", "meta_verify_token", "meta_access_token"):
                if _is_placeholder(getattr(self, name)):
                    missing.append(name)
            if not self.meta_phone_number_id:
                missing.append("meta_phone_number_id")
        if self.cashfree_enabled:
            for name in ("cashfree_app_id", "cashfree_secret_key"):
                if _is_placeholder(getattr(self, name)):
                    missing.append(name)
        if self.llm_provider == "openai" and _is_placeholder(self.openai_api_key):
            missing.append("openai_api_key")
        if self.google_enabled and not self.google_credentials_secret:
            missing.append("google_credentials_secret")
        if not _is_placeholder(self.gateway_signing_secret) and not self.gateway_base_url:
            missing.append("gateway_base_url")
        if missing:
            raise ValueError(
                f"environment={self.environment.value} requires real values for: {sorted(set(missing))}"
            )
        return self

    @model_validator(mode="after")
    def _require_persistence_wiring(self) -> Settings:
        if self.environment is Environment.LOCAL:
            return self
        if self.persistence_mode is PersistenceMode.MEMORY:
            raise ValueError(
                "persistence_mode=memory loses every conversation on container recycle; "
                "deployed environments must use data_api or lambda_proxy"
            )
        if self.persistence_mode is PersistenceMode.DATA_API and not (
            self.aurora_cluster_arn and self.aurora_secret_arn
        ):
            raise ValueError(
                "persistence_mode=data_api requires aurora_cluster_arn and aurora_secret_arn"
            )
        if self.persistence_mode is PersistenceMode.LAMBDA_PROXY and not self.persistence_lambda_arn:
            raise ValueError("persistence_mode=lambda_proxy requires persistence_lambda_arn")
        if not self.work_queue_url:
            # Without a queue the webhook would have to do the work inline, and
            # Meta retries anything slower than a few seconds — turning one slow
            # turn into a redelivery storm that re-runs the same LLM calls.
            raise ValueError("deployed environments require work_queue_url")
        return self

    @model_validator(mode="after")
    def _single_outbound_owner(self) -> Settings:
        """Exactly one agent delivers a given WhatsApp message."""
        if self.outbound_ownership == "caller_sends" and self.meta_enabled:
            raise ValueError(
                "outbound_ownership=caller_sends means the upstream agent sends the reply; "
                "meta_enabled=true would send it a second time"
            )
        if self.outbound_ownership == "self_sends" and self.is_deployed and not self.meta_enabled:
            raise ValueError(
                "outbound_ownership=self_sends requires meta_enabled=true, "
                "otherwise no agent delivers the reply at all"
            )
        return self

    @model_validator(mode="after")
    def _capabilities_have_their_providers(self) -> Settings:
        """A capability that is on must be able to do its job.

        `flag_payments_enabled` with Cashfree unconfigured does not fail at
        boot — it fails at the moment a parent says yes, which is the single
        worst moment in the funnel to discover a configuration gap.
        """
        if not self.is_deployed:
            return self
        broken: list[str] = []
        if self.flag_payments_enabled and not self.cashfree_enabled:
            broken.append("flag_payments_enabled requires cashfree_enabled")
        if self.flag_scheduling_enabled and not self.google_enabled:
            broken.append("flag_scheduling_enabled requires google_enabled")
        if self.flag_reminders_enabled and not self.scheduler_role_arn:
            broken.append("flag_reminders_enabled requires scheduler_role_arn")
        if broken:
            raise ValueError(f"capability flags without their providers: {broken}")
        return self

    # ------------------------------------------------------------- helpers
    @property
    def is_deployed(self) -> bool:
        return self.environment is not Environment.LOCAL

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.supported_languages.split(",") if part.strip())

    @property
    def cashfree_base_url(self) -> str:
        return (
            "https://api.cashfree.com"
            if self.cashfree_env == "production"
            else "https://sandbox.cashfree.com"
        )


def _is_placeholder(secret: SecretStr) -> bool:
    value = secret.get_secret_value().strip()
    if not value:
        return True
    lowered = value.lower()
    return any(lowered.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so a warm Lambda parses the env once."""
    return Settings()
