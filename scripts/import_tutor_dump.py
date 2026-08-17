"""Load tutors into `tutor_projection` from a website MySQL dump.

    python scripts/import_tutor_dump.py path/to/dump.sql [--dry-run] [--limit N]

**This is a bootstrap and disaster-recovery tool, not the production path.**
In production the projection is filled by `sync_projection`, which pages the
website's signed HTTPS feed (`/internal/agent/tutors`). Use this when:

* standing the agent up before the website's feed is deployed;
* the website is unreachable and the projection must be rebuilt from a backup;
* verifying that a schema change on the website still maps correctly, offline.

It reads the dump with the same field semantics the feed publishes, so a tutor
imported here and the same tutor synced later produce the same row — including
the same `source_checksum`, so the next reconciliation sees no drift.

Deliberately reads a *file*, never a live MySQL connection: this service holds
no MySQL driver and no grant on the website's database, and that stays true.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tutor_match_meta.config.settings import get_settings
from tutor_match_meta.contracts.common import Freshness
from tutor_match_meta.contracts.tutor import (
    ReviewAggregate,
    TutorCandidate,
    TutorCapabilities,
)
from tutor_match_meta.domain import academics, fees, modes, subjects
from tutor_match_meta.domain.identity import encode_public_ref
from tutor_match_meta.domain.reviews import parse_review_date
from tutor_match_meta.domain.text import clean
from tutor_match_meta.repositories.postgres import build_sessions, create_engine
from tutor_match_meta.sync.projection import _upsert

#: Tables read. Everything else in the dump is ignored.
WANTED = (
    "register",
    "teacher_courses",
    "teacher_course_managment",
    "teacher_review",
    "category",
)


def split_values(block: str):
    """Yield one row of raw values per `(...)` group.

    Hand-written rather than regex because dumps contain commas, parentheses
    and escaped quotes inside text columns, and a regex over that silently
    mangles rows — which would import corrupted tutor data.
    """
    row: list[str] = []
    field: list[str] = []
    quoted = False
    escaped = False
    depth = 0

    for ch in block:
        if quoted:
            if escaped:
                field.append(ch)
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                quoted = False
            else:
                field.append(ch)
            continue
        if ch == "'":
            quoted = True
        elif ch == "(":
            depth += 1
            if depth == 1:
                row, field = [], []
        elif ch == ")":
            depth -= 1
            if depth == 0:
                row.append("".join(field).strip())
                yield row
                row, field = [], []
        elif ch == "," and depth == 1:
            row.append("".join(field).strip())
            field = []
        elif depth == 1:
            field.append(ch)


def read_tables(dump: Path) -> dict[str, list[dict[str, str]]]:
    """One pass over the dump, collecting only the tables we need.

    The column list is read off each INSERT rather than assumed, so a column
    added to the website later cannot shift every value one position left.
    """
    pattern = re.compile(r"^INSERT INTO `(" + "|".join(WANTED) + r")` \((.*?)\) VALUES\s*(.*)$")
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    table = ""
    columns: list[str] = []
    buffer: list[str] = []
    collecting = False

    with dump.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not collecting:
                match = pattern.match(line)
                if not match:
                    continue
                table = match.group(1)
                columns = [c.strip().strip("`") for c in match.group(2).split(",")]
                buffer = [match.group(3)]
                collecting = True
            else:
                buffer.append(line)

            if buffer and buffer[-1].rstrip().endswith(";"):
                collecting = False
                for row in split_values("".join(buffer)):
                    out[table].append(dict(zip(columns, row, strict=False)))
                buffer = []
    return out


def value(row: dict[str, str], key: str) -> str:
    raw = row.get(key) or ""
    return "" if raw.upper() == "NULL" else raw


def build_candidates(tables: dict[str, list[dict[str, str]]], now: datetime):
    """Map the dump into candidates using the website's own field semantics.

    Mirrors `App\\NxtAi\\Support\\PublicTutorFieldMapper::capabilities()`:
    the id-schema resolves `cat_id`/`pid`/`cid` against `category` as
    subject/board/class, and the string-schema reads its plain columns. Both are
    unioned, because 1,269 tutors appear only in the id-schema and 24 only in
    the string-schema — either alone loses most of the tutor base.
    """
    categories = {
        str(row.get("id")): clean(value(row, "cat_title"))
        for row in tables.get("category", [])
        if (row.get("status") or "t") == "t"
    }

    # Root categories (`pid = 0`) are taxonomy levels, not subjects:
    # "Academic (Class XI-XII)", "Entrance Exam Preparation", "IT & Software
    # Training". Every `cat_id` in the data resolves to one of them.
    #
    # Importing them as subjects is actively harmful, not merely useless. A
    # tutor with the fake subject "Academic (Class XI-XII)" has a *non-empty*
    # subject list, so `hard_filters.subject_supported` rejects them for every
    # real subject request — whereas a tutor with no subject at all is kept,
    # because unknown capability is not a contradiction. Mapping these would
    # have silently removed ~1,270 of 1,894 tutors from every subject search.
    root_ids = {
        str(row.get("id"))
        for row in tables.get("category", [])
        if str(row.get("pid") or "0").strip() in {"0", "", "NULL"}
    }

    # ------------------------------------------------------------ capabilities
    caps: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"subjects": [], "boards": [], "classes": [], "modes": []}
    )

    for row in tables.get("teacher_course_managment", []):
        user = value(row, "user_id")
        if not user:
            continue
        bucket = caps[user]
        cat_id = value(row, "cat_id")
        if cat_id not in root_ids:
            bucket["subjects"].append(categories.get(cat_id, ""))
        bucket["boards"].append(categories.get(value(row, "pid"), ""))
        bucket["classes"].append(categories.get(value(row, "cid"), ""))

    for row in tables.get("teacher_courses", []):
        if (row.get("status") or "t") != "t":
            continue
        user = value(row, "user_id")
        if not user:
            continue
        bucket = caps[user]
        bucket["subjects"].append(value(row, "subject"))
        bucket["boards"].append(value(row, "board"))
        bucket["classes"].append(value(row, "for_class"))
        bucket["modes"].extend([value(row, "class_type"), value(row, "mode")])

    # ---------------------------------------------------------------- reviews
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in tables.get("teacher_review", []):
        if (row.get("status") or "") == "t" and value(row, "user_id"):
            grouped[value(row, "user_id")].append(row)

    # --------------------------------------------------------------- tutors
    candidates: list[TutorCandidate] = []
    skipped = 0

    for row in tables.get("register", []):
        if (value(row, "join_as") or "").lower() != "teacher":
            continue
        if (row.get("status") or "") != "t":
            continue
        user = value(row, "user_id")
        if not user:
            skipped += 1
            continue

        bucket = caps.get(user, {"subjects": [], "boards": [], "classes": [], "modes": []})
        mode_values = [*bucket["modes"], value(row, "class_type")]

        candidates.append(
            TutorCandidate(
                tutor_id=user,
                public_ref=encode_public_ref(user),
                name=clean(value(row, "name")) or "Tutor",
                gender=_gender(value(row, "gender")),
                avatar_url=clean(value(row, "avatar")) or None,
                city=clean(value(row, "city")) or None,
                # `register.address` is deliberately never read. The agent
                # matches at locality granularity and a street address it never
                # holds is one it can never leak.
                locality=None,
                district=clean(value(row, "district")) or None,
                state=clean(value(row, "state")) or None,
                pincode=_pincode(value(row, "pincode")),
                capabilities=TutorCapabilities(
                    subjects=_unique(subjects.normalize, bucket["subjects"])[:12],
                    boards=_unique(academics.normalize_board, bucket["boards"])[:8],
                    classes=_unique(academics.normalize_class, bucket["classes"])[:16],
                    modes=modes.union_modes(*mode_values),
                ),
                experience_years=fees.parse_experience_years(clean(value(row, "experience"))),
                education=clean(f"{value(row, 'education')} {value(row, 'other_education')}")[:400]
                or None,
                profile_summary=(
                    clean(value(row, "profile_desc"))
                    or clean(value(row, "profile"))
                    or clean(value(row, "pro_desc"))
                )[:1_200]
                or None,
                fee=fees.parse_tutor_fee(clean(value(row, "budget"))),
                reviews=_reviews(grouped.get(user, [])),
                availability=None,  # the website schema holds no schedules
                freshness=Freshness.FRESH,
                source_updated_at=parse_review_date(value(row, "date")) or now,
                synced_at=now,
            )
        )
    return candidates, skipped


def _unique(normalise, values: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for raw in values:
        canonical = normalise(clean(raw)) if raw else None
        if canonical:
            seen.setdefault(canonical, None)
    return tuple(seen)


def _reviews(rows: list[dict[str, str]]) -> ReviewAggregate:
    if not rows:
        return ReviewAggregate()

    def average(key: str) -> float | None:
        numbers = []
        for row in rows:
            raw = value(row, key)
            try:
                parsed = float(raw)
            except (TypeError, ValueError):
                continue
            # Out of range means malformed source data, not a perfect score.
            if 0.0 <= parsed <= 5.0:
                numbers.append(parsed)
        return round(sum(numbers) / len(numbers), 2) if numbers else None

    dates = [d for d in (parse_review_date(value(r, "date")) for r in rows) if d]
    return ReviewAggregate(
        count=len(rows),
        rating_avg=average("rating"),
        expertise_avg=average("expertise"),
        patience_avg=average("patience"),
        reliability_avg=average("reliability"),
        communication_avg=average("communication"),
        latest_review_at=max(dates) if dates else None,
    )


def _gender(raw: str) -> str | None:
    text = clean(raw).lower()
    return text.capitalize() if text in {"male", "female"} else None


def _pincode(raw: str) -> str | None:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits if len(digits) == 6 else None


def report(candidates: list[TutorCandidate]) -> None:
    total = len(candidates)
    with_subject = sum(1 for c in candidates if c.capabilities.subjects)
    with_class = sum(1 for c in candidates if c.capabilities.classes)
    with_board = sum(1 for c in candidates if c.capabilities.boards)
    with_fee = sum(1 for c in candidates if c.fee.minimum is not None)
    with_reviews = sum(1 for c in candidates if c.reviews.count)
    with_pin = sum(1 for c in candidates if c.pincode)
    with_mode = sum(1 for c in candidates if c.capabilities.modes)

    print(f"\n  mapped tutors            {total:,}")
    for label, count in (
        ("with a subject", with_subject),
        ("with a class", with_class),
        ("with a board", with_board),
        ("with a mode", with_mode),
        ("with a pincode", with_pin),
        ("with a fee", with_fee),
        ("with >=1 review", with_reviews),
    ):
        pct = (count / total * 100) if total else 0
        print(f"    {label:22} {count:6,}  {pct:5.1f}%")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path, help="path to the website .sql dump")
    parser.add_argument("--dry-run", action="store_true", help="map and report, write nothing")
    parser.add_argument("--limit", type=int, default=0, help="import at most N tutors")
    parser.add_argument("--batch", type=int, default=200, help="rows per transaction")
    args = parser.parse_args()

    if not args.dump.exists():
        print(f"dump not found: {args.dump}")
        return 1

    now = datetime.now(UTC)
    print(f"reading {args.dump.name} ...")
    tables = read_tables(args.dump)
    for name in WANTED:
        print(f"  {name:28} {len(tables.get(name, [])):>8,} rows")

    candidates, skipped = build_candidates(tables, now)
    if skipped:
        print(f"  skipped {skipped} tutor rows with no user_id")
    if args.limit:
        candidates = candidates[: args.limit]
    report(candidates)

    if args.dry_run:
        print("\n  --dry-run: nothing written")
        return 0

    settings = get_settings()
    sessions = build_sessions(create_engine(settings))
    written = 0
    for start in range(0, len(candidates), args.batch):
        batch = candidates[start : start + args.batch]
        written += await _upsert(sessions, batch, now)
        print(f"    upserted {written:,}/{len(candidates):,}", end="\r")

    print(f"\n  wrote {written:,} rows into {settings.postgres_schema}.tutor_projection")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
