"""In-memory analysis, commerce and operations stores.

`InMemoryCommerceRepository.record_event` is the durable half of the
duplicate-payment defence: the provider event id is a set, and a second webhook
carrying the same id returns False without touching the order. The signature
check sheds the forged ones; this sheds the replayed ones.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from demo_command_center.domain.objections import ObjectionAnalysisV1
from demo_command_center.domain.payments import PaymentEvent, PaymentOrder, SubscriptionActivation
from demo_command_center.domain.pricing import DiscountDecision
from demo_command_center.shared.clock import ensure_utc
from demo_command_center.shared.ids import new_ulid


class InMemoryAnalysisRepository:
    def __init__(self) -> None:
        self._objections: dict[str, ObjectionAnalysisV1] = {}
        self._forecasts: dict[str, dict[str, Any]] = {}
        self._quality: list[dict[str, Any]] = []

    async def save_objections(self, analysis: ObjectionAnalysisV1) -> None:
        self._objections[analysis.demo_id] = analysis

    async def load_objections(self, demo_id: str) -> ObjectionAnalysisV1 | None:
        return self._objections.get(demo_id)

    async def save_forecast(self, record: dict[str, Any]) -> None:
        self._forecasts[str(record["demo_id"])] = record

    async def load_forecast(self, demo_id: str) -> dict[str, Any] | None:
        return self._forecasts.get(demo_id)

    async def save_quality(self, record: dict[str, Any]) -> None:
        self._quality.append(record)

    def quality_rows(self) -> list[dict[str, Any]]:
        return list(self._quality)


class InMemoryCommerceRepository:
    def __init__(self) -> None:
        self._decisions: dict[str, DiscountDecision] = {}
        self._offer_log: list[tuple[str, datetime]] = []
        self._orders: dict[str, PaymentOrder] = {}
        self._event_ids: set[str] = set()
        self._events: list[PaymentEvent] = []
        self._activations: dict[str, SubscriptionActivation] = {}
        self._lock = asyncio.Lock()

    async def save_decision(self, decision: DiscountDecision) -> None:
        self._decisions[decision.demo_id] = decision
        if decision.percent > 0 and decision.student_ref:
            self._offer_log.append((decision.student_ref, decision.decided_at))

    async def load_decision(self, demo_id: str) -> DiscountDecision | None:
        return self._decisions.get(demo_id)

    async def offers_since(self, *, student_ref: str | None, since: datetime) -> int:
        if not student_ref:
            return 0
        floor = ensure_utc(since)
        return sum(1 for ref, at in self._offer_log if ref == student_ref and at >= floor)

    async def save_order(self, order: PaymentOrder) -> None:
        self._orders[order.order_ref] = order

    async def load_order(self, order_ref: str) -> PaymentOrder | None:
        return self._orders.get(order_ref)

    async def order_for_conversation(self, conversation_ref: str) -> PaymentOrder | None:
        for order in reversed(list(self._orders.values())):
            if order.conversation_ref == conversation_ref:
                return order
        return None

    async def record_event(self, event: PaymentEvent, *, now: datetime) -> bool:
        async with self._lock:
            if event.provider_event_id in self._event_ids:
                return False
            self._event_ids.add(event.provider_event_id)
            self._events.append(event)
            return True

    async def save_activation(self, activation: SubscriptionActivation) -> None:
        self._activations[activation.order_ref] = activation

    async def activation_for(self, order_ref: str) -> SubscriptionActivation | None:
        return self._activations.get(order_ref)


class InMemoryOperationsRepository:
    def __init__(self) -> None:
        self._rollups: list[dict[str, Any]] = []
        self._alerts: dict[tuple[str, str], datetime] = {}
        self._alert_log: list[dict[str, Any]] = []
        self._cases: list[dict[str, Any]] = []
        self._audit: list[dict[str, Any]] = []

    async def save_rollup(self, record: dict[str, Any]) -> None:
        self._rollups.append(record)

    async def rollups(self, *, region: str, metric: str, limit: int) -> list[dict[str, Any]]:
        matching = [
            row
            for row in self._rollups
            if row.get("region") == region and row.get("metric") == metric
        ]
        # Newest first: the sustained-window check reads the most recent N.
        return list(reversed(matching))[:limit]

    async def alert_fired_at(self, *, region: str, rule: str) -> datetime | None:
        return self._alerts.get((region, rule))

    async def record_alert(
        self, *, region: str, rule: str, now: datetime, payload: dict[str, Any]
    ) -> None:
        self._alerts[(region, rule)] = ensure_utc(now)
        self._alert_log.append({"region": region, "rule": rule, **payload})

    async def open_human_case(self, record: dict[str, Any]) -> str:
        case_id = f"hc_{new_ulid()}"
        self._cases.append({"case_id": case_id, **record})
        return case_id

    async def audit(self, record: dict[str, Any]) -> None:
        self._audit.append(record)

    def alerts(self) -> list[dict[str, Any]]:
        return list(self._alert_log)

    def cases(self) -> list[dict[str, Any]]:
        return list(self._cases)

    def audit_trail(self) -> list[dict[str, Any]]:
        return list(self._audit)
