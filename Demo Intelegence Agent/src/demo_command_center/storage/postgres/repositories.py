"""PostgreSQL repositories over asyncpg. All ten aggregates.

The same protocols and the same observable behaviour as the in-memory versions,
including the concurrency contract. Where the in-memory store takes an
`asyncio.Lock`, this one relies on the database:

* `save_transition` is `UPDATE … WHERE version = $expected`; a zero rowcount is
  `ConcurrencyConflict`. Optimistic locking with no read gap at all.
* `place_hold` relies on the partial unique index on `conflict_key`; a unique
  violation becomes `SlotConflict`. Two concurrent bookings cannot both win
  however the reads interleave.
* `claim_send`, `record_event` and `claim` use `ON CONFLICT DO NOTHING` and
  report whether the row was actually inserted. One winner, decided by Postgres.

No statement qualifies a table name — `search_path` is set per connection in
`pool.py`, which is also why no runtime value ever reaches SQL by
interpolation. Every value is a numbered placeholder.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from demo_command_center.contracts.ownership import Owner, Ownership, unowned
from demo_command_center.contracts.ports import ProviderRejected
from demo_command_center.contracts.tutor_match import TutorCandidateV1
from demo_command_center.domain.demo import Demo, DemoRequest
from demo_command_center.domain.messages import OutboundMessage, SendResult
from demo_command_center.domain.objections import ObjectionAnalysisV1
from demo_command_center.domain.payments import (
    PaymentEvent,
    PaymentOrder,
    SubscriptionActivation,
)
from demo_command_center.domain.pricing import DiscountDecision
from demo_command_center.domain.reminders import ReminderStatus, ScheduledReminder
from demo_command_center.domain.slots import HoldStatus, SlotConflict, SlotHold, TimeSlot
from demo_command_center.observability.logging import get_logger
from demo_command_center.shared.clock import ensure_utc
from demo_command_center.shared.ids import new_ulid
from demo_command_center.state.machine import (
    ConcurrencyConflict,
    StateSnapshot,
    TransitionResult,
)
from demo_command_center.state.states import DemoState
from demo_command_center.storage.postgres.pool import UNIQUE_VIOLATION, PostgresPool

logger = get_logger("storage.postgres.repositories")


def _rowcount(tag: str) -> int:
    """asyncpg returns a command tag like `UPDATE 1`. The trailing count is the
    only reliable way to know whether an optimistic update actually applied."""
    parts = tag.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


def _json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default if default is not None else {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default if default is not None else {}
    return value


# ============================================================== conversations


class PgConversationRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._db = pool

    async def load(self, conversation_ref: str) -> StateSnapshot:
        row = await self._db.fetchrow(
            "SELECT state, version, demo_id, updated_at, facts "
            "FROM dcc_conversation_state WHERE conversation_ref = $1",
            conversation_ref,
        )
        if row is None:
            return StateSnapshot(conversation_ref=conversation_ref, state=DemoState.NEW, version=0)
        return StateSnapshot(
            conversation_ref=conversation_ref,
            state=DemoState(row["state"]),
            version=int(row["version"]),
            demo_id=row.get("demo_id"),
            updated_at=row.get("updated_at"),
            facts=_json(row.get("facts")),
        )

    async def save_transition(
        self, result: TransitionResult, *, now: datetime, facts: dict[str, Any] | None = None
    ) -> StateSnapshot:
        moment = ensure_utc(now)
        merged = facts or {}
        payload = json.dumps(merged, default=str)

        async with self._db.transaction() as connection:
            if result.expected_version == 0:
                inserted = await connection.fetchrow(
                    "INSERT INTO dcc_conversation_state "
                    "(conversation_ref, state, version, demo_id, facts, created_at, updated_at) "
                    "VALUES ($1, $2, 1, $3, $4::jsonb, $5, $5) "
                    # If a row already exists another worker created it and this
                    # attempt lost the race — never an upsert.
                    "ON CONFLICT (conversation_ref) DO NOTHING RETURNING version",
                    result.conversation_ref,
                    result.to_state.value,
                    merged.get("demo_id"),
                    payload,
                    moment,
                )
            else:
                inserted = await connection.fetchrow(
                    "UPDATE dcc_conversation_state SET state = $2, version = version + 1, "
                    "demo_id = COALESCE($3, demo_id), facts = facts || $4::jsonb, "
                    "updated_at = $5 "
                    "WHERE conversation_ref = $1 AND version = $6 RETURNING version",
                    result.conversation_ref,
                    result.to_state.value,
                    merged.get("demo_id"),
                    payload,
                    moment,
                    result.expected_version,
                )

            if inserted is None:
                current = await self.load(result.conversation_ref)
                raise ConcurrencyConflict(
                    result.conversation_ref,
                    expected=result.expected_version,
                    actual=current.version,
                )
            version = int(inserted["version"])

            # Same transaction as the state change: a transition without its
            # audit row is a state we cannot explain later.
            await connection.execute(
                "INSERT INTO dcc_state_transitions "
                "(transition_id, conversation_ref, from_state, to_state, trigger, actor, "
                " command, reason, version, occurred_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                new_ulid(now=moment),
                result.conversation_ref,
                result.from_state.value,
                result.to_state.value,
                result.trigger.value,
                result.actor.value,
                result.command.value,
                result.reason,
                version,
                moment,
            )

        return StateSnapshot(
            conversation_ref=result.conversation_ref,
            state=result.to_state,
            version=version,
            demo_id=merged.get("demo_id"),
            updated_at=moment,
            facts=merged,
        )

    async def history(self, conversation_ref: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._db.fetch(
            "SELECT from_state, to_state, trigger, actor, command, reason, version, occurred_at "
            "FROM dcc_state_transitions WHERE conversation_ref = $1 "
            "ORDER BY occurred_at DESC LIMIT $2",
            conversation_ref,
            limit,
        )

    async def load_ownership(self, conversation_ref: str, *, now: datetime) -> Ownership:
        row = await self._db.fetchrow(
            "SELECT owner, since, lease_expires_at, previous "
            "FROM dcc_conversations WHERE conversation_ref = $1",
            conversation_ref,
        )
        if row is None:
            return unowned(conversation_ref, now=ensure_utc(now))
        return Ownership(
            conversation_ref=conversation_ref,
            owner=Owner(row["owner"]),
            since=row["since"],
            lease_expires_at=row.get("lease_expires_at"),
            previous=tuple(Owner(value) for value in _json(row.get("previous"), [])),
        )

    async def save_ownership(self, ownership: Ownership) -> None:
        await self._db.execute(
            "INSERT INTO dcc_conversations "
            "(conversation_ref, owner, since, lease_expires_at, previous) "
            "VALUES ($1, $2, $3, $4, $5::jsonb) "
            "ON CONFLICT (conversation_ref) DO UPDATE SET owner = EXCLUDED.owner, "
            "since = EXCLUDED.since, lease_expires_at = EXCLUDED.lease_expires_at, "
            "previous = EXCLUDED.previous",
            ownership.conversation_ref,
            ownership.owner.value,
            ownership.since,
            ownership.lease_expires_at,
            json.dumps([owner.value for owner in ownership.previous]),
        )

    async def touch_inbound(self, conversation_ref: str, *, at: datetime) -> None:
        await self._db.execute(
            "INSERT INTO dcc_conversations (conversation_ref, owner, since, last_inbound_at) "
            "VALUES ($1, 'released', $2, $2) "
            "ON CONFLICT (conversation_ref) DO UPDATE SET last_inbound_at = $2",
            conversation_ref,
            ensure_utc(at),
        )

    async def last_inbound_at(self, conversation_ref: str) -> datetime | None:
        row = await self._db.fetchrow(
            "SELECT last_inbound_at FROM dcc_conversations WHERE conversation_ref = $1",
            conversation_ref,
        )
        return row.get("last_inbound_at") if row else None


# ================================================================ idempotency


class PgIdempotencyRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._db = pool

    async def claim(self, key: str, *, scope: str, now: datetime, ttl_seconds: int) -> bool:
        """One winner, decided by the unique index rather than by a read."""
        row = await self._db.fetchrow(
            # Every placeholder is cast explicitly. Without the casts Postgres
            # cannot deduce `$3`: it appears both as a timestamp column value
            # and as the left operand of `$3 + interval`, and because
            # `interval + interval` is itself valid the planner reports
            # "inconsistent types deduced for parameter $3: interval versus
            # timestamp with time zone" and the statement never runs.
            #
            # This failed only against a real database, so the unit suite could
            # not see it — `storage/postgres/*` has no in-process double.
            "INSERT INTO dcc_idempotency_keys (scope, idempotency_key, claimed_at, expires_at) "
            "VALUES ($1, $2, $3::timestamptz, "
            "        $3::timestamptz + make_interval(secs => $4::double precision)) "
            # An expired claim is re-claimable: a redelivery weeks later is a
            # new event, not something to drop against a key nobody cleaned up.
            "ON CONFLICT (scope, idempotency_key) DO UPDATE "
            "SET claimed_at = $3::timestamptz, "
            "    expires_at = $3::timestamptz + make_interval(secs => $4::double precision) "
            "WHERE dcc_idempotency_keys.expires_at < $3::timestamptz "
            "RETURNING idempotency_key",
            scope,
            key,
            ensure_utc(now),
            ttl_seconds,
        )
        return row is not None

    async def result_for(self, key: str, *, scope: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT result FROM dcc_idempotency_keys WHERE scope = $1 AND idempotency_key = $2",
            scope,
            key,
        )
        return _json(row.get("result")) if row and row.get("result") else None

    async def record_result(self, key: str, *, scope: str, result: dict[str, Any]) -> None:
        await self._db.execute(
            "UPDATE dcc_idempotency_keys SET result = $3::jsonb "
            "WHERE scope = $1 AND idempotency_key = $2",
            scope,
            key,
            json.dumps(result, default=str),
        )


# ======================================================================= slots


class PgSlotRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._db = pool

    async def place_hold(self, hold: SlotHold) -> SlotHold:
        """The partial unique index is the exclusion. A conflict is a lost race."""
        try:
            row = await self._db.fetchrow(
                "INSERT INTO dcc_slot_holds "
                "(hold_id, conversation_ref, tutor_ref, conflict_key, starts_at, "
                " duration_minutes, timezone, mode, status, created_at, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'active', $9, $10) "
                "ON CONFLICT (conflict_key) WHERE status = 'active' DO NOTHING "
                "RETURNING hold_id",
                hold.hold_id,
                hold.conversation_ref,
                hold.tutor_ref,
                hold.conflict_key,
                hold.slot.starts_at,
                hold.slot.duration_minutes,
                hold.slot.timezone,
                hold.mode.value,
                hold.created_at,
                hold.expires_at,
            )
        except ProviderRejected as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise SlotConflict(hold.tutor_ref, hold.slot.starts_at) from exc
            raise

        if row is None:
            existing = await self._db.fetchrow(
                "SELECT hold_id FROM dcc_slot_holds WHERE conflict_key = $1 AND status = 'active'",
                hold.conflict_key,
            )
            raise SlotConflict(
                hold.tutor_ref,
                hold.slot.starts_at,
                existing_hold_id=str(existing["hold_id"]) if existing else "",
            )
        return hold

    async def load_hold(self, hold_id: str) -> SlotHold | None:
        row = await self._db.fetchrow("SELECT * FROM dcc_slot_holds WHERE hold_id = $1", hold_id)
        return _hold_from_row(row) if row else None

    async def active_hold_for(self, conversation_ref: str, *, now: datetime) -> SlotHold | None:
        row = await self._db.fetchrow(
            "SELECT * FROM dcc_slot_holds WHERE conversation_ref = $1 "
            "AND status = 'active' AND expires_at > $2 ORDER BY created_at DESC LIMIT 1",
            conversation_ref,
            ensure_utc(now),
        )
        return _hold_from_row(row) if row else None

    async def release(self, hold_id: str, *, now: datetime) -> None:
        await self._set_status(hold_id, "released", now)

    async def confirm(self, hold_id: str, *, now: datetime) -> None:
        await self._set_status(hold_id, "confirmed", now)

    async def _set_status(self, hold_id: str, status: str, now: datetime) -> None:
        await self._db.execute(
            "UPDATE dcc_slot_holds SET status = $2, resolved_at = $3 "
            "WHERE hold_id = $1 AND status = 'active'",
            hold_id,
            status,
            ensure_utc(now),
        )

    async def expire_due(self, *, now: datetime) -> list[SlotHold]:
        rows = await self._db.fetch(
            "UPDATE dcc_slot_holds SET status = 'expired', resolved_at = $1 "
            "WHERE status = 'active' AND expires_at <= $1 RETURNING *",
            ensure_utc(now),
        )
        return [hold for hold in (_hold_from_row(row) for row in rows) if hold is not None]


def _hold_from_row(row: dict[str, Any] | None) -> SlotHold | None:
    if row is None:
        return None
    return SlotHold(
        hold_id=row["hold_id"],
        conversation_ref=row["conversation_ref"],
        tutor_ref=row["tutor_ref"],
        slot=TimeSlot(
            starts_at=row["starts_at"],
            duration_minutes=int(row["duration_minutes"]),
            timezone=row["timezone"],
        ),
        mode=row["mode"],
        status=HoldStatus(row["status"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


# ======================================================================= demos


class PgDemoRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._db = pool

    async def save_request(self, request: DemoRequest) -> None:
        async with self._db.transaction() as connection:
            await connection.execute(
                "INSERT INTO dcc_demo_requests "
                "(request_id, conversation_ref, student_ref, region, language, "
                " match_session_id, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (request_id) DO UPDATE SET "
                "match_session_id = EXCLUDED.match_session_id",
                request.request_id,
                request.conversation_ref,
                request.student_ref,
                request.region,
                request.language.value,
                request.match_session_id,
                request.created_at,
            )
            requirement = request.requirement
            await connection.execute(
                "INSERT INTO dcc_demo_requirements "
                "(request_id, service, board, student_class, subject, mode, region, "
                " locality, timezone, availability_note, special_requirements, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now()) "
                "ON CONFLICT (request_id) DO UPDATE SET "
                "service = EXCLUDED.service, board = EXCLUDED.board, "
                "student_class = EXCLUDED.student_class, subject = EXCLUDED.subject, "
                "mode = EXCLUDED.mode, region = EXCLUDED.region, "
                "locality = EXCLUDED.locality, timezone = EXCLUDED.timezone, "
                "availability_note = EXCLUDED.availability_note, "
                "special_requirements = EXCLUDED.special_requirements, updated_at = now()",
                request.request_id,
                requirement.service,
                requirement.board,
                requirement.student_class,
                requirement.subject,
                requirement.mode.value if requirement.mode else None,
                requirement.region,
                requirement.locality,
                requirement.timezone,
                requirement.availability_note,
                requirement.special_requirements,
            )

    async def load_request(self, request_id: str) -> DemoRequest | None:
        row = await self._db.fetchrow(
            "SELECT r.*, q.service, q.board, q.student_class, q.subject, q.mode, "
            "       q.region AS req_region, q.locality, q.timezone, q.availability_note, "
            "       q.special_requirements "
            "FROM dcc_demo_requests r LEFT JOIN dcc_demo_requirements q USING (request_id) "
            "WHERE r.request_id = $1",
            request_id,
        )
        return _request_from_row(row)

    async def request_for_conversation(self, conversation_ref: str) -> DemoRequest | None:
        row = await self._db.fetchrow(
            "SELECT r.*, q.service, q.board, q.student_class, q.subject, q.mode, "
            "       q.region AS req_region, q.locality, q.timezone, q.availability_note, "
            "       q.special_requirements "
            "FROM dcc_demo_requests r LEFT JOIN dcc_demo_requirements q USING (request_id) "
            "WHERE r.conversation_ref = $1 ORDER BY r.created_at DESC LIMIT 1",
            conversation_ref,
        )
        return _request_from_row(row)

    async def save(self, demo: Demo) -> None:
        async with self._db.transaction() as connection:
            await connection.execute(
                "INSERT INTO dcc_demos "
                "(demo_id, conversation_ref, request_id, student_ref, tutor_ref, region, "
                " mode, language, starts_at, duration_minutes, timezone, calendar_event_id, "
                " meet_url, location_label, revision, created_at, updated_at, "
                " cancelled_at, cancellation_reason) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,"
                "$11,$12,$13,$14,$15,$16,$17,$18,$19) "
                "ON CONFLICT (demo_id) DO UPDATE SET "
                "tutor_ref = EXCLUDED.tutor_ref, starts_at = EXCLUDED.starts_at, "
                "duration_minutes = EXCLUDED.duration_minutes, timezone = EXCLUDED.timezone, "
                "calendar_event_id = EXCLUDED.calendar_event_id, meet_url = EXCLUDED.meet_url, "
                "location_label = EXCLUDED.location_label, revision = EXCLUDED.revision, "
                "updated_at = EXCLUDED.updated_at, cancelled_at = EXCLUDED.cancelled_at, "
                "cancellation_reason = EXCLUDED.cancellation_reason",
                demo.demo_id,
                demo.conversation_ref,
                demo.request_id,
                demo.student_ref,
                demo.tutor_ref,
                demo.region,
                demo.mode.value,
                demo.language.value,
                demo.slot.starts_at if demo.slot else None,
                demo.slot.duration_minutes if demo.slot else 45,
                demo.slot.timezone if demo.slot else "Asia/Kolkata",
                demo.calendar_event_id,
                demo.meet_url,
                demo.location_label,
                demo.revision,
                demo.created_at,
                demo.updated_at,
                demo.cancelled_at,
                demo.cancellation_reason,
            )
            for person in demo.attendees:
                await connection.execute(
                    "INSERT INTO dcc_demo_attendees "
                    "(demo_id, party, participant_ref, display_name, invite_consent) "
                    "VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (demo_id, party) DO UPDATE SET "
                    "participant_ref = EXCLUDED.participant_ref, "
                    "display_name = EXCLUDED.display_name, "
                    "invite_consent = EXCLUDED.invite_consent",
                    demo.demo_id,
                    person.party.value,
                    person.ref,
                    person.display_name,
                    person.invite_consent,
                )
            outcome = demo.outcome
            if outcome.evidence_source.value != "none":
                await connection.execute(
                    "INSERT INTO dcc_demo_outcomes "
                    "(demo_id, outcome, student_attended, tutor_attended, evidence_source, "
                    " duration_minutes, recorded_by, notes, recorded_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
                    "ON CONFLICT (demo_id) DO UPDATE SET outcome = EXCLUDED.outcome, "
                    "student_attended = EXCLUDED.student_attended, "
                    "tutor_attended = EXCLUDED.tutor_attended, "
                    "evidence_source = EXCLUDED.evidence_source, "
                    "duration_minutes = EXCLUDED.duration_minutes, "
                    "recorded_at = EXCLUDED.recorded_at",
                    demo.demo_id,
                    outcome.outcome.value,
                    outcome.student_attended,
                    outcome.tutor_attended,
                    outcome.evidence_source.value,
                    outcome.duration_minutes,
                    outcome.recorded_by,
                    outcome.notes,
                    outcome.recorded_at,
                )

    async def load(self, demo_id: str) -> Demo | None:
        return _demo_from_row(
            await self._db.fetchrow(_DEMO_SELECT + " WHERE d.demo_id = $1", demo_id),
            await self._attendees(demo_id),
        )

    async def for_conversation(self, conversation_ref: str) -> Demo | None:
        row = await self._db.fetchrow(
            _DEMO_SELECT + " WHERE d.conversation_ref = $1 AND d.cancelled_at IS NULL "
            "ORDER BY d.created_at DESC LIMIT 1",
            conversation_ref,
        )
        if row is None:
            return None
        return _demo_from_row(row, await self._attendees(str(row["demo_id"])))

    async def in_window(
        self, *, region: str | None, from_at: datetime, to_at: datetime
    ) -> list[Demo]:
        rows = await self._db.fetch(
            _DEMO_SELECT + " WHERE d.starts_at >= $1 AND d.starts_at < $2 "
            "AND ($3::text IS NULL OR d.region = $3) ORDER BY d.starts_at",
            ensure_utc(from_at),
            ensure_utc(to_at),
            region,
        )
        out: list[Demo] = []
        for row in rows:
            demo = _demo_from_row(row, await self._attendees(str(row["demo_id"])))
            if demo is not None:
                out.append(demo)
        return out

    async def _attendees(self, demo_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch(
            "SELECT party, participant_ref, display_name, invite_consent "
            "FROM dcc_demo_attendees WHERE demo_id = $1",
            demo_id,
        )

    async def save_candidates(
        self,
        *,
        conversation_ref: str,
        match_session_id: str,
        candidates: tuple[TutorCandidateV1, ...],
        captured_at: datetime,
    ) -> None:
        async with self._db.transaction() as connection:
            for candidate in candidates:
                await connection.execute(
                    "INSERT INTO dcc_tutor_candidate_snapshots "
                    "(snapshot_id, conversation_ref, match_session_id, rank, tutor_ref, "
                    " display_name, profile_url, evidence, final_score, weight_coverage, "
                    " freshness, captured_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12) "
                    "ON CONFLICT (conversation_ref, match_session_id, rank) DO UPDATE SET "
                    "tutor_ref = EXCLUDED.tutor_ref, display_name = EXCLUDED.display_name, "
                    "profile_url = EXCLUDED.profile_url, evidence = EXCLUDED.evidence",
                    new_ulid(now=ensure_utc(captured_at)),
                    conversation_ref,
                    match_session_id,
                    candidate.rank,
                    candidate.tutor_ref,
                    candidate.name,
                    candidate.profile_url,
                    candidate.model_dump_json(),
                    candidate.final_score,
                    candidate.weight_coverage,
                    candidate.freshness.value,
                    ensure_utc(captured_at),
                )

    async def load_candidates(self, conversation_ref: str) -> tuple[TutorCandidateV1, ...]:
        rows = await self._db.fetch(
            "SELECT evidence FROM dcc_tutor_candidate_snapshots "
            "WHERE conversation_ref = $1 AND match_session_id = ("
            "  SELECT match_session_id FROM dcc_tutor_candidate_snapshots "
            "  WHERE conversation_ref = $1 ORDER BY captured_at DESC LIMIT 1) "
            "ORDER BY rank",
            conversation_ref,
        )
        out: list[TutorCandidateV1] = []
        for row in rows:
            try:
                out.append(TutorCandidateV1.model_validate(_json(row["evidence"])))
            except Exception:
                logger.warning("skipping an unparseable candidate snapshot")
        return tuple(out)


_DEMO_SELECT = (
    "SELECT d.*, o.outcome, o.student_attended, o.tutor_attended, o.evidence_source, "
    "       o.duration_minutes AS outcome_minutes, o.recorded_by, o.notes, o.recorded_at "
    "FROM dcc_demos d LEFT JOIN dcc_demo_outcomes o USING (demo_id)"
)


def _request_from_row(row: dict[str, Any] | None) -> DemoRequest | None:
    if row is None:
        return None
    from demo_command_center.contracts.common import Language, Requirement

    return DemoRequest(
        request_id=row["request_id"],
        conversation_ref=row["conversation_ref"],
        student_ref=row.get("student_ref"),
        requirement=Requirement(
            service=row.get("service"),
            board=row.get("board"),
            student_class=row.get("student_class"),
            subject=row.get("subject"),
            mode=row.get("mode"),
            region=row.get("req_region"),
            locality=row.get("locality"),
            timezone=row.get("timezone") or "Asia/Kolkata",
            availability_note=row.get("availability_note"),
            special_requirements=row.get("special_requirements"),
        ),
        language=Language(row.get("language") or "en"),
        region=row.get("region"),
        match_session_id=row.get("match_session_id"),
        created_at=row["created_at"],
    )


def _demo_from_row(row: dict[str, Any] | None, attendees: list[dict[str, Any]]) -> Demo | None:
    if row is None:
        return None
    from demo_command_center.contracts.common import (
        DemoOutcome,
        Language,
        Party,
    )
    from demo_command_center.domain.demo import (
        AttendanceSignal,
        DemoAttendee,
        DemoOutcomeRecord,
    )

    return Demo(
        demo_id=row["demo_id"],
        conversation_ref=row["conversation_ref"],
        request_id=row["request_id"],
        student_ref=row.get("student_ref"),
        tutor_ref=row.get("tutor_ref"),
        region=row.get("region"),
        mode=row["mode"],
        language=Language(row.get("language") or "en"),
        slot=(
            TimeSlot(
                starts_at=row["starts_at"],
                duration_minutes=int(row["duration_minutes"]),
                timezone=row["timezone"],
            )
            if row.get("starts_at")
            else None
        ),
        calendar_event_id=row.get("calendar_event_id"),
        meet_url=row.get("meet_url"),
        location_label=row.get("location_label"),
        attendees=tuple(
            DemoAttendee(
                party=Party(person["party"]),
                ref=person["participant_ref"],
                display_name=person.get("display_name") or "",
                invite_consent=bool(person.get("invite_consent")),
            )
            for person in attendees
        ),
        outcome=DemoOutcomeRecord(
            outcome=DemoOutcome(row.get("outcome") or "unknown"),
            student_attended=row.get("student_attended"),
            tutor_attended=row.get("tutor_attended"),
            evidence_source=AttendanceSignal(row.get("evidence_source") or "none"),
            duration_minutes=row.get("outcome_minutes"),
            recorded_by=row.get("recorded_by") or "",
            notes=row.get("notes") or "",
            recorded_at=row.get("recorded_at"),
        ),
        revision=int(row["revision"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cancelled_at=row.get("cancelled_at"),
        cancellation_reason=row.get("cancellation_reason") or "",
    )


# =================================================================== reminders


class PgReminderRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._db = pool

    async def replace_for_demo(
        self, demo_id: str, *, revision: int, reminders: list[ScheduledReminder]
    ) -> None:
        async with self._db.transaction() as connection:
            # Cancel then insert, in one transaction: a crash between the two
            # would otherwise leave a demo with no reminders at all.
            await connection.execute(
                "UPDATE dcc_demo_reminders SET status = 'cancelled' "
                "WHERE demo_id = $1 AND status = 'pending'",
                demo_id,
            )
            for reminder in reminders:
                await connection.execute(
                    "INSERT INTO dcc_demo_reminders "
                    "(reminder_id, demo_id, conversation_ref, demo_revision, label, audience, "
                    " recipient_ref, template, channel, fire_at, demo_starts_at, status) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'pending') "
                    "ON CONFLICT (demo_id, demo_revision, label, audience) DO NOTHING",
                    reminder.reminder_id,
                    reminder.demo_id,
                    reminder.conversation_ref,
                    reminder.demo_revision,
                    reminder.label,
                    reminder.audience.value,
                    reminder.recipient_ref,
                    reminder.template,
                    reminder.channel,
                    reminder.fire_at,
                    reminder.demo_starts_at,
                )

    async def due(self, *, now: datetime, limit: int = 100) -> list[ScheduledReminder]:
        rows = await self._db.fetch(
            "SELECT * FROM dcc_demo_reminders WHERE status = 'pending' AND fire_at <= $1 "
            "ORDER BY fire_at LIMIT $2",
            ensure_utc(now),
            limit,
        )
        return [_reminder_from_row(row) for row in rows]

    async def mark(self, reminder_id: str, *, status: str, now: datetime, detail: str = "") -> None:
        await self._db.execute(
            "UPDATE dcc_demo_reminders SET status = $2, attempts = attempts + 1, "
            "sent_at = CASE WHEN $2 = 'sent' THEN $3 ELSE sent_at END, "
            "suppression_reason = $4 WHERE reminder_id = $1",
            reminder_id,
            status,
            ensure_utc(now),
            detail[:64],
        )

    async def cancel_for_demo(self, demo_id: str) -> int:
        return _rowcount(
            await self._db.execute(
                "UPDATE dcc_demo_reminders SET status = 'cancelled' "
                "WHERE demo_id = $1 AND status = 'pending'",
                demo_id,
            )
        )

    async def sent_count(self, demo_id: str) -> int:
        value = await self._db.fetchval(
            "SELECT count(*) FROM dcc_demo_reminders WHERE demo_id = $1 AND status = 'sent'",
            demo_id,
        )
        return int(value or 0)


def _reminder_from_row(row: dict[str, Any]) -> ScheduledReminder:
    from demo_command_center.contracts.common import Party

    return ScheduledReminder(
        reminder_id=row["reminder_id"],
        demo_id=row["demo_id"],
        conversation_ref=row["conversation_ref"],
        demo_revision=int(row["demo_revision"]),
        label=row["label"],
        audience=Party(row["audience"]),
        recipient_ref=row["recipient_ref"],
        template=row["template"],
        channel=row["channel"],
        fire_at=row["fire_at"],
        demo_starts_at=row["demo_starts_at"],
        status=ReminderStatus(row["status"]),
        attempts=int(row["attempts"]),
        sent_at=row.get("sent_at"),
        suppression_reason=row.get("suppression_reason") or "",
    )


# ====================================================================== outbox


class PgOutboxRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._db = pool

    async def enqueue(
        self, *, event: str, payload: dict[str, Any], idempotency_key: str, now: datetime
    ) -> bool:
        row = await self._db.fetchrow(
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,"
            "$13::jsonb,$14::jsonb,$15,$16,$17,$18,$19,$20) "
            "VALUES ($1, $2, $3::jsonb, $4, $5) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING outbox_id",
            new_ulid(now=ensure_utc(now)),
            event,
            json.dumps(payload, default=str),
            idempotency_key,
            ensure_utc(now),
        )
        return row is not None

    async def unpublished(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return await self._db.fetch(
            "SELECT outbox_id, event, payload, idempotency_key, created_at "
            "FROM dcc_outbox_events WHERE published_at IS NULL "
            "ORDER BY created_at LIMIT $1",
            limit,
        )

    async def mark_published(self, outbox_id: str, *, now: datetime) -> None:
        await self._db.execute(
            "UPDATE dcc_outbox_events SET published_at = $2, attempts = attempts + 1 "
            "WHERE outbox_id = $1",
            outbox_id,
            ensure_utc(now),
        )


# ================================================================ message log


class PgMessageLog:
    def __init__(self, pool: PostgresPool) -> None:
        self._db = pool

    async def claim_send(self, message: OutboundMessage, *, now: datetime) -> bool:
        """The duplicate-send exclusion. One winner, decided by the primary key."""
        row = await self._db.fetchrow(
            "INSERT INTO dcc_message_log "
            "(idempotency_key, conversation_ref, recipient_ref, kind, template, demo_id, "
            " outcome, claimed_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,'claimed',$7) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING idempotency_key",
            message.idempotency_key,
            message.conversation_ref,
            message.recipient_ref,
            message.kind.value,
            message.template.name if message.template else "",
            message.demo_id,
            ensure_utc(now),
        )
        return row is not None

    async def record_result(
        self, *, idempotency_key: str, result: SendResult, now: datetime
    ) -> None:
        await self._db.execute(
            "UPDATE dcc_message_log SET outcome = $2, provider_message_id = $3, "
            "detail = $4, sent_at = $5 WHERE idempotency_key = $1",
            idempotency_key,
            result.outcome.value,
            result.provider_message_id,
            result.detail[:200],
            result.sent_at,
        )

    async def record_status(self, *, provider_message_id: str, status: str, now: datetime) -> None:
        await self._db.execute(
            "UPDATE dcc_message_log SET delivery_status = $2 WHERE provider_message_id = $1",
            provider_message_id,
            status,
        )

    async def sends_since(self, *, recipient_ref: str, since: datetime) -> int:
        value = await self._db.fetchval(
            "SELECT count(*) FROM dcc_message_log WHERE recipient_ref = $1 AND claimed_at >= $2",
            recipient_ref,
            ensure_utc(since),
        )
        return int(value or 0)


# ==================================================================== analysis


class PgAnalysisRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._db = pool

    async def save_objections(self, analysis: ObjectionAnalysisV1) -> None:
        await self._db.execute(
            "INSERT INTO dcc_objection_analyses "
            "(demo_id, conversation_ref, objections, sentiment, intent, "
            " recommended_next_step, summary, model_ref, prompt_version, analysed_at) "
            "VALUES ($1,$2,$3::jsonb,$4,$5,$6,$7,$8,$9,$10) "
            "ON CONFLICT (demo_id) DO UPDATE SET objections = EXCLUDED.objections, "
            "sentiment = EXCLUDED.sentiment, intent = EXCLUDED.intent, "
            "recommended_next_step = EXCLUDED.recommended_next_step, "
            "summary = EXCLUDED.summary, analysed_at = EXCLUDED.analysed_at",
            analysis.demo_id,
            analysis.conversation_ref,
            json.dumps([item.model_dump(mode="json") for item in analysis.objections]),
            analysis.sentiment.value,
            analysis.intent.value,
            analysis.recommended_next_step.value,
            analysis.summary,
            analysis.model_ref,
            analysis.prompt_version,
            analysis.analysed_at,
        )

    async def load_objections(self, demo_id: str) -> ObjectionAnalysisV1 | None:
        row = await self._db.fetchrow(
            "SELECT * FROM dcc_objection_analyses WHERE demo_id = $1", demo_id
        )
        if row is None:
            return None
        return ObjectionAnalysisV1.model_validate(
            {
                "demo_id": row["demo_id"],
                "conversation_ref": row["conversation_ref"],
                "objections": _json(row["objections"], []),
                "sentiment": row["sentiment"],
                "intent": row["intent"],
                "recommended_next_step": row["recommended_next_step"],
                "summary": row["summary"],
                "model_ref": row["model_ref"],
                "prompt_version": row["prompt_version"],
                "analysed_at": row["analysed_at"],
            }
        )

    async def save_forecast(self, record: dict[str, Any]) -> None:
        await self._db.execute(
            "INSERT INTO dcc_conversion_forecasts "
            "(forecast_id, demo_id, probability, risk_band, confidence, strategy, "
            " features, missing_features, contributions, policy_stamp, scored_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10,$11)",
            new_ulid(),
            record["demo_id"],
            float(record["probability"]),
            record["risk_band"],
            record["confidence"],
            record["strategy"],
            json.dumps(record.get("features", {}), default=str),
            json.dumps(record.get("missing_features", []), default=str),
            json.dumps(record.get("contributions", {}), default=str),
            record["policy_stamp"],
            record["scored_at"],
        )

    async def load_forecast(self, demo_id: str) -> dict[str, Any] | None:
        return await self._db.fetchrow(
            "SELECT * FROM dcc_conversion_forecasts WHERE demo_id = $1 "
            "ORDER BY scored_at DESC LIMIT 1",
            demo_id,
        )

    async def save_quality(self, record: dict[str, Any]) -> None:
        await self._db.execute(
            "INSERT INTO dcc_demo_quality_scores (demo_id, score, computed_at) "
            "VALUES ($1, $2, $3) ON CONFLICT (demo_id, computed_at) DO NOTHING",
            record["demo_id"],
            float(record["score"]),
            record["computed_at"],
        )


# ==================================================================== commerce


class PgCommerceRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._db = pool

    async def save_decision(self, decision: DiscountDecision) -> None:
        await self._db.execute(
            "INSERT INTO dcc_discount_decisions "
            "(decision_id, conversation_ref, demo_id, student_ref, status, band_name, percent, "
            " list_price_minor, discount_minor, payable_minor, floor_minor, currency, "
            " triggers, conditions, reason_code, requires_human_approval, approved_by, "
            " policy_stamp, valid_until, decided_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,"
            "$13::jsonb,$14::jsonb,$15,$16,$17,$18,$19,$20) "
            "ON CONFLICT (demo_id) DO UPDATE SET status = EXCLUDED.status, "
            "band_name = EXCLUDED.band_name, percent = EXCLUDED.percent, "
            "discount_minor = EXCLUDED.discount_minor, payable_minor = EXCLUDED.payable_minor, "
            "approved_by = EXCLUDED.approved_by, valid_until = EXCLUDED.valid_until, "
            "decided_at = EXCLUDED.decided_at",
            new_ulid(),
            decision.conversation_ref,
            decision.demo_id,
            decision.student_ref,
            decision.status.value,
            decision.band_name,
            decision.percent,
            decision.list_price_minor,
            decision.discount_minor,
            decision.payable_minor,
            decision.floor_minor,
            decision.currency,
            json.dumps([t.value for t in decision.triggers]),
            json.dumps(list(decision.conditions)),
            decision.reason_code.value,
            decision.requires_human_approval,
            decision.approved_by,
            decision.policy_stamp,
            decision.valid_until,
            decision.decided_at,
        )

    async def load_decision(self, demo_id: str) -> DiscountDecision | None:
        row = await self._db.fetchrow(
            "SELECT * FROM dcc_discount_decisions WHERE demo_id = $1", demo_id
        )
        if row is None:
            return None
        return DiscountDecision.model_validate(
            {
                **{k: v for k, v in row.items() if k != "decision_id"},
                "triggers": _json(row["triggers"], []),
                "conditions": _json(row["conditions"], []),
            }
        )

    async def offers_since(self, *, student_ref: str | None, since: datetime) -> int:
        if not student_ref:
            return 0
        value = await self._db.fetchval(
            "SELECT count(*) FROM dcc_discount_decisions "
            "WHERE student_ref = $1 AND percent > 0 AND decided_at >= $2",
            student_ref,
            ensure_utc(since),
        )
        return int(value or 0)

    async def save_order(self, order: PaymentOrder) -> None:
        await self._db.execute(
            "INSERT INTO dcc_payment_orders "
            "(order_ref, conversation_ref, demo_id, student_ref, amount_minor, currency, "
            " status, provider_order_id, payment_link, offer_policy_stamp, discount_percent, "
            " created_at, expires_at, paid_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) "
            "ON CONFLICT (order_ref) DO UPDATE SET status = EXCLUDED.status, "
            "provider_order_id = EXCLUDED.provider_order_id, "
            "payment_link = EXCLUDED.payment_link, paid_at = EXCLUDED.paid_at",
            order.order_ref,
            order.conversation_ref,
            order.demo_id,
            order.student_ref,
            order.amount_minor,
            order.currency,
            order.status.value if hasattr(order.status, "value") else str(order.status),
            order.provider_order_id,
            order.payment_link,
            order.offer_policy_stamp,
            order.discount_percent,
            order.created_at,
            order.expires_at,
            order.paid_at,
        )

    async def load_order(self, order_ref: str) -> PaymentOrder | None:
        row = await self._db.fetchrow(
            "SELECT * FROM dcc_payment_orders WHERE order_ref = $1", order_ref
        )
        return PaymentOrder.model_validate(dict(row)) if row else None

    async def order_for_conversation(self, conversation_ref: str) -> PaymentOrder | None:
        row = await self._db.fetchrow(
            "SELECT * FROM dcc_payment_orders WHERE conversation_ref = $1 "
            "ORDER BY created_at DESC LIMIT 1",
            conversation_ref,
        )
        return PaymentOrder.model_validate(dict(row)) if row else None

    async def record_event(self, event: PaymentEvent, *, now: datetime) -> bool:
        """The durable replay defence. The primary key decides."""
        row = await self._db.fetchrow(
            "INSERT INTO dcc_payment_events "
            "(provider_event_id, order_ref, kind, amount_minor, currency, "
            " provider_reference, signature_verified, raw_digest, occurred_at, recorded_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
            "ON CONFLICT (provider_event_id) DO NOTHING RETURNING provider_event_id",
            event.provider_event_id,
            event.order_ref,
            event.kind.value,
            event.amount_minor,
            event.currency,
            event.provider_reference,
            event.signature_verified,
            event.raw_digest,
            event.occurred_at,
            ensure_utc(now),
        )
        return row is not None

    async def save_activation(self, activation: SubscriptionActivation) -> None:
        await self._db.execute(
            "INSERT INTO dcc_subscription_activation_attempts "
            "(attempt_id, order_ref, conversation_ref, idempotency_key, attempt, succeeded, "
            " subscription_ref, error_code, attempted_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
            "ON CONFLICT (order_ref, attempt) DO NOTHING",
            new_ulid(),
            activation.order_ref,
            activation.conversation_ref,
            activation.idempotency_key,
            activation.attempt,
            activation.succeeded,
            activation.subscription_ref,
            activation.error_code,
            activation.attempted_at,
        )

    async def activation_for(self, order_ref: str) -> SubscriptionActivation | None:
        # A successful attempt wins over a later failed one: the partial unique
        # index guarantees at most one success, and that is the answer.
        row = await self._db.fetchrow(
            "SELECT * FROM dcc_subscription_activation_attempts WHERE order_ref = $1 "
            "ORDER BY succeeded DESC, attempt DESC LIMIT 1",
            order_ref,
        )
        if row is None:
            return None
        return SubscriptionActivation.model_validate(
            {k: v for k, v in row.items() if k != "attempt_id"}
        )


# ================================================================== operations


class PgOperationsRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._db = pool

    async def save_rollup(self, record: dict[str, Any]) -> None:
        await self._db.execute(
            "INSERT INTO dcc_regional_metric_rollups "
            "(rollup_id, region, metric, value, sample_size, window_start, window_end) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7) "
            "ON CONFLICT (region, metric, window_start) DO UPDATE SET "
            "value = EXCLUDED.value, sample_size = EXCLUDED.sample_size",
            new_ulid(),
            record["region"],
            record["metric"],
            record.get("value"),
            int(record.get("sample_size") or 0),
            record["window_start"],
            record["window_end"],
        )

    async def rollups(self, *, region: str, metric: str, limit: int) -> list[dict[str, Any]]:
        return await self._db.fetch(
            "SELECT * FROM dcc_regional_metric_rollups WHERE region = $1 AND metric = $2 "
            "ORDER BY window_start DESC LIMIT $3",
            region,
            metric,
            limit,
        )

    async def alert_fired_at(self, *, region: str, rule: str) -> datetime | None:
        fired: datetime | None = await self._db.fetchval(
            "SELECT max(fired_at) FROM dcc_underperformance_alerts WHERE region = $1 AND rule = $2",
            region,
            rule,
        )
        return fired

    async def record_alert(
        self, *, region: str, rule: str, now: datetime, payload: dict[str, Any]
    ) -> None:
        await self._db.execute(
            "INSERT INTO dcc_underperformance_alerts "
            "(alert_id, region, rule, metric, value, threshold, sample_size, severity, fired_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            new_ulid(),
            region,
            rule,
            str(payload.get("metric") or ""),
            float(payload.get("value") or 0),
            float(payload.get("threshold") or 0),
            int(payload.get("sample_size") or 0),
            str(payload.get("severity") or "warning"),
            ensure_utc(now),
        )

    async def open_human_case(self, record: dict[str, Any]) -> str:
        case_id = str(record.get("case_id") or f"hc_{new_ulid()}")
        await self._db.execute(
            "INSERT INTO dcc_human_handoff_cases "
            "(case_id, conversation_ref, demo_id, state, reason, severity, opened_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,now()) ON CONFLICT (case_id) DO NOTHING",
            case_id,
            record.get("conversation_ref"),
            record.get("demo_id"),
            str(record.get("state") or ""),
            str(record.get("problem") or record.get("reason") or ""),
            str(record.get("severity") or "normal"),
        )
        return case_id

    async def audit(self, record: dict[str, Any]) -> None:
        await self._db.execute(
            "INSERT INTO dcc_audit_events (audit_id, conversation_ref, event, actor, detail) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            new_ulid(),
            record.get("conversation_ref"),
            str(record.get("event") or "unknown"),
            str(record.get("actor") or ""),
            json.dumps(record, default=str),
        )


# ==================================================================== wiring


def build_postgres_stores(settings: Any) -> dict[str, Any]:
    """Wire every repository onto one shared pool."""
    pool = PostgresPool(
        settings.postgres_dsn.get_secret_value(),
        schema=settings.aurora_schema,
        min_size=settings.postgres_pool_min,
        max_size=settings.postgres_pool_max,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        require_tls=settings.postgres_require_tls,
    )
    return {
        "pool": pool,
        "conversations": PgConversationRepository(pool),
        "idempotency": PgIdempotencyRepository(pool),
        "demos": PgDemoRepository(pool),
        "slots": PgSlotRepository(pool),
        "reminders": PgReminderRepository(pool),
        "outbox": PgOutboxRepository(pool),
        "messages": PgMessageLog(pool),
        "analysis": PgAnalysisRepository(pool),
        "commerce": PgCommerceRepository(pool),
        "operations": PgOperationsRepository(pool),
    }
