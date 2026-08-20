"""Capability 129 — Regional Demo Monitoring.

Rollups, comparisons and underperformance alerting for a regional sub-admin.

Two things this module is careful about, because both are how monitoring becomes
noise nobody reads:

* **Authorization is server-side.** `authorised_regions` comes from the gateway
  and every query is filtered by it here. Filtering in the console would mean
  the API returns every region's data and the browser hides it, which is not
  access control.
* **An alert must clear three independent gates**: minimum sample size, a
  sustained condition across consecutive windows, and a cooldown since the last
  fire. Three no-shows out of four demos in a small region satisfies none of
  them, and alerting on it is how a team learns to ignore the channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from demo_command_center.config.policies import MonitoringPolicy, UnderperformanceRule
from demo_command_center.contracts.common import DemoOutcome
from demo_command_center.domain.demo import Demo
from demo_command_center.observability.logging import get_logger
from demo_command_center.repositories.ports import DemoRepository, OperationsRepository

logger = get_logger("capability.monitoring")


class NotAuthorisedForRegion(Exception):
    def __init__(self, region: str) -> None:
        super().__init__(f"operator is not authorised for region {region}")
        self.region = region


@dataclass(frozen=True, slots=True)
class RegionalMetrics:
    """One window's worth of numbers for one region."""

    region: str
    window_start: datetime
    window_end: datetime
    total: int = 0
    upcoming: int = 0
    completed: int = 0
    cancelled: int = 0
    student_no_shows: int = 0
    tutor_no_shows: int = 0
    rescheduled: int = 0
    converted: int = 0
    quality_sum: float = 0.0
    quality_count: int = 0

    def _rate(self, numerator: int) -> float:
        """Rates are over *held* demos, not over every row.

        Dividing by the total would let a burst of cancellations lower the
        no-show rate, which is exactly backwards.
        """
        held = self.completed + self.student_no_shows + self.tutor_no_shows
        return round(numerator / held, 4) if held else 0.0

    @property
    def student_no_show_rate(self) -> float:
        return self._rate(self.student_no_shows)

    @property
    def tutor_no_show_rate(self) -> float:
        return self._rate(self.tutor_no_shows)

    @property
    def conversion_rate(self) -> float:
        return round(self.converted / self.completed, 4) if self.completed else 0.0

    @property
    def reschedule_rate(self) -> float:
        return round(self.rescheduled / self.total, 4) if self.total else 0.0

    @property
    def mean_quality_score(self) -> float:
        return round(self.quality_sum / self.quality_count, 4) if self.quality_count else 0.0

    @property
    def sample_size(self) -> int:
        return self.completed + self.student_no_shows + self.tutor_no_shows

    def metric(self, name: str) -> float | None:
        return {
            "student_no_show_rate": self.student_no_show_rate,
            "tutor_no_show_rate": self.tutor_no_show_rate,
            "conversion_rate": self.conversion_rate,
            "reschedule_rate": self.reschedule_rate,
            "mean_quality_score": self.mean_quality_score,
            "reminder_failure_rate": None,  # supplied by the message log, not demos
        }.get(name)

    def as_row(self, metric: str) -> dict[str, Any]:
        return {
            "region": self.region,
            "metric": metric,
            "value": self.metric(metric),
            "sample_size": self.sample_size,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Alert:
    region: str
    rule: str
    metric: str
    value: float
    threshold: float
    baseline: float | None
    sample_size: int
    severity: str
    detail: str


class MonitoringCapability:
    def __init__(
        self,
        *,
        policy: MonitoringPolicy,
        demos: DemoRepository,
        operations: OperationsRepository,
    ) -> None:
        self._policy = policy
        self._demos = demos
        self._operations = operations

    # ------------------------------------------------------- authorization
    @staticmethod
    def assert_authorised(region: str, authorised_regions: list[str]) -> None:
        """Server-side region check. Called before every read."""
        if region not in authorised_regions:
            raise NotAuthorisedForRegion(region)

    async def calendar(
        self,
        *,
        region: str,
        authorised_regions: list[str],
        from_at: datetime,
        to_at: datetime,
    ) -> list[Demo]:
        self.assert_authorised(region, authorised_regions)
        return await self._demos.in_window(region=region, from_at=from_at, to_at=to_at)

    # -------------------------------------------------------------- rollups
    async def rollup(
        self,
        *,
        region: str,
        authorised_regions: list[str],
        now: datetime,
        quality_by_demo: dict[str, float] | None = None,
        converted_demo_ids: frozenset[str] = frozenset(),
    ) -> RegionalMetrics:
        """Aggregate one policy window for one region."""
        self.assert_authorised(region, authorised_regions)
        window_start = now - timedelta(hours=self._policy.window_hours)
        demos = await self._demos.in_window(region=region, from_at=window_start, to_at=now)
        quality = quality_by_demo or {}

        counters = {
            "total": 0, "upcoming": 0, "completed": 0, "cancelled": 0,
            "student_no_shows": 0, "tutor_no_shows": 0, "rescheduled": 0, "converted": 0,
        }  # fmt: skip
        quality_sum, quality_count = 0.0, 0

        for demo in demos:
            counters["total"] += 1
            if demo.cancelled:
                counters["cancelled"] += 1
                continue
            if demo.revision > 1:
                counters["rescheduled"] += 1
            if demo.slot is not None and demo.slot.starts_at > now:
                counters["upcoming"] += 1
                continue
            if demo.outcome.student_attended is False:
                counters["student_no_shows"] += 1
            elif demo.outcome.tutor_attended is False:
                counters["tutor_no_shows"] += 1
            elif demo.outcome.outcome is not DemoOutcome.UNKNOWN:
                counters["completed"] += 1
                if demo.demo_id in converted_demo_ids:
                    counters["converted"] += 1
            score = quality.get(demo.demo_id)
            if score is not None:
                quality_sum += score
                quality_count += 1

        return RegionalMetrics(
            region=region,
            window_start=window_start,
            window_end=now,
            quality_sum=quality_sum,
            quality_count=quality_count,
            **counters,
        )

    async def compare(
        self, *, regions: list[str], authorised_regions: list[str], now: datetime
    ) -> dict[str, RegionalMetrics]:
        """Side-by-side rollups. Unauthorised regions are omitted, not errored.

        A comparison view listing three regions where one is off-limits should
        show the two, not fail entirely — but it must not show a placeholder
        that leaks the existence of numbers for the third.
        """
        out: dict[str, RegionalMetrics] = {}
        for region in regions:
            if region not in authorised_regions:
                continue
            out[region] = await self.rollup(
                region=region, authorised_regions=authorised_regions, now=now
            )
        return out

    # --------------------------------------------------------------- alerts
    async def evaluate(
        self,
        *,
        metrics: RegionalMetrics,
        now: datetime,
        extra_metrics: dict[str, float] | None = None,
    ) -> list[Alert]:
        """Every rule that fires, after all three gates."""
        fired: list[Alert] = []
        for rule in self._policy.rules:
            alert = await self._evaluate_rule(rule, metrics, now=now, extra=extra_metrics or {})
            if alert is not None:
                fired.append(alert)
                await self._operations.record_alert(
                    region=metrics.region,
                    rule=rule.name,
                    now=now,
                    payload={
                        "metric": alert.metric,
                        "value": alert.value,
                        "severity": alert.severity,
                        "sample_size": alert.sample_size,
                    },
                )
        return fired

    async def _evaluate_rule(
        self,
        rule: UnderperformanceRule,
        metrics: RegionalMetrics,
        *,
        now: datetime,
        extra: dict[str, float],
    ) -> Alert | None:
        value = metrics.metric(rule.metric)
        if value is None:
            value = extra.get(rule.metric)
        if value is None:
            return None

        # Gate 1: sample floor.
        if metrics.sample_size < rule.min_sample_size:
            return None

        baseline: float | None = None
        threshold = rule.threshold
        if rule.mode == "relative_to_baseline":
            baseline = await self._baseline(metrics.region, rule.metric)
            if baseline is None:
                # No history yet. A first window cannot be "worse than usual".
                return None
            threshold = (
                baseline * (1 + rule.threshold)
                if rule.comparison == "above"
                else baseline * (1 - rule.threshold)
            )

        breached = value > threshold if rule.comparison == "above" else value < threshold
        if not breached:
            return None

        # Gate 2: sustained across consecutive windows.
        if rule.sustained_windows > 1 and not await self._sustained(
            rule, metrics.region, threshold
        ):
            return None

        # Gate 3: cooldown.
        last = await self._operations.alert_fired_at(region=metrics.region, rule=rule.name)
        if last is not None and now - last < timedelta(hours=rule.cooldown_hours):
            return None

        return Alert(
            region=metrics.region,
            rule=rule.name,
            metric=rule.metric,
            value=round(value, 4),
            threshold=round(threshold, 4),
            baseline=None if baseline is None else round(baseline, 4),
            sample_size=metrics.sample_size,
            severity=rule.severity,
            detail=f"{rule.metric} {rule.comparison} {threshold:.4f} (observed {value:.4f})",
        )

    async def _baseline(self, region: str, metric: str) -> float | None:
        rows = await self._operations.rollups(
            region=region, metric=metric, limit=self._policy.baseline_days
        )
        values = [float(row["value"]) for row in rows if row.get("value") is not None]
        return round(sum(values) / len(values), 6) if values else None

    async def _sustained(self, rule: UnderperformanceRule, region: str, threshold: float) -> bool:
        """The condition must hold in every one of the last N windows."""
        rows = await self._operations.rollups(
            region=region, metric=rule.metric, limit=rule.sustained_windows
        )
        if len(rows) < rule.sustained_windows:
            return False
        return all(
            (float(row["value"]) > threshold)
            if rule.comparison == "above"
            else (float(row["value"]) < threshold)
            for row in rows
            if row.get("value") is not None
        )
