"""Embedding cost control.

Re-embedding an unchanged corpus is the purest form of wasted spend: it costs
real money and changes nothing. So embedding is gated on a content hash, and
the ledger of what has been embedded lives in `embedding_ledger`.

The rule (§18):

    checksum unchanged and same model  ->  skip entirely
    checksum changed                   ->  embed that chunk only
    model changed                      ->  re-embed (a vector from another
                                           model is not comparable)

Three things are refused outright rather than merely discouraged: credentials
and one-time codes, operational noise, and raw conversation turns. A vector
index is a durable, hard-to-redact store; putting a parent's WhatsApp message
in one is a privacy decision disguised as a performance optimisation.

Batching is used wherever latency does not matter. Embedding runs in the
scheduled job, never on the WhatsApp path, so a 200-chunk batch that takes ten
seconds costs nothing a parent can perceive.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.rag.pipeline import Chunk, checksum_of
from tutor_match_meta.security.pii import contains_pii, found_pii_kinds

logger = get_logger("rag.embeddings")

#: Provider batch ceiling. Larger batches amortise request overhead; beyond a
#: couple of hundred the payload starts hitting request size limits.
DEFAULT_BATCH_SIZE = 128

#: Micro-USD per 1k tokens. Configuration for the cost graph, not a price quote.
EMBEDDING_COST_MICROS_PER_1K: dict[str, int] = {
    "text-embedding-3-small": 20,
    "text-embedding-3-large": 130,
}

#: Content that must never enter a vector index, whatever its checksum says.
#: Matched on content because an embedding corpus is assembled from many
#: sources and the type information is long gone by the time text arrives here.
_NEVER_EMBED = re.compile(
    r"\b(?:otp|one[\s-]?time[\s-]?password|passcode|password|api[\s_-]?key|"
    r"bearer\s+[A-Za-z0-9._-]{10,}|secret|private[\s_-]?key|cvv|pin\s*:\s*\d{4,})\b",
    re.IGNORECASE,
)

#: Chunk kinds that are operational noise: high volume, no retrieval value.
SKIP_KINDS: frozenset[str] = frozenset({"conversation_turn", "audit_event", "heartbeat"})


class EmbeddingRefused(ValueError):
    """Content that must not be embedded. Not an error to retry."""


@runtime_checkable
class EmbeddingBackend(Protocol):
    model: str
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(slots=True)
class EmbeddingReport:
    """What one ingestion run actually spent."""

    considered: int = 0
    skipped_unchanged: int = 0
    skipped_refused: int = 0
    embedded: int = 0
    tokens: int = 0
    cost_micros: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def skip_rate(self) -> float:
        return round(self.skipped_unchanged / self.considered, 4) if self.considered else 0.0


def refuse_reason(text: str, *, kind: str = "") -> str | None:
    """Why this content must not be embedded, or None if it may be.

    Returns a *reason*, not a bool, so the report can say what was refused
    without ever logging the content itself.
    """
    if kind in SKIP_KINDS:
        return f"kind_not_embeddable:{kind}"
    if not text.strip():
        return "empty"
    if _NEVER_EMBED.search(text):
        return "credential_like_content"
    if contains_pii(text):
        # Direct identifiers are redacted at ingestion (`RagIndex.ingest`). If
        # any survive to here the pipeline has been bypassed, and embedding
        # would bake them into a vector nobody can later redact.
        return f"unredacted_pii:{','.join(found_pii_kinds(text))}"
    return None


@runtime_checkable
class EmbeddingLedger(Protocol):
    """Content-hash bookkeeping. The thing that makes a re-run nearly free."""

    async def seen(self, chunk_id: str, checksum: str, model: str) -> bool: ...

    async def record(
        self, *, chunk_id: str, checksum: str, model: str, dimensions: int, tokens: int, cost: int
    ) -> None: ...


class InMemoryEmbeddingLedger:
    """Single-process ledger for tests and local runs."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[str, str]] = {}

    async def seen(self, chunk_id: str, checksum: str, model: str) -> bool:
        return self._seen.get(chunk_id) == (checksum, model)

    async def record(
        self, *, chunk_id: str, checksum: str, model: str, dimensions: int, tokens: int, cost: int
    ) -> None:
        self._seen[chunk_id] = (checksum, model)


