"""FastAPI app.

Hosts the internal handoff endpoint and health probes. Wrapped by Mangum for
Lambda; runnable directly with `make run` for local integration against a real
Lead Intake instance.
"""

from __future__ import annotations

from fastapi import FastAPI

from tutor_match_meta.api.internal import router as internal_router
from tutor_match_meta.config.settings import get_settings
from tutor_match_meta.observability.context import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    application = FastAPI(
        title="NXTutors TutorMatch Meta Agent",
        version="0.1.0",
        # No public docs: this service has no public surface.
        docs_url=None if settings.is_deployed else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_deployed else "/openapi.json",
    )
    application.include_router(internal_router)
    return application


app = create_app()
