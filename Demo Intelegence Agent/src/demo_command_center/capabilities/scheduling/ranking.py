"""Deterministic slot ranking.

Pure arithmetic over four signals, with fixed weights. No model is involved:
"which of these three times is best" is a preference-matching problem with an
obvious answer, and making it non-deterministic would mean the same parent asked
twice gets different options and cannot tell why.

Weights are constants here rather than policy YAML deliberately — they encode
*how* preference matching works, not a business threshold someone should tune
without a code review. The business knobs (booking horizon, hold TTL, quiet
hours) are all in settings or policy.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from demo_command_center.domain.slots import SlotProposal, TimeSlot, resolve_timezone

#: Sum to 1.0. Asserted by the unit tests so a future edit cannot silently
#: rescale every score.
W_PREFERENCE = 0.50
W_SOONER = 0.25
W_DAYPART = 0.15
W_WEEKDAY = 0.10

#: Past this distance from the requested time, preference contributes nothing.
PREFERENCE_DECAY = timedelta(days=3)
#: Past this, "sooner" stops distinguishing slots.
SOONER_HORIZON = timedelta(days=14)

#: After-school hours in the demo's local zone. A tuition demo at 11am on a
#: Wednesday is technically available and practically useless.
PREFERRED_HOURS: frozenset[int] = frozenset({16, 17, 18, 19, 20})


def rank_slots(
    available: list[TimeSlot],
    *,
    preferred: TimeSlot | None,
    timezone: str,
    duration_minutes: int,
    now: datetime,
    limit: int = 3,
) -> tuple[SlotProposal, ...]:
    """Score, sort and take the top `limit`. Ties break on the earlier slot."""
    scored: list[tuple[float, tuple[str, ...], TimeSlot]] = []
    for slot in available:
        normalised = TimeSlot(
            starts_at=slot.starts_at, duration_minutes=duration_minutes, timezone=timezone
        )
        score, codes = _score(normalised, preferred=preferred, now=now, timezone=timezone)
        scored.append((score, codes, normalised))

    scored.sort(key=lambda row: (-row[0], row[2].starts_at))
    return tuple(
        SlotProposal(slot=slot, rank=index, score=round(score, 4), reason_codes=codes)
        for index, (score, codes, slot) in enumerate(scored[:limit], start=1)
    )


def _score(
    slot: TimeSlot, *, preferred: TimeSlot | None, now: datetime, timezone: str
) -> tuple[float, tuple[str, ...]]:
    codes: list[str] = []

    if preferred is None:
        # With nothing to match against, preference is neutral rather than zero.
        # Zero would let "sooner" dominate completely and always propose the
        # next three consecutive slots, which is a worse set of options.
        preference = 0.5
    else:
        distance = abs(slot.starts_at - preferred.starts_at)
        preference = max(0.0, 1.0 - distance / PREFERENCE_DECAY)
        if distance <= timedelta(minutes=30):
            codes.append("matches_requested_time")
        elif distance <= timedelta(hours=4):
            codes.append("near_requested_time")

    lead = slot.starts_at - now
    sooner = max(0.0, 1.0 - lead / SOONER_HORIZON)
    if lead <= timedelta(days=1):
        codes.append("available_tomorrow")

    local_hour = slot.starts_at.astimezone(resolve_timezone(timezone)).hour
    daypart = 1.0 if local_hour in PREFERRED_HOURS else 0.35
    if local_hour in PREFERRED_HOURS:
        codes.append("after_school_hours")

    weekday = 1.0 if slot.starts_at.astimezone(resolve_timezone(timezone)).weekday() < 5 else 0.7

    total = (
        W_PREFERENCE * preference + W_SOONER * sooner + W_DAYPART * daypart + W_WEEKDAY * weekday
    )
    return min(1.0, max(0.0, total)), tuple(codes)