class PostgresEmbeddingLedger:
    """Durable ledger. Survives redeploys, which is the whole point."""

    def __init__(self, sessions: Any, *, schema: str) -> None:
        self._sessions = sessions
        self._table = f"{schema}.embedding_ledger" if schema else "embedding_ledger"

    async def seen(self, chunk_id: str, checksum: str, model: str) -> bool:
        from sqlalchemy import text

        try:
            async with self._sessions() as session:
                row = await session.scalar(
                    text(
                        f"SELECT 1 FROM {self._table} WHERE chunk_id = :id "  # noqa: S608
                        "AND content_checksum = :sum AND embedding_model = :model"
                    ),
                    {"id": chunk_id, "sum": checksum, "model": model},
                )
        except Exception:
            # Fail *open* into re-embedding: a ledger outage should cost money,
            # not correctness. A stale index is worse than a duplicate spend.
            logger.warning("embedding ledger unreadable; treating chunk as unseen")
            return False
        return row is not None

    async def record(
        self, *, chunk_id: str, checksum: str, model: str, dimensions: int, tokens: int, cost: int
    ) -> None:
        from sqlalchemy import text

        try:
            async with self._sessions() as session, session.begin():
                await session.execute(
                    text(
                        f"INSERT INTO {self._table} "  # noqa: S608
                        "(chunk_id, content_checksum, embedding_model, dimensions, "
                        " token_count, cost_micros) "
                        "VALUES (:id, :sum, :model, :dim, :tokens, :cost) "
                        "ON CONFLICT (chunk_id) DO UPDATE SET "
                        "content_checksum = EXCLUDED.content_checksum, "
                        "embedding_model = EXCLUDED.embedding_model, "
                        "dimensions = EXCLUDED.dimensions, "
                        "token_count = EXCLUDED.token_count, "
                        "cost_micros = EXCLUDED.cost_micros, "
                        "embedded_at = now()"
                    ),
                    {
                        "id": chunk_id,
                        "sum": checksum,
                        "model": model,
                        "dim": dimensions,
                        "tokens": tokens,
                        "cost": cost,
                    },
                )
        except Exception:
            logger.warning("embedding ledger write failed; chunk may be re-embedded next run")


def cost_micros(model: str, tokens: int) -> int:
    rate = EMBEDDING_COST_MICROS_PER_1K.get(model)
    return round(tokens / 1000 * rate) if rate else 0


async def embed_changed(
    chunks: Sequence[Chunk],
    *,
    backend: EmbeddingBackend,
    ledger: EmbeddingLedger,
    batch_size: int = DEFAULT_BATCH_SIZE,
    store: Any | None = None,
) -> EmbeddingReport:
    """Embed only what changed. Returns what it actually cost.

    Order matters: refuse first (never pay to embed a credential), then check
    the ledger (never pay twice for the same bytes), then batch what remains.
    """
    report = EmbeddingReport(considered=len(chunks))
    pending: list[Chunk] = []

    for chunk in chunks:
        reason = refuse_reason(chunk.content, kind=str(chunk.kind))
        if reason is not None:
            report.skipped_refused += 1
            report.reasons.append(f"{chunk.chunk_id}:{reason}")
            continue
        checksum = chunk.checksum or checksum_of(chunk.content)
        if await ledger.seen(chunk.chunk_id, checksum, backend.model):
            report.skipped_unchanged += 1
            continue
        pending.append(chunk)

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        try:
            vectors = await backend.embed([c.content for c in batch])
        except Exception as exc:
            # A failed batch is retried on the next scheduled run: the ledger
            # was not written, so those chunks are still "unseen". Nothing is
            # lost and nothing is double-charged.
            report.reasons.append(f"batch_failed:{type(exc).__name__}")
            logger.warning("embedding batch failed", extra={"tmm_batch": len(batch)})
            continue

        for chunk, vector in zip(batch, vectors, strict=False):
            tokens = chunk.token_count
            spend = cost_micros(backend.model, tokens)
            if store is not None:
                await store.save_embedding(chunk.chunk_id, vector, model=backend.model)
            await ledger.record(
                chunk_id=chunk.chunk_id,
                checksum=chunk.checksum,
                model=backend.model,
                dimensions=backend.dimensions,
                tokens=tokens,
                cost=spend,
            )
            report.embedded += 1
            report.tokens += tokens
            report.cost_micros += spend

    logger.info(
        "embedding run complete",
        extra={
            "tmm_considered": report.considered,
            "tmm_embedded": report.embedded,
            "tmm_skipped_unchanged": report.skipped_unchanged,
            "tmm_skipped_refused": report.skipped_refused,
            "tmm_cost_micros": report.cost_micros,
        },
    )
    return report


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "EMBEDDING_COST_MICROS_PER_1K",
    "SKIP_KINDS",
    "EmbeddingBackend",
    "EmbeddingLedger",
    "EmbeddingRefused",
    "EmbeddingReport",
    "InMemoryEmbeddingLedger",
    "PostgresEmbeddingLedger",
    "cost_micros",
    "embed_changed",
    "refuse_reason",
]
