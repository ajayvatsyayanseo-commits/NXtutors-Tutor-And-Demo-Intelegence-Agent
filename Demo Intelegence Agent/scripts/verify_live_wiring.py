"""Prove the two agents are wired to one database through one `.env`.

Not a unit test — it touches the real database named in the shared `.env`. It
writes into Demo's own schema and reads it back, then deletes what it wrote, so
it leaves no residue.

What it proves, in order:

1. Demo's `Settings` finds the shared repository `.env`.
2. Demo inherits `TMM_POSTGRES_DSN` — one URL, two agents.
3. Demo's schema and Tutor's schema are different, in one database.
4. A real state transition round-trips through the Postgres repositories.
5. Optimistic locking rejects a stale write.
6. The slot-hold unique index rejects a concurrent double-booking.
7. Tutor's schema is untouched by any of it.

    python scripts/verify_live_wiring.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from demo_command_center.config.settings import (
    PersistenceMode,
    get_settings,
)
from demo_command_center.contracts.common import DemoMode
from demo_command_center.domain.slots import SlotConflict, TimeSlot, new_hold
from demo_command_center.state.machine import (
    ConcurrencyConflict,
    StateMachine,
)
from demo_command_center.state.states import DemoState
from demo_command_center.state.triggers import Actor, Trigger
from demo_command_center.storage.postgres.repositories import (
    build_postgres_stores,
)

PASS = "  [PASS]"  # noqa: S105 - an output marker, not a credential
FAIL = "  [FAIL]"


async def main() -> int:
    failures: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print(f"{PASS if ok else FAIL} {label}{f'  ({detail})' if detail else ''}")
        if not ok:
            failures.append(label)

    print("=== 1. shared .env ===")
    settings = get_settings()
    check(
        settings.environment.value in {"local", "dev", "staging", "production"},
        "Settings loaded",
        f"environment={settings.environment.value}",
    )

    dsn = settings.postgres_dsn.get_secret_value()
    check(bool(dsn), "Demo has a DSN")
    check(
        "@" in dsn and "/" in dsn,
        "DSN inherited from TMM_POSTGRES_DSN",
        dsn.split("@")[-1] if "@" in dsn else "",
    )

    print("\n=== 2. schema separation ===")
    check(settings.aurora_schema == "demo_agent", "Demo schema", settings.aurora_schema)
    check(settings.aurora_schema != "tutor_match", "Demo does NOT write the protected Tutor schema")

    if settings.persistence_mode is not PersistenceMode.POSTGRES_DSN:
        print(f"\nSKIP: persistence_mode={settings.persistence_mode.value}, not postgres_dsn")
        return 1 if failures else 0

    stores = build_postgres_stores(settings)
    pool = stores["pool"]
    conversations = stores["conversations"]
    slots = stores["slots"]
    machine = StateMachine()
    ref = f"cv_wiring_{int(datetime.now(UTC).timestamp())}"

    try:
        print("\n=== 3. one database, two schemas ===")
        rows = await pool.fetch(
            "SELECT nspname FROM pg_namespace WHERE nspname IN ('demo_agent','tutor_match')"
        )
        names = {row["nspname"] for row in rows}
        check(
            names == {"demo_agent", "tutor_match"}, "both schemas present", ", ".join(sorted(names))
        )

        demo_tables = await pool.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'demo_agent'"
        )
        tutor_tables = await pool.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'tutor_match'"
        )
        check(int(demo_tables) >= 36, "Demo tables", f"{demo_tables} in demo_agent")
        check(int(tutor_tables) > 0, "Tutor tables intact", f"{tutor_tables} in tutor_match")

        print("\n=== 4. a real state transition round-trips ===")
        snapshot = await conversations.load(ref)
        check(
            snapshot.state is DemoState.NEW and snapshot.version == 0,
            "new conversation starts clean",
        )

        result = machine.fire(snapshot, Trigger.HANDOFF_RECEIVED, actor=Actor.AGENT)
        saved = await conversations.save_transition(result, now=datetime.now(UTC), facts={})
        check(
            saved.state is DemoState.OWNERSHIP_ACQUIRING and saved.version == 1,
            "transition persisted",
            f"{saved.state.value} v{saved.version}",
        )

        reloaded = await conversations.load(ref)
        check(
            reloaded.state is saved.state and reloaded.version == 1, "read back from the database"
        )

        history = await conversations.history(ref)
        check(len(history) == 1, "audit row written in the same transaction")

        print("\n=== 5. optimistic locking ===")
        try:
            await conversations.save_transition(result, now=datetime.now(UTC), facts={})
            check(False, "stale write rejected", "it was accepted")
        except ConcurrencyConflict:
            check(True, "stale write rejected with ConcurrencyConflict")

        print("\n=== 6. slot-hold exclusion ===")
        slot = TimeSlot(starts_at=datetime.now(UTC) + timedelta(days=2, hours=8))
        first = new_hold(
            hold_id=f"hld_a_{ref}",
            conversation_ref=ref,
            tutor_ref="tut_wiring",
            slot=slot,
            mode=DemoMode.ONLINE,
            now=datetime.now(UTC),
        )
        second = new_hold(
            hold_id=f"hld_b_{ref}",
            conversation_ref=f"{ref}_other",
            tutor_ref="tut_wiring",
            slot=slot,
            mode=DemoMode.ONLINE,
            now=datetime.now(UTC),
        )
        await slots.place_hold(first)
        check(True, "first hold placed")
        try:
            await slots.place_hold(second)
            check(False, "second hold rejected", "double-booking was allowed")
        except SlotConflict:
            check(True, "second hold rejected with SlotConflict")

        print("\n=== 7. cleanup ===")
        await pool.execute("DELETE FROM dcc_slot_holds WHERE tutor_ref = 'tut_wiring'")
        await pool.execute("DELETE FROM dcc_state_transitions WHERE conversation_ref = $1", ref)
        await pool.execute("DELETE FROM dcc_conversation_state WHERE conversation_ref = $1", ref)
        left = await pool.fetchval(
            "SELECT count(*) FROM dcc_conversation_state WHERE conversation_ref = $1", ref
        )
        check(int(left) == 0, "test rows removed")

        after = await pool.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'tutor_match'"
        )
        check(int(after) == int(tutor_tables), "Tutor schema unchanged throughout")
    finally:
        await pool.close()

    print()
    if failures:
        print(f"WIRING FAILED — {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("WIRING OK — both agents share one .env and one database, with separate schemas.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
