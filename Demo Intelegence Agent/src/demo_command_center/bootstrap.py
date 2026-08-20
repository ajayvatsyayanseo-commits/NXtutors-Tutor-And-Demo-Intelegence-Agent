"""Composition root. The only module that knows which class satisfies which port.

Two rules, both learned the expensive way in the Tutor Intelligence service:

**Degrade, never crash on startup.** A missing OpenAI key means the stub
provider, not a boot failure. A Lambda that will not initialise cannot even
report why it will not initialise.

**Build once per container.** Every expensive object is cached at module scope.
Rebuilding an HTTP client per request resets its circuit breaker on every
message, which is precisely the behaviour a breaker exists to prevent.

`build_local_stack()` is the same object graph with fakes. It is what `make demo`
and the E2E suite use, so those exercise the real orchestrator, the real state
machine and the real outbound boundary — only the edges are doubled.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from demo_command_center.capabilities.conversion.service import ConversionCapability
from demo_command_center.capabilities.discounts.service import DiscountCapability
from demo_command_center.capabilities.forecasting.service import ForecastCapability
from demo_command_center.capabilities.monitoring.service import MonitoringCapability
from demo_command_center.capabilities.objection_extraction.service import (
    ObjectionExtractionCapability,
)
from demo_command_center.capabilities.paid_transition.service import PaidTransitionCapability
from demo_command_center.capabilities.reminders.service import ReminderCapability
from demo_command_center.capabilities.scheduling.service import SchedulingCapability
from demo_command_center.config import policies as policy_loader
from demo_command_center.config.policies import (
    DiscountPolicy,
    ForecastModel,
    MonitoringPolicy,
    ReminderPolicy,
)
from demo_command_center.config.settings import PersistenceMode, Settings, get_settings
from demo_command_center.guardrails.output import OutputGuard, customer_safe_url_policy
from demo_command_center.integrations.meta_whatsapp.templates import registry
from demo_command_center.observability import logging as log_config
from demo_command_center.observability import metrics
from demo_command_center.orchestration.context import Dependencies
from demo_command_center.orchestration.orchestrator import DemoCommandCenterOrchestrator
from demo_command_center.orchestration.outbound import OutboundBoundary, OutboundPolicy
from demo_command_center.security.pii import Pseudonymiser
from demo_command_center.security.rate_limit import InProcessLimiter, LayeredLimiter
from demo_command_center.shared.clock import Clock, SystemClock
from demo_command_center.state.machine import StateMachine
from demo_command_center.storage.memory.commerce import (
    InMemoryAnalysisRepository,
    InMemoryCommerceRepository,
    InMemoryOperationsRepository,
)
from demo_command_center.storage.memory.conversations import (
    InMemoryConversationRepository,
    InMemoryIdempotencyRepository,
    InMemoryOutboxRepository,
)
from demo_command_center.storage.memory.demos import (
    InMemoryDemoRepository,
    InMemoryMessageLog,
    InMemoryReminderRepository,
    InMemorySlotRepository,
)

logger = log_config.get_logger("bootstrap")

_singletons: dict[str, Any] = {}

T = TypeVar("T")


def reset_singletons() -> None:
    """Tests only. Never called in production."""
    _singletons.clear()


def _cached(key: str, factory: Callable[[], T]) -> T:
    """Container-lifetime memoisation, typed on the factory's return."""
    if key not in _singletons:
        _singletons[key] = factory()
    value: T = _singletons[key]
    return value


# ------------------------------------------------------------------ policies


def _policy_dir(settings: Settings) -> Path:
    """Resolve `policy_dir` against the package root when it is relative.

    A Lambda's working directory is not the project root, so a relative
    `config/policies` resolves to nothing and every policy load fails at cold
    start. Walking up from this file finds them wherever the package is unzipped.
    """
    configured = Path(settings.policy_dir)
    if configured.is_absolute() and configured.exists():
        return configured

    # `Path.cwd()` is deliberately NOT tried first, and is only accepted when
    # the directory actually holds one of Demo's own policies.
    #
    # Both agents keep a `config/policies/`, and the two sets are disjoint —
    # Demo has discount/forecast/monitoring/reminder, Tutor has the scoring
    # policies. Whichever agent resolves against the working directory picks up
    # the other's folder, and the failure is a `PolicyError` at cold start with
    # a path that looks entirely plausible. It has now happened in both
    # directions; see `integrations/tutor_intelligence/local_adapter.py`
    # for the same fix on the Tutor side.
    def _has_demo_policies(base: Path) -> bool:
        return (base / f"{settings.reminder_policy}.yaml").is_file()

    for base in (*Path(__file__).resolve().parents, Path.cwd()):
        candidate = base / configured
        if candidate.is_dir() and _has_demo_policies(candidate):
            return candidate

    # Nothing matched by content. Fall back to mere existence so a partial
    # checkout still reports "policy file unreadable: <path>" rather than a
    # bare relative path nobody can act on.
    for base in (*Path(__file__).resolve().parents, Path.cwd()):
        candidate = base / configured
        if candidate.is_dir():
            return candidate
    return configured


