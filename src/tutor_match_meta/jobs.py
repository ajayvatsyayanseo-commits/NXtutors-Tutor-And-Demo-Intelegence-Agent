"""Scheduled maintenance jobs.

Dispatched by name from EventBridge through an explicit allowlist, so a
malformed rule cannot invoke an arbitrary callable.

None of these run on the WhatsApp path. Glue and the heavier ETL work is offline
by design (docs/integration-inventory.md §9).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from tutor_match_meta.config.settings import Settings
from tutor_match_meta.observability.context import get_logger

logger = get_logger("jobs")

JobFn = Callable[[Settings], Awaitable[dict[str, Any]]]


async def sync_projection(settings: Settings) -> dict[str, Any]:
    """Fetch **and** apply in one process.

    Correct locally and from the CLI, where one process has both the
    internet and the database. Not deployable: with no NAT Gateway no single
    Lambda has both, which is why the deployed path is the two jobs below.
    """
    from tutor_match_meta.sync.projection import run_incremental_sync

    return await run_incremental_sync(settings)


async def stage_tutor_feed(settings: Settings) -> dict[str, Any]:
    """[internet zone] Website feed -> S3. Opens no database connection."""
    from tutor_match_meta.sync.feed_staging import stage_feed_pages

    return await stage_feed_pages(settings)


async def ingest_tutor_feed(settings: Settings) -> dict[str, Any]:
    """[vpc zone] S3 -> tutor_projection. Makes no internet call."""
    from tutor_match_meta.sync.feed_staging import ingest_staged_pages

    return await ingest_staged_pages(settings)


async def reconcile_projection(settings: Settings) -> dict[str, Any]:
    """Full checksum comparison; repairs drift the incremental sync missed."""
    from tutor_match_meta.sync.projection import run_reconciliation

    return await run_reconciliation(settings)


async def check_staleness(settings: Settings) -> dict[str, Any]:
    """Emit the projection freshness gauge and alarm when it ages out."""
    from tutor_match_meta.sync.projection import report_staleness

    return await report_staleness(settings)


async def relay_outbox(settings: Settings) -> dict[str, Any]:
    """Drain the transactional outbox to SQS. Safety net for a crashed worker."""
    from tutor_match_meta.sync.outbox_relay import relay

    return await relay(settings)


async def retention_cleanup(settings: Settings) -> dict[str, Any]:
    """Delete data past its retention window (docs/data-ownership-matrix.md)."""
    from tutor_match_meta.sync.retention import run_cleanup

    return await run_cleanup(settings)


#: The dispatch allowlist. EventBridge rules reference these names.
JOBS: dict[str, JobFn] = {
    "sync_projection": sync_projection,
    "stage_tutor_feed": stage_tutor_feed,
    "ingest_tutor_feed": ingest_tutor_feed,
    "reconcile_projection": reconcile_projection,
    "check_staleness": check_staleness,
    "relay_outbox": relay_outbox,
    "retention_cleanup": retention_cleanup,
}
