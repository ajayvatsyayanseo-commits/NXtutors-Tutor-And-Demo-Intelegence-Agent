"""The projection sync, split across the network boundary.

`sync/projection.py` does both halves in one process, which is correct for local
development and the CLI. It is not deployable: fetching needs the public
internet and upserting needs PostgreSQL, and with no NAT Gateway no single
Lambda has both.

So the deployed shape is two jobs and a staging area:

    tutor-feed-fetcher  [internet]  website feed -> S3 (JSON pages)
                                    no database handle at all
    scheduled           [VPC]       S3 -> tutor_projection
                                    reads through the S3 *gateway* endpoint

S3 rather than SQS for this hop, deliberately. A page of 200 tutors with profile
summaries can exceed SQS's 256 KB message ceiling, and the claim-check pattern
that works around it needs S3 anyway. A gateway endpoint also costs nothing,
where an interface endpoint bills per hour per AZ — and the object *is* the
message, so there is nothing to keep in sync between the two.

Staged pages are deleted once ingested, so the prefix is a work queue rather
than an archive: a page still present is a page not yet applied.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from tutor_match_meta.config.settings import Settings
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.observability.metrics import Metric, MetricsEmitter
from tutor_match_meta.repositories.postgres import build_sessions, create_engine
from tutor_match_meta.sync.projection import MAX_PAGES_PER_RUN, _feed_source, _upsert

logger = get_logger("sync.feed_staging")

#: Bounded per run so one invocation cannot exceed the Lambda timeout, and so a
#: runaway upstream cannot fill the bucket in a single sweep.
MAX_OBJECTS_PER_INGEST = 50


def _client(settings: Settings) -> Any:
    import boto3

    return boto3.client("s3", region_name=settings.aws_region)


def _key(prefix: str, stamp: datetime, page: int) -> str:
    return f"{prefix.rstrip('/')}/{stamp:%Y/%m/%d}/{stamp:%H%M%S}-{page:04d}.json"


# ------------------------------------------------------------------- internet
async def stage_feed_pages(settings: Settings) -> dict[str, Any]:
    """Page the website feed and write each page to S3. Touches no database.

    Runs in the internet zone. Deliberately stateless with respect to the
    checkpoint: the checkpoint lives in PostgreSQL, which this side cannot
    reach, so a fetch run always starts at offset 0 and sweeps forward. That is
    affordable because the tutor base is small (~1.9k rows) and the ingest side
    upserts, so re-seeing a tutor is a no-op beyond moving `synced_at`.
    """
    if not settings.analytics_bucket:
        return {"skipped": "no_bucket"}

    source = _feed_source(settings)
    if source is None:
        logger.warning("feed fetch skipped: website feed is not configured")
        return {"skipped": "feed_not_configured"}

    metrics = MetricsEmitter().with_dimensions(environment=settings.environment.value)
    client = _client(settings)
    stamp = datetime.now(UTC)
    offset = 0
    staged = 0
    tutors = 0
    error: str | None = None

    try:
        for page_number in range(MAX_PAGES_PER_RUN):
            page = await source.fetch_page(offset=offset, now=stamp)
            if not page.fetched:
                break
            body = json.dumps(
                {
                    "staged_at": stamp.isoformat(),
                    "offset": offset,
                    "tutors": [t.model_dump(mode="json") for t in page.tutors],
                },
                separators=(",", ":"),
            ).encode("utf-8")
            client.put_object(
                Bucket=settings.analytics_bucket,
                Key=_key(settings.feed_staging_prefix, stamp, page_number),
                Body=body,
                ContentType="application/json",
                ServerSideEncryption="aws:kms",
            )
            staged += 1
            tutors += page.fetched
            offset += page.fetched
            if not page.has_more:
                break
    except Exception as exc:  # recorded, not swallowed
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("feed fetch failed")
        metrics.count(Metric.FEED_FAILURES)

    metrics.flush()
    return {"pages_staged": staged, "tutors": tutors, "error": error}


# ------------------------------------------------------------------------ VPC
async def ingest_staged_pages(settings: Settings) -> dict[str, Any]:
    """Read staged pages from S3 and upsert them. Makes no internet call.

    Runs in the VPC zone, reaching S3 through the gateway endpoint. A page that
    fails to apply is left in place: the next run retries it, and a page that
    keeps failing stays visible in the bucket rather than vanishing.
    """
    if not settings.analytics_bucket:
        return {"skipped": "no_bucket"}

    client = _client(settings)
    sessions = build_sessions(create_engine(settings))
    now = datetime.now(UTC)
    prefix = settings.feed_staging_prefix.rstrip("/") + "/"

    listing = client.list_objects_v2(
        Bucket=settings.analytics_bucket, Prefix=prefix, MaxKeys=MAX_OBJECTS_PER_INGEST
    )
    keys = [item["Key"] for item in listing.get("Contents", [])]
    if not keys:
        return {"pages": 0, "tutors": 0}

    applied = 0
    tutors = 0
    failed: list[str] = []

    for key in keys:
        try:
            body = client.get_object(Bucket=settings.analytics_bucket, Key=key)["Body"].read()
            payload = json.loads(body)
            candidates = [TutorCandidate.model_validate(row) for row in payload.get("tutors", [])]
        except Exception:
            logger.exception("unreadable staged page", extra={"tmm_key": key})
            failed.append(key)
            continue

        try:
            tutors += await _upsert(sessions, candidates, now)
        except Exception:
            # Left in place on purpose. Deleting it would lose the page.
            logger.exception("failed to apply staged page", extra={"tmm_key": key})
            failed.append(key)
            continue

        client.delete_object(Bucket=settings.analytics_bucket, Key=key)
        applied += 1

    metrics = MetricsEmitter().with_dimensions(environment=settings.environment.value)
    if failed:
        metrics.count(Metric.FEED_FAILURES, len(failed))
    metrics.put(Metric.CANDIDATE_POOL_SIZE, tutors)
    metrics.flush()

    logger.info(
        "staged pages ingested",
        extra={"tmm_pages": applied, "tmm_tutors": tutors, "tmm_failed": len(failed)},
    )
    return {"pages": applied, "tutors": tutors, "failed": len(failed)}
