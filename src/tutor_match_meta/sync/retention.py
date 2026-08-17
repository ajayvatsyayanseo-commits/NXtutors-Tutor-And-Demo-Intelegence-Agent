"""Retention cleanup.

Deletes data past the windows in docs/data-ownership-matrix.md. Runs nightly.

Deletion is by age and in bounded batches: an unbounded `DELETE` on a large
table takes a long lock, and this job shares a database with the live matcher.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete

from tutor_match_meta.config.settings import Settings
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.repositories.models import (
    IdempotencyRecord,
    LLMUsageRow,
    MatchDecisionRow,
    OutboxEvent,
)
from tutor_match_meta.repositories.postgres import build_sessions, create_engine

logger = get_logger("sync.retention")

#: Days, matching the ownership matrix. Decisions are kept longest because they
#: are the audit trail for what a parent was told.
RETENTION_DAYS = {
    "match_decision": 400,
    "llm_usage": 180,
    "outbox_delivered": 30,
    "idempotency": 7,
}


async def run_cleanup(settings: Settings) -> dict[str, Any]:
    sessions = build_sessions(create_engine(settings))
    now = datetime.now(UTC)
    removed: dict[str, int] = {}

    async with sessions() as session, session.begin():
        result = await session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < now)
        )
        removed["idempotency"] = result.rowcount or 0

        result = await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.status == "delivered",
                OutboxEvent.delivered_at < now - timedelta(days=RETENTION_DAYS["outbox_delivered"]),
            )
        )
        removed["outbox_delivered"] = result.rowcount or 0

        result = await session.execute(
            delete(LLMUsageRow).where(
                LLMUsageRow.created_at < now - timedelta(days=RETENTION_DAYS["llm_usage"])
            )
        )
        removed["llm_usage"] = result.rowcount or 0

        result = await session.execute(
            delete(MatchDecisionRow).where(
                MatchDecisionRow.generated_at
                < now - timedelta(days=RETENTION_DAYS["match_decision"])
            )
        )
        removed["match_decision"] = result.rowcount or 0

    logger.info("retention cleanup complete", extra={"tmm_removed": removed})
    return removed
