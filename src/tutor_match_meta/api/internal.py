"""The internal handoff endpoint Lead Intake calls.

Deliberately **not** a public Meta webhook. Lead Intake owns the only public
WhatsApp route (`app/api/whatsapp.py` in that repo, with the verify token and
app-secret signature); exposing a second one here would give Meta two places to
deliver the same message.

Authentication mirrors what Lead Intake already sends to the onboarding agent:
a shared secret in `X-NXTUTORS-INTERNAL-SECRET`. The stronger HMAC scheme in
`security/signing.py` is available and used by the SQS ingress path, but the
header secret is what the caller sends *today* — matching it means Lead Intake
needs no code change to start routing to us.

Every response is a 200 with a status in the body, including failures. Lead
Intake treats a non-2xx as retryable, and retrying a message we deliberately
declined would loop.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response
from pydantic import ValidationError

from tutor_match_meta.config.flags import from_settings
from tutor_match_meta.config.settings import Settings, get_settings
from tutor_match_meta.contracts.handoff import (
    HandoffResponseV1,
    HandoffStatus,
    LeadIntakeHandoffV1,
)
from tutor_match_meta.observability.context import (
    RequestContext,
    get_logger,
    new_trace_id,
    request_context,
)

logger = get_logger("api.internal")
router = APIRouter(prefix="/internal/v1", tags=["internal"])


def _authorised(provided: str | None, settings: Settings) -> bool:
    expected = settings.internal_secret.get_secret_value()
    if not expected:
        # Fail closed. An unset secret in a deployed environment means the
        # endpoint is unauthenticated, which is worse than being unavailable.
        logger.error("internal secret is not configured; refusing all handoffs")
        return False
    return hmac.compare_digest(expected, (provided or "").strip())


@router.post("/handoff")
async def handoff(
    request: Request,
    response: Response,
    x_nxtutors_internal_secret: Annotated[str | None, Header()] = None,
    x_trace_id: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Accept one message from Lead Intake and return `{status, reply_text}`."""
    settings = get_settings()
    trace_id = (x_trace_id or "").strip() or new_trace_id()

    if not _authorised(x_nxtutors_internal_secret, settings):
        response.status_code = 401
        return {"status": "unauthorized", "reply_text": None}

    try:
        raw_body = await request.json()
    except (ValueError, UnicodeDecodeError):
        response.status_code = 400
        return {"status": "bad_request", "reply_text": None}

    try:
        payload = LeadIntakeHandoffV1.model_validate(raw_body)
    except ValidationError as exc:
        logger.warning("handoff payload rejected", extra={"tmm_errors": exc.error_count()})
        response.status_code = 422
        return {"status": "invalid_payload", "reply_text": None}

    from tutor_match_meta.bootstrap import build_handoff_service

    service, pseudonymiser = await build_handoff_service(settings)
    context = RequestContext(
        trace_id=trace_id,
        conversation_id_hash=pseudonymiser.conversation(payload.effective_conversation_id()),
        message_id=payload.wa_message_id,
        source_agent=payload.source,
    )
    with request_context(context):
        try:
            result = await service.handle(payload, trace_id=trace_id)
        except Exception:
            # Never 5xx: Lead Intake would retry, and a retry of a crash is
            # another crash. Return ERROR so it falls back to its own reply.
            logger.exception("handoff failed")
            return HandoffResponseV1.errored("internal_error", trace_id=trace_id).to_body()

    logger.info(
        "handoff complete",
        extra={"tmm_status": result.status.value, "tmm_replied": bool(result.reply_text)},
    )
    response_body: dict[str, Any] = result.to_body()
    return response_body


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus the rollout posture.

    Deliberately exposes flag state and no secrets: during a staged rollout the
    first question is always "is this conversation even going to TutorMatch",
    and the answer should not require a deploy to find out.
    """
    settings = get_settings()
    flags = from_settings(settings)
    return {
        "status": "ok",
        "service": settings.service_name,
        "environment": settings.environment.value,
        "outbound_ownership": settings.outbound_ownership,
        "whatsapp_sender_enabled": settings.whatsapp_enabled,
        "flags": flags.describe(),
        "integrations": {
            "chitragupta": settings.chitragupta_enabled,
            "website_write": settings.website_write_enabled,
            "llm_provider": settings.llm_provider,
            "internal_secret_configured": bool(settings.internal_secret.get_secret_value()),
        },
    }


@router.get("/version")
async def version(
    response: Response,
    x_nxtutors_internal_secret: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """What is deployed: app, commit, schema, policy, prompts, contract.

    Authenticated with the same shared secret as `/handoff`. None of it is a
    secret, but an unauthenticated build-identity endpoint hands an attacker a
    precise version to look up known issues against, and it costs nothing to
    require the header we already have.

    Prompt and policy versions are reported separately from the app version on
    purpose: those roll back independently (§33), and an operator mid-incident
    needs to know which of the three they are actually looking at.
    """
    settings = get_settings()
    if not _authorised(x_nxtutors_internal_secret, settings):
        response.status_code = 401
        return {"status": "unauthorized"}

    from tutor_match_meta.bootstrap import build_model_routing, kill_switches
    from tutor_match_meta.version import build_info

    switches = kill_switches(settings)
    return {
        "status": "ok",
        **build_info().as_dict(),
        "models": build_model_routing(settings).as_dict(),
        "kill_switches": await switches.snapshot(),
        "holding_work": await switches.any_lossy_pause(),
    }


@router.get("/ready")
async def ready(response: Response) -> dict[str, Any]:
    """Readiness. Fails when a hard dependency is misconfigured."""
    settings = get_settings()
    problems: list[str] = []
    if not settings.internal_secret.get_secret_value():
        problems.append("internal_secret_missing")
    if not settings.continuation_signing_key.get_secret_value():
        problems.append("continuation_signing_key_missing")
    try:
        from tutor_match_meta.bootstrap import build_policy_registry

        build_policy_registry(settings).load_all()
    except Exception as exc:
        problems.append(f"policy_load_failed:{type(exc).__name__}")

    try:
        # A prompt pin that names a version this build does not contain would
        # otherwise fail on the first real message, one conversation at a time.
        # Resolving every prompt here turns that into a readiness failure.
        from tutor_match_meta.prompts.registry import REGISTRY

        REGISTRY.active()
    except Exception as exc:
        problems.append(f"prompt_pin_unresolvable:{exc}")

    # The database must actually hold what this build expects. A missing table
    # or a migration that never ran otherwise surfaces mid-conversation, one
    # code path at a time — see repositories/schema_check.py.
    from tutor_match_meta.bootstrap import database_sessions
    from tutor_match_meta.repositories import schema_check

    sessions = database_sessions(settings)
    if sessions is None:
        if settings.is_deployed:
            problems.append("postgres_dsn_missing")
    else:
        report = await schema_check.verify(sessions, schema=settings.postgres_schema)
        problems.extend(report.problems())

    if problems:
        response.status_code = 503
        return {"status": "not_ready", "problems": problems}
    return {"status": "ready"}


#: Statuses Lead Intake should treat as "I will send this text".
SENDABLE_STATUSES = frozenset({HandoffStatus.HANDLED})
