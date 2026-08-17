"""Cities, localities, pincodes and distance.

City canonicalisation mirrors Laravel's `CityNormalizer` (Gurgaon → Gurugram)
because the projection stores whatever `register.city` holds and the two must
agree or a Gurgaon parent sees zero Gurugram tutors.

Distance is haversine on coordinates this service derived itself
(docs/assumptions.md A5) — the website stores no lat/lng, and the tutor's private
street address is never geocoded or transmitted.
"""

from __future__ import annotations

import math
import re
from enum import StrEnum

from tutor_match_meta.contracts.tutor import GeoPoint
from tutor_match_meta.domain.text import normalize_key, tokens

EARTH_RADIUS_KM = 6371.0088

_CITY_ALIASES: dict[str, str] = {
    "gurgaon": "Gurugram",
    "gurugram": "Gurugram",
    "ggn": "Gurugram",
    "bangalore": "Bengaluru",
    "bengaluru": "Bengaluru",
    "blr": "Bengaluru",
    "newdelhi": "Delhi",
    "delhi": "Delhi",
    "ncr": "Delhi",
    "delhincr": "Delhi",
    "bombay": "Mumbai",
    "mumbai": "Mumbai",
    "calcutta": "Kolkata",
    "kolkata": "Kolkata",
    "madras": "Chennai",
    "chennai": "Chennai",
    "noida": "Noida",
    "greaternoida": "Greater Noida",
    "ghaziabad": "Ghaziabad",
    "faridabad": "Faridabad",
    "hyderabad": "Hyderabad",
    "pune": "Pune",
    "ahmedabad": "Ahmedabad",
    "jaipur": "Jaipur",
    "lucknow": "Lucknow",
    "chandigarh": "Chandigarh",
}


class CityTier(StrEnum):
    """Drives the default travel radius (docs/assumptions.md A6)."""

    METRO = "metro"
    TIER2 = "tier2"
    DEFAULT = "default"


_METRO: frozenset[str] = frozenset(
    {"Delhi", "Mumbai", "Bengaluru", "Chennai", "Kolkata", "Hyderabad", "Gurugram", "Noida", "Pune"}
)
_TIER2: frozenset[str] = frozenset(
    {"Ahmedabad", "Jaipur", "Lucknow", "Chandigarh", "Ghaziabad", "Faridabad", "Greater Noida"}
)

_PINCODE = re.compile(r"\b([1-9]\d{5})\b")
#: 'sector 57', 'sec-57', 'dlf phase 3', 'hsr layout'
_SECTOR = re.compile(r"\b(?:sector|sec)\s*[-.]?\s*(\d{1,3}[a-z]?)\b", re.IGNORECASE)
_PHASE = re.compile(r"\b(?:phase)\s*[-.]?\s*(\d{1,2}|[ivx]{1,4})\b", re.IGNORECASE)


def normalize_city(city: str | None) -> str | None:
    if not city or not city.strip():
        return None
    canonical = _CITY_ALIASES.get(normalize_key(city))
    if canonical:
        return canonical
    return " ".join(word.capitalize() for word in city.split())


def city_aliases(city: str) -> list[str]:
    """Every lowercase spelling that means the same city, for SQL `IN` filters."""
    canonical = normalize_city(city)
    if not canonical:
        return []
    aliases = {canonical.lower()}
    aliases.update(alias for alias, value in _CITY_ALIASES.items() if value == canonical)
    return sorted(aliases)


def same_city(left: str | None, right: str | None) -> bool:
    a, b = normalize_city(left), normalize_city(right)
    return a is not None and a == b


def city_tier(city: str | None) -> CityTier:
    canonical = normalize_city(city)
    if canonical in _METRO:
        return CityTier.METRO
    if canonical in _TIER2:
        return CityTier.TIER2
    return CityTier.DEFAULT


def extract_pincode(text: str) -> str | None:
    """A 6-digit Indian pincode, if the message contains exactly one.

    Two or more candidates are ambiguous (a phone fragment, a fee, a year), and
    a wrong pincode silently relocates the whole search — so we return nothing
    and let Stage B ask.
    """
    found = _PINCODE.findall(text)
    unique = list(dict.fromkeys(found))
    return unique[0] if len(unique) == 1 else None


def extract_city(text: str) -> str | None:
    joined = "".join(tokens(text))
    for alias, canonical in _CITY_ALIASES.items():
        if alias in joined:
            return canonical
    return None


def extract_locality(text: str) -> str | None:
    """A locality label like 'Sector 57' or 'Phase 3'.

    Only recognises structured forms. Free-form colony names are left to the
    locality lookup against `city_area_list_managment`, because guessing them
    from arbitrary words produces confident nonsense.
    """
    sector = _SECTOR.search(text)
    if sector:
        return f"Sector {sector.group(1).upper()}"
    phase = _PHASE.search(text)
    if phase:
        return f"Phase {phase.group(1).upper()}"
    return None


def normalize_locality(locality: str | None) -> str | None:
    if not locality or not locality.strip():
        return None
    structured = extract_locality(locality)
    return structured or " ".join(word.capitalize() for word in locality.split())


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance in km."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a.latitude, a.longitude, b.latitude, b.longitude))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


#: Straight-line km inflated to approximate road distance in Indian cities.
#: Deliberately crude and named as such: a routing API would be more accurate but
#: would put a per-candidate network call on the WhatsApp path.
ROAD_DETOUR_FACTOR = 1.35

#: Average urban travel speed, km/h. Used only for a coarse feasibility label.
URBAN_SPEED_KMPH = 18.0


def estimated_travel_minutes(straight_line_km: float) -> int:
    road_km = straight_line_km * ROAD_DETOUR_FACTOR
    return max(1, round(road_km / URBAN_SPEED_KMPH * 60))