def reminder_policy(settings: Settings) -> ReminderPolicy:
    return _cached(
        "policy.reminder",
        lambda: policy_loader.load(ReminderPolicy, _policy_dir(settings), settings.reminder_policy),
    )


def discount_policy(settings: Settings) -> DiscountPolicy:
    return _cached(
        "policy.discount",
        lambda: policy_loader.load(DiscountPolicy, _policy_dir(settings), settings.discount_policy),
    )


def forecast_model(settings: Settings) -> ForecastModel:
    return _cached(
        "policy.forecast",
        lambda: policy_loader.load(ForecastModel, _policy_dir(settings), settings.forecast_model),
    )


def monitoring_policy(settings: Settings) -> MonitoringPolicy:
    return _cached(
        "policy.monitoring",
        lambda: policy_loader.load(
            MonitoringPolicy, _policy_dir(settings), settings.monitoring_policy
        ),
    )


# ----------------------------------------------------------------- primitives


def build_pseudonymiser(settings: Settings) -> Pseudonymiser:
    def make() -> Pseudonymiser:
        pepper = settings.hash_pepper.get_secret_value()
        if not pepper:
            if settings.is_deployed:  # pragma: no cover - settings validation catches this
                raise RuntimeError("hash_pepper is required outside local")
            # Fixed locally so hashes stay stable across a dev restart.
            pepper = "local-development-pepper"
        return Pseudonymiser(pepper)

    return _cached("pseudonymiser", make)


def build_llm(settings: Settings) -> Any:
    def make() -> Any:
        if settings.llm_provider != "openai" or not settings.openai_api_key.get_secret_value():
            from demo_command_center.integrations.openai.stub import StubLlm

            logger.info("llm provider is the deterministic stub")
            return StubLlm()
        from demo_command_center.integrations.openai.client import OpenAiClient

        return OpenAiClient(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.openai_base_url,
            models={
                "intent_classification": settings.model_classifier,
                "requirement_extraction": settings.model_extraction,
                "objection_extraction": settings.model_reasoning,
            },
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            max_output_tokens=settings.llm_max_output_tokens,
        )

    return _cached("llm", make)


def _stores(settings: Settings) -> dict[str, Any]:
    """Repositories. In-memory unless a persistence mode says otherwise."""

    def make() -> dict[str, Any]:
        if settings.persistence_mode is PersistenceMode.MEMORY:
            if settings.is_deployed:  # pragma: no cover - settings validation catches this
                raise RuntimeError("persistence_mode=memory is not permitted outside local")
            logger.warning("using in-memory stores; state is lost on container recycle")
            return _memory_stores()

        if settings.persistence_mode is PersistenceMode.POSTGRES_DSN:
            # This branch did not exist, and its absence was invisible.
            #
            # Every non-MEMORY mode fell through to the Data API builder, which
            # returns Data API repositories for the three aggregates it
            # implements and **in-memory ones for the other seven**. So with
            # `persistence_mode=postgres_dsn` the running service got
            # conversations, idempotency and slots pointed at an Aurora Data
            # API this cluster does not expose, and demos, reminders, outbox,
            # messages, analysis, commerce and operations silently held in RAM
            # — a booked demo, its reminders and its payment order all lost on
            # the next container recycle, with nothing logged.
            from demo_command_center.storage.postgres.repositories import (
                build_postgres_stores,
            )

            logger.info(
                "using PostgreSQL stores",
                extra={"dcc_schema": settings.aurora_schema},
            )
            return build_postgres_stores(settings)

        from demo_command_center.storage.data_api.repositories import build_data_api_stores

        return build_data_api_stores(settings)

    return _cached("stores", make)


def _memory_stores() -> dict[str, Any]:
    return {
        "conversations": InMemoryConversationRepository(),
        "idempotency": InMemoryIdempotencyRepository(),
        "demos": InMemoryDemoRepository(),
        "slots": InMemorySlotRepository(),
        "reminders": InMemoryReminderRepository(),
        "outbox": InMemoryOutboxRepository(),
        "messages": InMemoryMessageLog(),
        "analysis": InMemoryAnalysisRepository(),
        "commerce": InMemoryCommerceRepository(),
        "operations": InMemoryOperationsRepository(),
    }


