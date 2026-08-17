"""Website feed → PostgreSQL projection sync.

Runs on a schedule, never on the WhatsApp path. The runtime match reads the
projection; this is the only thing that reads the website.

The source is a signed HTTPS feed (`integrations/website/tutor_feed.py`), not a
second database. See that module for why MySQL was removed.

Three modes, deliberately separate:

* **incremental** — pages forward from a checkpoint, cheap, every 15 minutes;
* **reconciliation** — full checksum sweep, catches rows the incremental pass
  missed (a tutor deactivated between pages, a checkpoint that skipped);
* **staleness report** — emits the freshness gauge the alarm watches.

The checksum is what makes reconciliation cheap: comparing one hash per tutor is
far less work than diffing twenty columns, and it detects any change including
ones we do not project.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tutor_match_meta.config.settings import Settings
from tutor_match_meta.contracts.schedule import MINUTES_PER_DAY
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.observability.metrics import Metric, MetricsEmitter, Unit
from tutor_match_meta.repositories.models import (
    SyncCheckpoint,
    TutorAvailability,
    TutorProjection,
)
from tutor_match_meta.repositories.postgres import build_sessions, create_engine
from tutor_match_meta.security.urls import UrlPolicy

logger = get_logger("sync.projection")

#: Marks rows this sync owns, so a window a tutor confirmed with us directly
#: is never deleted by a website refresh.
AVAILABILITY_SOURCE = "website_feed"

CHECKPOINT_INCREMENTAL = "tutor_projection_incremental"
#: Bounded per invocation so a Lambda run cannot exceed its timeout on a large
#: tutor base; the checkpoint means the next run picks up where this stopped.
MAX_PAGES_PER_RUN = 25


def projection_checksum(tutor: TutorCandidate) -> str:
    """Hash of everything we project. Any change to any field moves the hash."""
    material = json.dumps(
        {
            "name": tutor.name,
            "gender": tutor.gender,
            "city": tutor.city,
            "pincode": tutor.pincode,
            "subjects": sorted(tutor.capabilities.subjects),
            "boards": sorted(tutor.capabilities.boards),
            "classes": sorted(tutor.capabilities.classes),
            "modes": sorted(m.value for m in tutor.capabilities.modes),
            "experience": tutor.experience_years,
            "education": tutor.education,
            "fee": [tutor.fee.minimum, tutor.fee.maximum],
            "reviews": [tutor.reviews.count, tutor.reviews.rating_avg],
            "profile": tutor.profile_summary,
            # Included so a schedule change alone moves the hash; without it
            # reconciliation would never repair drifted availability.
            "availability": sorted(
                (int(w.weekday), w.start.isoformat(), w.end.isoformat())
                for w in (tutor.availability.windows if tutor.availability else ())
            ),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_values(tutor: TutorCandidate, now: datetime) -> dict[str, Any]:
    return {
        "tutor_id": tutor.tutor_id,
        "public_ref": tutor.public_ref,
        "name": tutor.name,
        "gender": tutor.gender,
        "avatar_url": tutor.avatar_url,
        "city": tutor.city,
        "locality": tutor.locality,
        "district": tutor.district,
        "state": tutor.state,
        "pincode": tutor.pincode,
        "subjects": list(tutor.capabilities.subjects),
        "boards": list(tutor.capabilities.boards),
        "classes": list(tutor.capabilities.classes),
        "modes": [m.value for m in tutor.capabilities.modes],
        "experience_years": tutor.experience_years,
        "education": tutor.education,
        "profile_summary": tutor.profile_summary,
        "fee_min": tutor.fee.minimum,
        "fee_max": tutor.fee.maximum,
        "fee_label": tutor.fee.label,
        "review_count": tutor.reviews.count,
        "rating_avg": tutor.reviews.rating_avg,
        "expertise_avg": tutor.reviews.expertise_avg,
        "patience_avg": tutor.reviews.patience_avg,
        "reliability_avg": tutor.reviews.reliability_avg,
        "communication_avg": tutor.reviews.communication_avg,
        "latest_review_at": tutor.reviews.latest_review_at,
        "source_updated_at": tutor.source_updated_at,
        "synced_at": now,
        "source_checksum": projection_checksum(tutor),
    }


async def _upsert(sessions: Any, tutors: list[TutorCandidate], now: datetime) -> int:
    """Upsert a page. `synced_at` always moves, even when nothing else changed —
    that is what proves the row was seen, not merely that it is unchanged.

    One transaction for the whole page: a tutor whose profile is updated but
    whose availability is not would be readable in an inconsistent pair of
    states, and agent 013 would answer from it.
    """
    if not tutors:
        return 0
    async with sessions() as session, session.begin():
        for tutor in tutors:
            values = _row_values(tutor, now)
            await session.execute(
                pg_insert(TutorProjection)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[TutorProjection.tutor_id],
                    set_={k: v for k, v in values.items() if k != "tutor_id"},
                )
            )
            await _replace_availability(session, tutor, now)
    return len(tutors)


async def _replace_availability(session: Any, tutor: TutorCandidate, now: datetime) -> None:
    """Replace this tutor's published windows, wholesale.

    Delete-then-insert rather than upsert-by-slot, because a window the website
    *removed* has to disappear. Merging would leave a stale Tuesday slot behind
    forever, and agent 013 would keep offering a time the tutor withdrew.

    Only rows this sync owns are touched: windows a tutor confirmed directly
    with us carry a different `source` and are left alone.
    """
    await session.execute(
        delete(TutorAvailability).where(
            TutorAvailability.tutor_id == tutor.tutor_id,
            TutorAvailability.source == AVAILABILITY_SOURCE,
        )
    )
    schedule = tutor.availability
    if not schedule or not schedule.windows:
        return
    # `TimeWindow.*_minute` counts from Monday 00:00; the table stores minutes
    # within the day, with 1440 meaning end-of-day. Subtracting the weekday
    # offset converts exactly, including the midnight case, which a modulo
    # would fold to 0 and trip the `end_minute > start_minute` constraint.
    session.add_all(
        [
            TutorAvailability(
                tutor_id=tutor.tutor_id,
                weekday=int(window.weekday),
                start_minute=window.start_minute - int(window.weekday) * MINUTES_PER_DAY,
                end_minute=window.end_minute - int(window.weekday) * MINUTES_PER_DAY,
                timezone=schedule.timezone,
                source=AVAILABILITY_SOURCE,
                confirmed_at=now,
            )
            for window in schedule.windows
        ]
    )


def _feed_source(settings: Settings) -> Any:
    """The signed HTTPS feed, or None when it is not configured."""
    from tutor_match_meta.integrations.website.tutor_feed import build_feed

    return build_feed(
        base_url=settings.website_api_base_url,
        signing_key=settings.website_api_signing_key.get_secret_value(),
        url_policy=UrlPolicy(
            allowed_hosts=_feed_hosts(settings),
            # Outside a deployed environment the feed may legitimately be a
            # local `php artisan serve`, which is plain HTTP on 127.0.0.1.
            # Deployed, this stays False and the SSRF guard requires HTTPS to an
            # allowlisted host — so a misconfigured base URL cannot be used to
            # reach the instance metadata endpoint.
            allow_local=not settings.is_deployed,
        ),
        page_size=settings.tutor_feed_page_size,
        timeout_seconds=settings.tutor_feed_timeout_seconds,
    )


def _feed_hosts(settings: Settings) -> frozenset[str]:
    from urllib.parse import urlparse

    host = urlparse(settings.website_api_base_url).hostname
    return frozenset({host}) if host else frozenset()


async def run_incremental_sync(settings: Settings) -> dict[str, Any]:
    """Page forward from the checkpoint, upserting as we go."""
    metrics = MetricsEmitter().with_dimensions(environment=settings.environment.value)
    source = _feed_source(settings)
    if source is None:
        logger.warning("projection sync skipped: website feed is not configured")
        return {"skipped": "feed_not_configured"}

    sessions = build_sessions(create_engine(settings))
    now = datetime.now(UTC)

    async with sessions() as session:
        checkpoint = await session.get(SyncCheckpoint, CHECKPOINT_INCREMENTAL)
        offset = checkpoint.last_offset if checkpoint else 0

    processed = 0
    pages = 0
    error: str | None = None
    try:
        while pages < MAX_PAGES_PER_RUN:
            page = await source.fetch_page(offset=offset, now=now)
            processed += await _upsert(sessions, list(page.tutors), now)
            offset += page.fetched
            pages += 1
            if not page.has_more:
                # A complete pass: restart from the top next run so
                # already-synced tutors are refreshed too.
                offset = 0
                break
    except Exception as exc:  # recorded, not swallowed
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("projection sync failed")
        metrics.count(Metric.FEED_FAILURES)

    await _save_checkpoint(sessions, offset=offset, processed=processed, error=error, now=now)
    metrics.put(Metric.CANDIDATE_POOL_SIZE, processed)
    metrics.flush()
    return {"processed": processed, "pages": pages, "next_offset": offset, "error": error}


async def _save_checkpoint(
    sessions: Any, *, offset: int, processed: int, error: str | None, now: datetime
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            pg_insert(SyncCheckpoint)
            .values(
                name=CHECKPOINT_INCREMENTAL,
                last_offset=offset,
                last_run_at=now,
                last_success_at=None if error else now,
                rows_processed=processed,
                last_error=error[:240] if error else None,
            )
            .on_conflict_do_update(
                index_elements=[SyncCheckpoint.name],
                set_={
                    "last_offset": offset,
                    "last_run_at": now,
                    "rows_processed": processed,
                    "last_error": error[:240] if error else None,
                    # Only advance last_success_at on an actual success, so the
                    # gap since the last good run stays visible.
                    **({} if error else {"last_success_at": now}),
                },
            )
        )


async def run_reconciliation(settings: Settings) -> dict[str, Any]:
    """Full sweep comparing checksums; repairs drift and removes vanished rows.

    A tutor deactivated on the website simply stops appearing in the feed,
    so they are detected by absence and aged out rather than deleted — the
    freshness filter already excludes them from matching, and keeping the row
    preserves the audit trail for decisions that referenced them.
    """
    source = _feed_source(settings)
    if source is None:
        return {"skipped": "feed_not_configured"}

    sessions = build_sessions(create_engine(settings))
    now = datetime.now(UTC)
    seen: set[str] = set()
    changed = 0
    offset = 0

    while True:
        page = await source.fetch_page(offset=offset, now=now)
        if not page.fetched:
            break
        async with sessions() as session:
            existing = {
                row.tutor_id: row.source_checksum
                for row in (
                    await session.scalars(
                        select(TutorProjection).where(
                            TutorProjection.tutor_id.in_([t.tutor_id for t in page.tutors])
                        )
                    )
                ).all()
            }
        drifted = [
            tutor
            for tutor in page.tutors
            if existing.get(tutor.tutor_id) != projection_checksum(tutor)
        ]
        changed += await _upsert(sessions, drifted, now)
        seen.update(t.tutor_id for t in page.tutors)
        offset += page.fetched
        if not page.has_more:
            break

    async with sessions() as session:
        total = await session.scalar(select(func.count()).select_from(TutorProjection)) or 0

    missing = total - len(seen)
    logger.info(
        "reconciliation complete",
        extra={"tmm_seen": len(seen), "tmm_changed": changed, "tmm_absent": missing},
    )
    return {"seen": len(seen), "repaired": changed, "absent_from_source": missing}


async def report_staleness(settings: Settings) -> dict[str, Any]:
    """Emit the freshness gauge the `projection-stale` alarm watches."""
    sessions = build_sessions(create_engine(settings))
    now = datetime.now(UTC)
    async with sessions() as session:
        oldest = await session.scalar(select(func.min(TutorProjection.synced_at)))
        newest = await session.scalar(select(func.max(TutorProjection.synced_at)))
        total = await session.scalar(select(func.count()).select_from(TutorProjection)) or 0

    if newest is None:
        # No rows at all is worse than stale rows: report a value that breaches.
        hours = 999.0
    else:
        hours = (now - newest).total_seconds() / 3600

    metrics = MetricsEmitter().with_dimensions(environment=settings.environment.value)
    metrics.put(Metric.PROJECTION_STALENESS_HOURS, round(hours, 2), Unit.NONE)
    metrics.flush()

    return {
        "tutors": total,
        "newest_sync_hours_ago": round(hours, 2),
        "oldest_sync": oldest.isoformat() if oldest else None,
        "recommendable": hours <= settings.projection_aging_hours,
    }
