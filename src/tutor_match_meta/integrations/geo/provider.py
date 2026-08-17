"""Geocoding — coarse, cached, capped, and never on a per-tutor basis.

The expensive mistake this module exists to prevent is calling a paid distance
API once per candidate. With a pool of 40 tutors that is 40 billable calls per
message; at a few thousand messages a month it is the single largest line in
the cost model and it buys nothing, because tutor coordinates do not change
between requests.

The shape instead (§21):

    1. tutor coordinates are **pre-geocoded** by the offline sync job and stored
       on the projection, so the matching path reads them for free;
    2. the family's location is geocoded **once per turn**, at pincode or
       locality granularity, never a street address (docs/assumptions.md A5);
    3. distance is computed locally with haversine — it is arithmetic, not a
       service call, and certainly not an LLM call (§15);
    4. a paid travel-time provider, if ever enabled, is reserved for the final
       two or three candidates where the business value is real.

Three layers of restraint on top of that:

* **Cache.** A pincode centroid does not move. A 24-hour TTL turns the steady
  state into roughly one call per pincode per day.
* **Per-turn cap.** `geocode_max_calls_per_turn` bounds a single message's
  spend no matter how confused the parse is.
* **Circuit breaker.** A provider outage falls back to the stored locality
  centroid, and the proximity evaluator labels the result as coarse rather
  than pretending to precision it does not have.

Nothing here ever returns a tutor's exact coordinates to a caller. The
proximity evaluator turns them into a distance band and the coordinates stay
inside the process.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from tutor_match_meta.cache.base import Cache, CacheKeys, NullCache
from tutor_match_meta.contracts.tutor import GeoPoint
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.security.urls import UrlPolicy, validate

logger = get_logger("geo")

EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance. Arithmetic, not a service call."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a.latitude, a.longitude, b.latitude, b.longitude))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


@runtime_checkable
class GeocoderBackend(Protocol):
    """Raw lookup, no caching or budgeting. Wrapped by `BudgetedGeocoder`."""

    name: str

    async def lookup(self, key: str, granularity: str) -> GeoPoint | None: ...


class DisabledGeocoder:
    """Resolves nothing.

    Not an error state: for online tuition distance is irrelevant, and for home
    tuition the proximity evaluator falls back to locality-name equality and
    says so. `geocoder=disabled` is a legitimate production configuration.
    """

    name = "disabled"

    async def lookup(self, key: str, granularity: str) -> GeoPoint | None:
        return None


class StoredGeocoder:
    """Reads the `geo_point` table the sync job populates.

    This is the primary backend. Every pincode NXTutors serves is geocoded once,
    offline, and read from PostgreSQL thereafter — which is why the matching
    path has no paid dependency at all in steady state.
    """

    name = "stored"

    def __init__(self, sessions: Any, *, schema: str) -> None:
        self._sessions = sessions
        self._table = f"{schema}.geo_point" if schema else "geo_point"

    async def lookup(self, key: str, granularity: str) -> GeoPoint | None:
        from sqlalchemy import text

        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        text(
                            f"SELECT latitude, longitude, granularity, updated_at "  # noqa: S608
                            f"FROM {self._table} WHERE key = :key"
                        ),
                        {"key": key.lower()},
                    )
                ).one_or_none()
        except Exception:
            logger.warning("stored geocode lookup failed")
            return None
        if row is None:
            return None
        return GeoPoint(
            latitude=float(row.latitude),
            longitude=float(row.longitude),
            granularity=str(row.granularity or granularity),
            resolved_at=row.updated_at,
        )


class HttpGeocoder:
    """Paid provider. Only reached on a miss, and only within the turn cap."""

    name = "http"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 3.0,
        url_policy: UrlPolicy | None = None,
        client: Any | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("HttpGeocoder requires a base_url")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._policy = url_policy
        self._client = client
        self.failures = 0

    async def lookup(self, key: str, granularity: str) -> GeoPoint | None:
        import httpx

        # SSRF defence: the URL is built from configuration and validated
        # against the allowlist, and `key` is a query parameter rather than a
        # path segment so it cannot redirect the request elsewhere (§2).
        url = f"{self._base_url}/geocode"
        if self._policy is not None:
            url = validate(url, self._policy)

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.get(
                url,
                params={"q": key, "granularity": granularity},
                headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
                # Never follow a redirect: a redirect is the classic way an
                # allowlisted host becomes a request to somewhere else.
                follow_redirects=False,
            )
        except Exception:
            self.failures += 1
            logger.warning("geocoder unreachable", extra={"tmm_granularity": granularity})
            return None
        finally:
            if self._client is None:
                await client.aclose()

        if response.status_code >= 300:
            self.failures += 1
            logger.warning("geocoder error", extra={"tmm_status": response.status_code})
            return None
        try:
            body = response.json()
            return GeoPoint(
                latitude=float(body["latitude"]),
                longitude=float(body["longitude"]),
                granularity=str(body.get("granularity") or granularity),
                resolved_at=datetime.now(UTC),
            )
        except (ValueError, KeyError, TypeError):
            self.failures += 1
            logger.warning("geocoder returned an unusable body")
            return None


@dataclass(slots=True)
class GeoStats:
    calls: int = 0
    cache_hits: int = 0
    refused_over_budget: int = 0


class BudgetedGeocoder:
    """Cache → backend → cache, under a hard per-turn call ceiling.

    Implements `GeoPort`. The budget is per instance, and one instance is built
    per turn, so a confused parse that mentions six localities still costs at
    most `max_calls_per_turn` lookups.
    """

    def __init__(
        self,
        backend: GeocoderBackend,
        *,
        cache: Cache | None = None,
        ttl_seconds: int = 86_400,
        max_calls_per_turn: int = 1,
    ) -> None:
        self._backend = backend
        self._cache = cache or NullCache()
        self._ttl = ttl_seconds
        self._max_calls = max_calls_per_turn
        self.stats = GeoStats()

    async def resolve(
        self, *, pincode: str | None, locality: str | None, city: str | None
    ) -> GeoPoint | None:
        """Coarsest sufficient answer, cheapest source first.

        Order is pincode → locality → city because that is decreasing
        precision, and the first hit wins. An unknown input returns None rather
        than a city-centre guess: the proximity evaluator would then label a
        guessed distance as real, which is exactly the fabrication this service
        refuses to do.
        """
        for key, granularity in ((pincode, "pincode"), (locality, "locality"), (city, "city")):
            if not key or not key.strip():
                continue
            point = await self._resolve_one(key.strip().lower(), granularity)
            if point is not None:
                return point
        return None

    async def _resolve_one(self, key: str, granularity: str) -> GeoPoint | None:
        cache_key = CacheKeys.geo(f"{granularity}:{key}")
        cached = await self._cache.get(cache_key)
        if cached is not None:
            self.stats.cache_hits += 1
            return _decode(cached)

        if self.stats.calls >= self._max_calls:
            # Refusing is correct. The alternative is an unbounded number of
            # paid lookups driven by whatever the parent happened to type.
            self.stats.refused_over_budget += 1
            logger.info("geocode budget reached for this turn")
            return None

        self.stats.calls += 1
        point = await self._backend.lookup(key, granularity)
        if point is None:
            # Cache the miss briefly too, so a typo is not retried on every
            # turn of the same conversation.
            await self._cache.set(cache_key, "null", ttl_seconds=min(self._ttl, 900))
            return None
        await self._cache.set(cache_key, _encode(point), ttl_seconds=self._ttl)
        return point


def _encode(point: GeoPoint) -> str:
    return json.dumps(
        {"lat": point.latitude, "lon": point.longitude, "g": point.granularity},
        separators=(",", ":"),
    )


def _decode(raw: str) -> GeoPoint | None:
    if raw == "null":
        return None
    try:
        data = json.loads(raw)
        return GeoPoint(
            latitude=float(data["lat"]),
            longitude=float(data["lon"]),
            granularity=str(data.get("g") or "pincode"),
            resolved_at=datetime.now(UTC),
        )
    except (ValueError, KeyError, TypeError):
        return None


def build_geocoder(
    settings: Any,
    *,
    sessions: Any | None = None,
    cache: Cache | None = None,
) -> BudgetedGeocoder:
    """Pick a backend from configuration. Never fails to build.

    A misconfigured geocoder degrades proximity scoring; it must not stop the
    service from starting (see `bootstrap`'s degrade-never-crash rule).
    """
    mode = getattr(settings, "geocoder", "offline")
    backend: GeocoderBackend = DisabledGeocoder()
    if mode == "http" and getattr(settings, "geocoder_base_url", ""):
        from tutor_match_meta.security.urls import build_policy

        try:
            backend = HttpGeocoder(
                base_url=settings.geocoder_base_url,
                api_key=settings.geocoder_api_key.get_secret_value(),
                timeout_seconds=settings.geocoder_timeout_seconds,
                url_policy=build_policy(
                    settings.geocoder_base_url,
                    allow_local=not settings.is_deployed,
                ),
            )
        except Exception:
            logger.warning("http geocoder unavailable; falling back to stored coordinates")
            backend = DisabledGeocoder()
    elif mode == "offline" and sessions is not None:
        backend = StoredGeocoder(sessions, schema=settings.postgres_schema)

    return BudgetedGeocoder(
        backend,
        cache=cache,
        ttl_seconds=getattr(settings, "geocode_cache_ttl_seconds", 86_400),
        max_calls_per_turn=getattr(settings, "geocode_max_calls_per_turn", 1),
    )


__all__ = [
    "EARTH_RADIUS_KM",
    "BudgetedGeocoder",
    "DisabledGeocoder",
    "GeoStats",
    "GeocoderBackend",
    "HttpGeocoder",
    "StoredGeocoder",
    "build_geocoder",
    "haversine_km",
]