# -------------------------------------------------------------- assembly


def build_dependencies(
    settings: Settings | None = None, *, clock: Clock | None = None
) -> Dependencies:
    """The production object graph."""
    resolved = settings or get_settings()
    log_config.configure(resolved.log_level)
    metrics.configure(enabled=resolved.metrics_enabled)

    the_clock = clock or SystemClock()
    stores = _stores(resolved)

    from demo_command_center.integrations.cashfree.client import CashfreeClient
    from demo_command_center.integrations.google_calendar.client import GoogleCalendarClient
    from demo_command_center.integrations.google_calendar.credentials import (
        build_token_provider,
    )
    from demo_command_center.integrations.meta_whatsapp.sender import MetaWhatsAppSender
    from demo_command_center.integrations.nxtutors_gateway.client import NxtutorsGatewayClient
    from demo_command_center.integrations.nxtutors_gateway.contacts import GatewayContactResolver
    from demo_command_center.integrations.onboarding.bus import HttpAgentBus
    from demo_command_center.integrations.tutor_intelligence.fake import FakeTutorIntelligence
    from demo_command_center.integrations.tutor_intelligence.local_adapter import (
        LocalTutorIntelligenceAdapter,
        available,
    )
    from demo_command_center.storage.scheduler import EventBridgeScheduler

    gateway = NxtutorsGatewayClient(
        base_url=resolved.gateway_base_url,
        signing_secret=resolved.gateway_signing_secret.get_secret_value(),
        signing_key_id=resolved.gateway_signing_key_id,
        source_id=resolved.gateway_source_id,
        timeout_seconds=resolved.gateway_timeout_seconds,
        max_retries=resolved.gateway_max_retries,
    )
    tutors = (
        LocalTutorIntelligenceAdapter()
        if available()
        else _warn_and_fake("tutor_match_meta not installed", FakeTutorIntelligence())
    )

    return _assemble(
        settings=resolved,
        clock=the_clock,
        stores=stores,
        tutors=tutors,
        gateway=gateway,
        contacts=GatewayContactResolver(gateway),
        sender=MetaWhatsAppSender(
            access_token=resolved.meta_access_token.get_secret_value(),
            phone_number_id=resolved.meta_phone_number_id,
            graph_version=resolved.meta_graph_version,
            timeout_seconds=resolved.meta_timeout_seconds,
            enabled=resolved.meta_enabled,
        ),
        calendar=GoogleCalendarClient(
            calendar_id=resolved.google_calendar_id,
            organizer_email=resolved.google_organizer_email,
            credentials_secret=resolved.google_credentials_secret,
            timeout_seconds=resolved.google_timeout_seconds,
            poll_attempts=resolved.google_conference_poll_attempts,
            poll_delay_seconds=resolved.google_conference_poll_delay_seconds,
            enabled=resolved.google_enabled,
            # Without this the client raised "no google credential provider
            # configured" on every call: it takes a provider and nothing built
            # one, so the calendar was unreachable even fully configured and no
            # demo could ever receive a Meet link.
            token_provider=build_token_provider(resolved),
        ),
        payments=CashfreeClient(
            app_id=resolved.cashfree_app_id.get_secret_value(),
            secret_key=resolved.cashfree_secret_key.get_secret_value(),
            base_url=resolved.cashfree_base_url,
            api_version=resolved.cashfree_api_version,
            timeout_seconds=resolved.cashfree_timeout_seconds,
            enabled=resolved.cashfree_enabled,
        ),
        llm=build_llm(resolved),
        agents=HttpAgentBus(
            signing_secret=resolved.internal_signing_secret.get_secret_value(),
            timeout_seconds=resolved.gateway_timeout_seconds,
        ),
        scheduler=EventBridgeScheduler(
            group_name=resolved.scheduler_group_name,
            role_arn=resolved.scheduler_role_arn,
            region=resolved.aws_region,
            enabled=bool(resolved.scheduler_role_arn),
        ),
    )


