"""EventBridge Scheduler — one-shot future callbacks.

Reminders and expiries are rows plus a scheduled trigger, never an in-process
timer: a Lambda that is about to be frozen cannot hold a `sleep`.

`name` is the idempotency handle. Scheduling the same name twice *replaces* the
schedule rather than adding a second one, which is exactly what a reschedule
needs — and why reminder names embed the demo revision.

Disabled (no role ARN) degrades to a log line rather than an exception. A local
run and a partially-provisioned environment should still complete the lifecycle;
they simply do not fire timers, and `dcc-doctor` reports it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from demo_command_center.observability.logging import get_logger

logger = get_logger("scheduler")

#: EventBridge Scheduler's own limit on a schedule name.
MAX_NAME_CHARS = 64


class EventBridgeScheduler:
    def __init__(
        self,
        *,
        group_name: str,
        role_arn: str,
        region: str,
        target_arn: str = "",
        enabled: bool = True,
        client: Any = None,
    ) -> None:
        self._group = group_name
        self._role = role_arn
        self._region = region
        self._target = target_arn
        self._enabled = enabled and bool(role_arn and target_arn)
        self._client = client

    def _boto(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("scheduler", region_name=self._region)
        return self._client

    async def schedule(self, *, name: str, fire_at: datetime, payload: dict[str, Any]) -> None:
        safe = _safe_name(name)
        if not self._enabled:
            logger.info(
                "scheduler disabled; callback not registered",
                extra={"dcc_schedule": safe, "dcc_fire_at": fire_at.isoformat()},
            )
            return

        client = self._boto()
        request = {
            "Name": safe,
            "GroupName": self._group,
            # `at()` takes UTC without an offset suffix; passing an offset is
            # rejected, and passing local time silently fires at the wrong hour.
            "ScheduleExpression": f"at({fire_at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')})",
            "ScheduleExpressionTimezone": "UTC",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {
                "Arn": self._target,
                "RoleArn": self._role,
                "Input": json.dumps(payload, separators=(",", ":")),
                "RetryPolicy": {"MaximumRetryAttempts": 2},
            },
            # One-shot: the schedule deletes itself after firing, so the group
            # does not accumulate a row per reminder forever.
            "ActionAfterCompletion": "DELETE",
        }
        try:
            client.create_schedule(**request)
        except client.exceptions.ConflictException:
            # Already exists — this is a reschedule. Update, do not duplicate.
            client.update_schedule(**request)

    async def cancel(self, *, name: str) -> None:
        safe = _safe_name(name)
        if not self._enabled:
            return
        client = self._boto()
        try:
            client.delete_schedule(Name=safe, GroupName=self._group)
        except client.exceptions.ResourceNotFoundException:
            # Already gone. Cancelling a schedule that fired is normal.
            logger.info("schedule already absent", extra={"dcc_schedule": safe})


def _safe_name(name: str) -> str:
    """EventBridge accepts `[0-9a-zA-Z-_.]` only, up to 64 characters.

    Truncation is at the *front* — reminder names end with the distinguishing
    part (demo id, revision, label), and chopping the tail would collide two
    different reminders onto one schedule.
    """
    cleaned = "".join(char if char.isalnum() or char in "-_." else "-" for char in name)
    return cleaned[-MAX_NAME_CHARS:]
