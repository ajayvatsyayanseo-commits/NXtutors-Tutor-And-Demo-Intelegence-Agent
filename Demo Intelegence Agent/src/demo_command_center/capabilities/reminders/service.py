"""Capability 026 — Demo Reminders.

Builds the ladder from versioned policy, applies quiet hours, and replaces
rather than appends on reschedule. It plans; it does not send. Every reminder
becomes an `OutboundMessage` that the single outbound boundary may still refuse
(ownership, opt-out, rate limit) — a reminder being *due* is not the same as a
reminder being *allowed*.

The no-show risk score is deterministic and separate from the conversion
forecast. They answer different questions: "will they buy" and "will they turn
up" correlate but are not the same, and using conversion probability to decide
whether to send a T-15m nudge would skip exactly the low-intent parents who most
need one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from demo_command_center.config.policies import ReminderPolicy
from demo_command_center.contracts.common import Party
from demo_command_center.domain.demo import Demo
from demo_command_center.domain.messages import (
    MessageKind,
    OutboundMessage,
    TemplateBinding,
)
from demo_command_center.domain.reminders import ScheduledReminder, defer_past_quiet_hours
from demo_command_center.observability.logging import get_logger
from demo_command_center.shared.clock import Clock, ensure_utc
from demo_command_center.shared.ids import prefixed

logger = get_logger("capability.reminders")


@dataclass(frozen=True, slots=True)
class NoShowRisk:
    score: float
    band: str
    signals: tuple[str, ...]

    @property
    def high(self) -> bool:
        return self.band == "high"


class ReminderCapability:
    def __init__(self, policy: ReminderPolicy, clock: Clock) -> None:
        self._policy = policy
        self._clock = clock

    # -------------------------------------------------------------- planning
    def plan(self, demo: Demo) -> list[ScheduledReminder]:
        """The full ladder for one demo revision.

        Offsets already in the past are skipped rather than fired immediately —
        a demo booked three hours out should not trigger its T-24h reminder the
        instant it is created.
        """
        if demo.slot is None or demo.cancelled:
            return []

        now = self._clock.now()
        starts_at = demo.slot.starts_at
        timezone = demo.slot.timezone
        planned: list[ScheduledReminder] = []

        for offset in self._policy.offsets:
            fire_at = starts_at - timedelta(minutes=offset.minutes_before)
            if fire_at <= now:
                continue

            adjusted = defer_past_quiet_hours(
                fire_at,
                timezone=timezone,
                start_hour=self._policy.quiet_hours_start,
                end_hour=self._policy.quiet_hours_end,
                defer_to_hour=self._policy.quiet_hours_defer_to_hour,
                demo_starts_at=starts_at,
            )
            if adjusted is None:
                logger.info(
                    "reminder dropped: deferring past quiet hours would land after the demo",
                    extra={"dcc_label": offset.label},
                )
                continue

            for party in self._audiences(offset.audience):
                recipient = demo.attendee(party)
                if recipient is None:
                    continue
                planned.append(
                    ScheduledReminder(
                        reminder_id=prefixed("rmd", now=now),
                        demo_id=demo.demo_id,
                        conversation_ref=demo.conversation_ref,
                        demo_revision=demo.revision,
                        label=offset.label,
                        audience=party,
                        recipient_ref=recipient.ref,
                        template=offset.template,
                        channel=offset.channel,
                        fire_at=adjusted,
                        demo_starts_at=starts_at,
                    )
                )

        # The per-demo ceiling applies across the whole ladder, not per offset,
        # so a both-audience policy cannot quietly double it.
        return planned[: self._policy.max_reminders_per_demo]

    @staticmethod
    def _audiences(audience: str) -> tuple[Party, ...]:
        if audience == "both":
            return (Party.STUDENT, Party.TUTOR)
        return (Party.STUDENT,) if audience == "student" else (Party.TUTOR,)

    # ------------------------------------------------------------- rendering
    def to_message(
        self, reminder: ScheduledReminder, *, demo: Demo, variables: tuple[str, ...]
    ) -> OutboundMessage:
        """Reminders are always template sends — they are outside the window.

        `MessageKind.REMINDER` is in `TEMPLATE_REQUIRED`, so `OutboundMessage`
        refuses to construct without a binding. The type system carries the rule.
        """
        return OutboundMessage(
            conversation_ref=reminder.conversation_ref,
            recipient_ref=reminder.recipient_ref,
            audience=reminder.audience,
            kind=MessageKind.REMINDER,
            language=demo.language,
            template=TemplateBinding(
                name=reminder.template,
                language=demo.language.value,
                variables=variables,
            ),
            idempotency_key=reminder.idempotency_key,
            demo_id=demo.demo_id,
            # A reminder that missed its moment is dropped by the boundary
            # rather than delivered late.
            expires_at=min(reminder.fire_at + timedelta(minutes=20), demo.slot.starts_at)
            if demo.slot
            else None,
            created_at=self._clock.now(),
        )

    # ------------------------------------------------------------------ risk
    def no_show_risk(
        self,
        *,
        demo: Demo,
        reminders_sent: int,
        reminders_acknowledged: int,
        reschedule_count: int,
        minutes_since_last_inbound: float | None,
    ) -> NoShowRisk:
        """Deterministic risk from behavioural signals only.

        No model, and no conversion probability. Each signal is a documented
        additive weight so an operator asking "why was this flagged" gets the
        list back rather than a number.
        """
        score = 0.0
        signals: list[str] = []

        if reminders_sent and reminders_acknowledged == 0:
            score += 0.35
            signals.append("no_reminder_acknowledged")
        if reschedule_count >= 2:
            score += 0.25
            signals.append("repeated_reschedules")
        elif reschedule_count == 1:
            score += 0.10
            signals.append("rescheduled_once")
        if minutes_since_last_inbound is not None:
            if minutes_since_last_inbound > 60 * 48:
                score += 0.30
                signals.append("silent_48h")
            elif minutes_since_last_inbound > self._policy.silence_escalation_minutes:
                score += 0.15
                signals.append("silent_since_last_reminder")
        if demo.slot is not None:
            lead = demo.slot.starts_at - ensure_utc(self._clock.now())
            # A demo booked far out is more likely to be forgotten.
            if lead > timedelta(days=7):
                score += 0.10
                signals.append("booked_far_ahead")

        score = min(1.0, round(score, 3))
        threshold = self._policy.no_show_risk_escalation_threshold
        band = "high" if score >= threshold else ("medium" if score >= threshold / 2 else "low")
        return NoShowRisk(score=score, band=band, signals=tuple(signals))

    def should_escalate(self, risk: NoShowRisk) -> bool:
        return risk.score >= self._policy.no_show_risk_escalation_threshold

    def daily_cap_reached(self, sends_today: int) -> bool:
        return sends_today >= self._policy.max_reminders_per_identity_per_day

    def obsolete(self, reminder: ScheduledReminder, demo: Demo, *, now: datetime) -> str | None:
        """Why this reminder must not go out. None means it may."""
        if demo.cancelled:
            return "demo_cancelled"
        if reminder.obsolete_for(demo.revision):
            return "superseded_by_reschedule"
        if reminder.overdue(now=now):
            return "reminder_overdue"
        if demo.slot is not None and ensure_utc(now) >= demo.slot.starts_at:
            return "demo_already_started"
        return None