def build_local_stack(
    settings: Settings | None = None, *, clock: Clock | None = None, **overrides: Any
) -> tuple[DemoCommandCenterOrchestrator, Dependencies, dict[str, Any]]:
    """The same graph with fakes. Used by `make demo`, E2E and the doctor.

    Returns the fakes as well so a test can assert on what was sent, scheduled
    or dispatched without reaching into the dependency object.
    """
    from demo_command_center.integrations.fakes import (
        FakeAgentBus,
        FakeCalendar,
        FakeCashfree,
        FakeContacts,
        FakeGateway,
        FakeScheduler,
        FakeWhatsApp,
    )
    from demo_command_center.integrations.openai.stub import StubLlm
    from demo_command_center.integrations.tutor_intelligence.fake import FakeTutorIntelligence

    resolved = settings or get_settings()
    the_clock = clock or SystemClock()

    doubles: dict[str, Any] = {
        "tutors": FakeTutorIntelligence(now=the_clock.now()),
        "gateway": FakeGateway(clock=the_clock),
        "calendar": FakeCalendar(clock=the_clock),
        "payments": FakeCashfree(clock=the_clock),
        "whatsapp": FakeWhatsApp(),
        "contacts": FakeContacts(),
        # The heuristic stub, not an empty double: it produces schema-valid
        # output, so a local run exercises the objection validator, the quote
        # verifier and the discount trigger mapping for real. A fake returning
        # `{}` would leave all three untested by `make demo`.
        "llm": StubLlm(),
        "agents": FakeAgentBus(),
        "scheduler": FakeScheduler(),
    }
    doubles.update(overrides)

    stores = _memory_stores()
    deps = _assemble(
        settings=resolved,
        clock=the_clock,
        stores=stores,
        tutors=doubles["tutors"],
        gateway=doubles["gateway"],
        contacts=doubles["contacts"],
        sender=doubles["whatsapp"],
        calendar=doubles["calendar"],
        payments=doubles["payments"],
        llm=doubles["llm"],
        agents=doubles["agents"],
        scheduler=doubles["scheduler"],
    )
    doubles["stores"] = stores
    return DemoCommandCenterOrchestrator(deps), deps, doubles


def _assemble(
    *,
    settings: Settings,
    clock: Clock,
    stores: dict[str, Any],
    tutors: Any,
    gateway: Any,
    contacts: Any,
    sender: Any,
    calendar: Any,
    payments: Any,
    llm: Any,
    agents: Any,
    scheduler: Any,
) -> Dependencies:
    """Wire the graph. The one place a concrete class meets a port."""
    limiter = LayeredLimiter(InProcessLimiter(clock))
    outbound = OutboundBoundary(
        sender=sender,
        contacts=contacts,
        conversations=stores["conversations"],
        message_log=stores["messages"],
        guard=OutputGuard(customer_safe_url_policy(website_host=settings.website_host)),
        limiter=limiter,
        clock=clock,
        policy=OutboundPolicy(
            session_window_hours=settings.meta_session_window_hours,
            sends_per_identity_per_hour=settings.rate_limit_whatsapp_per_identity_per_hour,
        ),
        template_names=registry().approved_names(),
    )

    return Dependencies(
        clock=clock,
        machine=StateMachine(),
        pseudonymiser=build_pseudonymiser(settings),
        conversations=stores["conversations"],
        idempotency=stores["idempotency"],
        demos=stores["demos"],
        slots=stores["slots"],
        reminders=stores["reminders"],
        outbox=stores["outbox"],
        analysis=stores["analysis"],
        commerce=stores["commerce"],
        operations=stores["operations"],
        outbound=outbound,
        scheduling=SchedulingCapability(
            slots=stores["slots"],
            demos=stores["demos"],
            calendar=calendar,
            gateway=gateway,
            clock=clock,
        ),
        reminder_policy=ReminderCapability(reminder_policy(settings), clock),
        forecasting=ForecastCapability(forecast_model(settings)),
        objections=ObjectionExtractionCapability(llm, model_ref=settings.model_reasoning),
        conversion=ConversionCapability(),
        discounts=DiscountCapability(discount_policy(settings)),
        paid=PaidTransitionCapability(
            payments=payments,
            gateway=gateway,
            commerce=stores["commerce"],
            clock=clock,
            return_url=settings.cashfree_return_url,
        ),
        monitoring=MonitoringCapability(
            policy=monitoring_policy(settings),
            demos=stores["demos"],
            operations=stores["operations"],
        ),
        tutors=tutors,
        gateway=gateway,
        agents=agents,
        scheduler=scheduler,
        onboarding_webhook_url=settings.onboarding_webhook_url,
        handoff_ttl_seconds=settings.handoff_ttl_seconds,
        scheduling_enabled=settings.flag_scheduling_enabled,
        reminders_enabled=settings.flag_reminders_enabled,
        payments_enabled=settings.flag_payments_enabled,
        discounts_enabled=settings.flag_discounts_enabled,
    )


def build_orchestrator(settings: Settings | None = None) -> DemoCommandCenterOrchestrator:
    return _cached(
        "orchestrator", lambda: DemoCommandCenterOrchestrator(build_dependencies(settings))
    )


def _warn_and_fake(reason: str, fake: Any) -> Any:
    logger.warning("falling back to a fake adapter", extra={"dcc_reason": reason})
    return fake
