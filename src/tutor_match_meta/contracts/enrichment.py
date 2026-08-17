"""The versioned hop between the Internet side and the database side.

This contract exists because of one hard architectural constraint: there is no
NAT Gateway. A Lambda attached to the private subnets can reach RDS Proxy and
the VPC endpoints, and **nothing else** — no `api.openai.com`, no geocoder, no
memory service. A Lambda outside the VPC can reach the internet and **not** the
database.

The match worker needs both. So it is split at the network boundary:

    ingress            (outside the VPC)  validate, sign-check, enqueue
      │ SQS enrich.fifo
    enrich worker      (outside the VPC)  OpenAI + memory           — NO database
      │ SQS match.fifo
    match worker       (inside the VPC)   PostgreSQL, pgvector      — NO internet
      │ SQS outbound.fifo
    outbound worker    (outside the VPC)  WhatsApp Cloud API        — NO database

`EnrichmentV1` is what crosses the second boundary: everything the match worker
would otherwise have had to make a network call to obtain. It is versioned
because the two sides deploy as separate Lambdas and will, at some point, be
running different builds for a few minutes.

Nothing here carries raw PII: the requirement holds normalised tutoring facts,
and memory facts are already keyed by a pseudonymised conversation reference.

Geocoding is not on this hop. Its primary backend is the `geo_point` table, so
it runs on the database side; see config/settings.py for why the paid HTTP
geocoder is refused in a deployed environment.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tutor_match_meta.contracts.common import SCHEMA_VERSION
from tutor_match_meta.contracts.requirement import MatchRequirementV1

#: Bumped when the shape changes incompatibly. The match worker refuses a
#: version it does not understand rather than silently ignoring new fields.
ENRICHMENT_VERSION = "1"

#: A conversation cannot legitimately recall more than this. A larger packet is
#: a misbehaving memory service, and it would inflate every SQS message.
MAX_MEMORY_FACTS = 32


class EnrichmentV1(BaseModel):
    """Everything the internet side resolved, ready for the database side."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    enrichment_version: str = ENRICHMENT_VERSION

    #: The extracted requirement for *this turn only*. The match worker merges
    #: it with the stored requirement; merging needs the database, so it stays
    #: on the database side.
    requirement: MatchRequirementV1

    #: Facts recalled from the memory service, already filtered for confidence
    #: and denial. Empty when memory is disabled or degraded.
    memory_facts: dict[str, str] = Field(default_factory=dict)

    #: Sources that failed on the internet side. The match worker folds these
    #: into the turn's `degraded_sources` so one report covers both hops.
    degraded: tuple[str, ...] = ()

    #: Prompt-injection patterns detected in the parent's text. Counted for
    #: alerting on the database side, where the metric emitter runs.
    injection_detections: tuple[str, ...] = ()

    #: True when a model was actually called, so cost attribution is honest.
    used_llm: bool = False
    #: Set when the provider failed; the class name only, never the message.
    llm_error: str | None = None

    enriched_at: datetime

    @model_validator(mode="after")
    def _bound_memory(self) -> Self:
        if len(self.memory_facts) > MAX_MEMORY_FACTS:
            raise ValueError(f"memory packet exceeds {MAX_MEMORY_FACTS} facts")
        return self


class UnsupportedEnrichment(Exception):
    """The enrichment came from a build this worker cannot read."""


def assert_supported(enrichment: EnrichmentV1) -> None:
    """Fail loudly on a version skew rather than matching on partial data.

    A rejected record goes to the DLQ, which is visible. Silently treating an
    unreadable enrichment as "no enrichment" would produce a worse shortlist
    with no signal that anything was wrong.
    """
    if enrichment.enrichment_version != ENRICHMENT_VERSION:
        raise UnsupportedEnrichment(
            f"enrichment_version {enrichment.enrichment_version!r} "
            f"!= supported {ENRICHMENT_VERSION!r}"
        )
