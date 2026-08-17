"""Typed runtime configuration.

Nothing environment-specific is hardcoded anywhere else in the codebase. Secrets
arrive either through the environment (local) or through Secrets Manager
(deployed) — see `secrets.py` — and are never logged.

Business weights and thresholds deliberately do NOT live here. They live in
versioned policy files under `config/policies/` so a scoring change is a
reviewable data change with a version stamped on every decision.
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


# The capability agents from the NXTutors agent register that this meta agent
# subsumes. Recorded on every memory event so lineage stays traceable; the acting
# agent_id is still the single meta agent (docs/assumptions.md A20).
COMPOSED_AGENT_IDS: tuple[str, ...] = ("013", "014", "015", "016", "017", "019", "021", "022")

_PLACEHOLDER_PREFIXES = ("replace-with", "changeme", "your-", "xxx")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TMM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ------------------------------------------------------------- runtime
    environment: Environment = Environment.LOCAL
    service_name: str = "tutor-match-meta"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    default_timezone: str = "Asia/Kolkata"

    #: Which side of the network boundary this process is running on.
    #:
    #: There is no NAT Gateway, so the two sides are strictly disjoint:
    #:
    #: * ``vpc``      — attached to the private subnets. Reaches RDS Proxy and
    #:                  the VPC endpoints. **No route to the public internet**,
    #:                  so OpenAI, the memory service and any paid geocoder are
    #:                  unreachable and are not wired in at all.
    #: * ``internet`` — outside the VPC. Reaches the public internet. **No route
    #:                  to PostgreSQL**, so no session factory is built.
    #: * ``all``      — one process does everything. Local development, the CLI
    #:                  and the test suite. Never a deployed Lambda.
    #:
    #: Terraform sets this per function. It is declared rather than inferred
    #: because inferring it from `vpc_config` at runtime is impossible, and the
    #: failure mode it prevents — a VPC function hanging on every OpenAI call
    #: until its timeout — is invisible in logs except as latency.
    network_zone: Literal["vpc", "internet", "all"] = "all"

    # ------------------------------------------------------------ postgres
    postgres_dsn: str = "postgresql+asyncpg://tmm:tmm@localhost:5433/tmm"
    #: PostgreSQL schema for every TutorMatch table.
    #:
    #: We share the `demo_command_center` database on the shared RDS instance,
    #: so a dedicated schema is what keeps the two services from colliding —
    #: most importantly on `alembic_version`, which is a single row per schema
    #: and would otherwise have two services fighting over one migration head.
    postgres_schema: str = "tutor_match"
    #: RDS enforces TLS. asyncpg takes `ssl`, not libpq's `sslmode`.
    postgres_require_tls: bool = True
    postgres_pool_size: Annotated[int, Field(ge=1, le=20)] = 5
    postgres_max_overflow: Annotated[int, Field(ge=0, le=20)] = 0
    postgres_statement_timeout_ms: Annotated[int, Field(ge=100, le=60_000)] = 5_000

    # -------------------------------------------------------- website read
    website_api_base_url: str = ""
    website_api_signing_key: SecretStr = SecretStr("")
    website_api_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 8.0
    #: Tutor feed paging. Small pages keep one Lambda invocation well inside
    #: its timeout and keep an S3 claim-check object small.
    tutor_feed_page_size: Annotated[int, Field(ge=1, le=500)] = 100
    tutor_feed_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 15.0

    # ------------------------------------------------------- website write
    website_write_enabled: bool = False

    # ---------------------------------------------------------- chitragupta
    chitragupta_enabled: bool = False
    chitragupta_base_url: str = ""
    chitragupta_api_key: SecretStr = SecretStr("")
    chitragupta_agent_id: str = "tutor-match-meta"
    chitragupta_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 3.0

    # -------------------------------------------------------------- whatsapp
    whatsapp_enabled: bool = False
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_api_version: str = "v21.0"

    # ------------------------------------------------------------------ llm
    llm_provider: Literal["stub", "openai"] = "stub"
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = ""
    #: Tier aliases, kept because existing deployments set them. New code routes
    #: by *purpose* through the four `model_*` settings below.
    llm_tier1_model: str = "gpt-4o-mini"
    llm_tier2_model: str = "gpt-4o"
    #: Purpose-routed model ids (§16). Every model id in the system is one of
    #: these four values; none appears as a literal in application code, so a
    #: model swap is a config change with an evaluation run behind it.
    model_extraction: str = ""
    model_escalation: str = ""
    model_explanation: str = ""
    model_embedding: str = "text-embedding-3-small"
    llm_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 12.0
    llm_max_retries: Annotated[int, Field(ge=0, le=5)] = 2
    llm_max_output_tokens: Annotated[int, Field(ge=64, le=8192)] = 800
    llm_store_responses: bool = False

    # ------------------------------------------------------- token budgeting
    #: Four independent ceilings (§14). Tokens alone does not bound cost: the
    #: expensive failure is a redelivery loop making many cheap calls.
    llm_conversation_token_budget: Annotated[int, Field(ge=1_000)] = 40_000
    llm_max_calls_per_turn: Annotated[int, Field(ge=1, le=10)] = 2
    llm_max_calls_per_conversation: Annotated[int, Field(ge=1, le=100)] = 12
    llm_max_escalations_per_conversation: Annotated[int, Field(ge=0, le=10)] = 1
    #: Per-turn ceiling on retrieved RAG chunks (§19). Small on purpose: top-k
    #: is both a cost lever and the injection surface.
    rag_top_k: Annotated[int, Field(ge=1, le=20)] = 5
    rag_token_budget: Annotated[int, Field(ge=100, le=8_000)] = 1_200

    # ------------------------------------------------------- ingress security
    ingress_signing_key: SecretStr = SecretStr("")
    ingress_timestamp_tolerance_seconds: Annotated[int, Field(ge=30, le=900)] = 300
    ingress_max_body_bytes: Annotated[int, Field(ge=1024, le=1_048_576)] = 65_536
    rate_limit_per_conversation_per_minute: Annotated[int, Field(ge=1, le=120)] = 12
    rate_limit_per_caller_per_minute: Annotated[int, Field(ge=1)] = 600
    rate_limit_per_identity_per_minute: Annotated[int, Field(ge=1, le=240)] = 20
    #: Checked BEFORE the provider call, so refused traffic costs nothing.
    rate_limit_llm_per_conversation_per_minute: Annotated[int, Field(ge=1, le=60)] = 4
    rate_limit_website_write_per_minute: Annotated[int, Field(ge=1)] = 30
    rate_limit_global_per_minute: Annotated[int, Field(ge=1)] = 600
    hash_pepper: SecretStr = SecretStr("")

    # ------------------------------------------------------------------ aws
    aws_region: str = "ap-south-1"
    #: Ingress publishes here. The enrich worker (internet side) consumes it,
    #: attaches an `EnrichmentV1`, and forwards to `match_queue_url`.
    enrich_queue_url: str = ""
    match_queue_url: str = ""
    outbound_queue_url: str = ""
    analytics_bucket: str = ""
    #: Where the internet-side fetcher stages feed pages for the VPC-side
    #: ingest job to apply. A work queue, not an archive: an object still
    #: present is a page not yet written to the projection.
    feed_staging_prefix: str = "feed-staging/"
    secrets_prefix: str = "/tutor-match-meta/"

    # ---------------------------------------------------------------- cache
    #: `memory` = in-process only (single container). `postgres` = in-process L1
    #: over a shared PostgreSQL L2. No Redis: see cache/postgres_store.py.
    cache_backend: Literal["memory", "postgres"] = "postgres"
    cache_default_ttl_seconds: Annotated[int, Field(ge=1, le=86_400)] = 300

    # ------------------------------------------------------------ geocoding
    geocoder: Literal["offline", "http", "disabled"] = "offline"
    geocoder_base_url: str = ""
    geocoder_api_key: SecretStr = SecretStr("")
    geocoder_timeout_seconds: Annotated[float, Field(gt=0, le=15)] = 3.0
    #: A pincode centroid does not move. Caching for a day turns a per-request
    #: paid lookup into roughly one call per pincode per day (§21).
    geocode_cache_ttl_seconds: Annotated[int, Field(ge=60, le=2_592_000)] = 86_400
    #: Hard ceiling on paid geocoder calls in one turn. Distance for the rest of
    #: the pool comes from stored tutor coordinates, never from the provider.
    geocode_max_calls_per_turn: Annotated[int, Field(ge=0, le=5)] = 1

    # ------------------------------------------------------------ analytics
    analytics_enabled: bool = True
    #: Never flip this on in production. It is a debugging affordance for a
    #: local corpus, and `_enforce_deployed_invariants` refuses it when set.
    analytics_include_raw_text: bool = False

    # --------------------------------------------------------------- policy
    policy_dir: Path = Path("config/policies")
    default_policy: str = "regular_school_support.v1"
    projection_fresh_hours: Annotated[int, Field(ge=1, le=168)] = 6
    projection_aging_hours: Annotated[int, Field(ge=2, le=336)] = 24

    # ---------------------------------------------------------------- links
    website_public_base_url: str = "https://www.nxtutors.com"

    # ------------------------------------------------------ multi-agent harmony
    #: Shared secret with Lead Intake, sent as `X-NXTUTORS-INTERNAL-SECRET`.
    #: Mirrors what Lead Intake already uses for the onboarding agent.
    internal_secret: SecretStr = SecretStr("")
    #: Signs continuation tokens. Separate from `internal_secret` so rotating
    #: the transport secret does not invalidate every paused MatchSession.
    continuation_signing_key: SecretStr = SecretStr("")
    continuation_ttl_seconds: Annotated[int, Field(ge=300, le=604_800)] = 21_600
    #: Who delivers the WhatsApp message. `caller_sends` (default) means Lead
    #: Intake sends the `reply_text` we return; `tutor_match_sends` means we own
    #: delivery. Both at once is a double send, rejected below.
    outbound_ownership: Literal["caller_sends", "tutor_match_sends"] = "caller_sends"
    #: Business policy: must a parent have a website account before we match?
    account_required_for_match: bool = False

    # ----------------------------------------------------------- rollout flags
    flag_enabled: bool = False
    flag_shadow_mode: bool = True
    flag_llm_enabled: bool = False
    flag_writeback_enabled: bool = False
    flag_rag_enabled: bool = False
    flag_memory_enabled: bool = False
    flag_auto_demo_enabled: bool = False
    flag_percentage_rollout: Annotated[int, Field(ge=0, le=100)] = 0
    #: Comma-separated conversation refs that bypass bucketing (staff testing).
    flag_always_on_refs: str = ""
    #: How long a container may serve a cached kill-switch state. The
    #: blast-radius knob during an incident.
    kill_switch_ttl_seconds: Annotated[int, Field(ge=1, le=300)] = 10

    # ------------------------------------------------------------ validation
    @field_validator("website_public_base_url", "website_api_base_url", "chitragupta_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _enforce_deployed_invariants(self) -> Settings:
        """Fail fast rather than run a deployed environment with dev defaults."""
        if self.environment is Environment.LOCAL:
            return self

        missing: list[str] = []
        if _is_placeholder(self.ingress_signing_key):
            missing.append("ingress_signing_key")
        if _is_placeholder(self.hash_pepper):
            missing.append("hash_pepper")
        if self.llm_provider == "openai" and _is_placeholder(self.openai_api_key):
            missing.append("openai_api_key")
        if self.whatsapp_enabled and _is_placeholder(self.whatsapp_access_token):
            missing.append("whatsapp_access_token")
        if self.chitragupta_enabled and _is_placeholder(self.chitragupta_api_key):
            missing.append("chitragupta_api_key")
        if missing:
            raise ValueError(
                f"environment={self.environment} requires real values for: {sorted(missing)}"
            )

        if self.projection_aging_hours <= self.projection_fresh_hours:
            raise ValueError("projection_aging_hours must exceed projection_fresh_hours")

        if self.analytics_include_raw_text:
            # Raw WhatsApp text in the analytics export is a privacy incident
            # waiting for a dashboard. Unrepresentable outside local.
            raise ValueError("analytics_include_raw_text must be false outside local")

        if self.geocoder == "http" and not self.geocoder_base_url:
            raise ValueError("geocoder=http requires geocoder_base_url")

        if not self.enrich_queue_url:
            # Without it, ingress would publish straight to the match queue and
            # the VPC-side worker would try to call OpenAI over a route that
            # does not exist — hanging for the full LLM timeout on every turn
            # and degrading to deterministic-only extraction, silently.
            raise ValueError(
                f"environment={self.environment} requires enrich_queue_url; "
                "the match worker has no route to the internet "
                "(see contracts/enrichment.py)"
            )

        return self

    @model_validator(mode="after")
    def _enforce_network_boundary(self) -> Settings:
        """A process may only be configured to reach what it can actually reach.

        There is no NAT Gateway. A VPC-attached Lambda told to call a public API
        does not fail fast — it hangs until the client timeout, on every
        invocation, and the only symptom is latency. Refusing the configuration
        turns an invisible production problem into a boot error.
        """
        if self.network_zone == "vpc":
            offenders = []
            if self.geocoder == "http":
                offenders.append("geocoder=http")
            if self.chitragupta_enabled:
                offenders.append("chitragupta_enabled=true")
            if self.whatsapp_enabled:
                offenders.append("whatsapp_enabled=true")
            if offenders:
                raise ValueError(
                    f"network_zone=vpc cannot reach the public internet, but {offenders} "
                    "require it. Move that work to an internet-zone function "
                    "(see contracts/enrichment.py)."
                )

        # The mirror-image rule — an internet-zone function must not open a
        # database connection — is enforced in bootstrap rather than here,
        # because `postgres_dsn` carries a usable local default and its mere
        # presence in the environment is not the problem. Building a session
        # factory is. See `bootstrap._sessions`.
        return self

    @model_validator(mode="after")
    def _enforce_model_routing(self) -> Settings:
        """Every purpose must resolve to a model, and only to a known one.

        An empty `model_*` falls back to the tier alias rather than failing —
        but an empty *alias* would route to the literal empty string and produce
        a 404 from the provider on the first real message, which is exactly the
        kind of thing that should not survive to production.
        """
        if self.llm_provider != "openai":
            return self
        resolved = {
            "extraction": self.model_extraction or self.llm_tier1_model,
            "escalation": self.model_escalation or self.llm_tier2_model,
            "explanation": self.model_explanation or self.llm_tier1_model,
            "embedding": self.model_embedding,
        }
        blank = sorted(name for name, value in resolved.items() if not value.strip())
        if blank:
            raise ValueError(f"llm_provider=openai requires a model for: {blank}")
        return self

    @model_validator(mode="after")
    def _enforce_single_outbound_owner(self) -> Settings:
        """Exactly one agent may deliver a WhatsApp message.

        Lead Intake owns the Meta webhook and the sender. If we also enable our
        own sender while returning `reply_text` to it, the parent gets the same
        shortlist twice. Making that configuration unrepresentable is cheaper
        than detecting it in production.
        """
        if self.outbound_ownership == "caller_sends" and self.whatsapp_enabled:
            raise ValueError(
                "outbound_ownership=caller_sends means Lead Intake sends the reply; "
                "whatsapp_enabled=true would send it a second time. Set one or the other."
            )
        if self.outbound_ownership == "tutor_match_sends" and not self.whatsapp_enabled:
            raise ValueError(
                "outbound_ownership=tutor_match_sends requires whatsapp_enabled=true, "
                "otherwise no agent delivers the reply at all"
            )
        return self

    # ------------------------------------------------------------- helpers
    @property
    def is_deployed(self) -> bool:
        return self.environment is not Environment.LOCAL

    @property
    def composed_agent_ids(self) -> tuple[str, ...]:
        return COMPOSED_AGENT_IDS


def _is_placeholder(secret: SecretStr) -> bool:
    value = secret.get_secret_value().strip()
    if not value:
        return True
    lowered = value.lower()
    return any(lowered.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so a warm Lambda parses env exactly once."""
    return Settings()
