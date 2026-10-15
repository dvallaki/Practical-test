"""
Ingest entry point.

Run it with:

    docker compose exec backend python -m app.ingest

The CSVs are mounted read-only at /data. Write `rejects.csv` wherever you like
and commit it to the repo.

Everything below the argument parsing is yours.

How it works:

1. Every row is parsed and validated in Python (using `app.normalize`). A row
   with any problem goes to rejects.csv with *all* of its reasons; it never
   reaches the database and never stops the run.
2. Valid rows are upserted on their natural keys (survey.id, response.id,
   respondent.email, client.name). An upsert only touches a row whose values
   actually changed, so re-running on the same file changes nothing.
3. `response.is_counted` is recomputed so each respondent counts once per
   survey (see `_recount`).

The whole load is one transaction: a crash half way leaves the previous load.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .db import SessionLocal, engine
from .models import CHANNELS, RESPONSE_STATUSES, Client, Survey, create_all
from .normalize import is_empty, normalise_email, parse_client_datetime, parse_duration_seconds

DATA_DIR = os.getenv("DATA_DIR", "/data")
SURVEYS_CSV = os.path.join(DATA_DIR, "surveys.csv")
RESPONSES_CSV = os.path.join(DATA_DIR, "responses.csv")
# /data is read-only; backend/ is mounted at /srv, so this lands in the repo.
REJECTS_CSV = os.getenv("REJECTS_CSV", str(Path(__file__).resolve().parent.parent / "rejects.csv"))

# Which attempt counts when one person answered a survey more than once:
# the furthest one, then the latest. started and abandoned are equally far.
STATUS_RANK = {"completed": 0, "partial": 1, "started": 2, "abandoned": 2}


class RowError(Exception):
    pass


@dataclass
class Rejects:
    rows: list[dict] = field(default_factory=list)

    def add(self, source: str, line: int, record_id: str, reasons: list[str], raw: dict) -> None:
        self.rows.append(
            {
                "source_file": source,
                "line_number": line,
                "record_id": record_id,
                "reason": "; ".join(reasons),
                "raw_row": json.dumps(raw, ensure_ascii=False),
            }
        )

    def write(self, path: str) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f, fieldnames=["source_file", "line_number", "record_id", "reason", "raw_row"]
            )
            w.writeheader()
            w.writerows(self.rows)


def _read_csv(path: str):
    """Yield (line_number, row). utf-8-sig strips the BOM the export starts with."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # line_num is the *last* physical line of the record (free_text can
            # span lines); good enough to find it in an editor.
            yield reader.line_num, row


def _text(raw: str | None) -> str | None:
    return None if is_empty(raw) else raw.strip()


def _int(raw: str | None, name: str) -> int:
    if is_empty(raw):
        raise RowError(f"missing {name}")
    try:
        return int(raw.strip())
    except ValueError:
        raise RowError(f"{name} is not an integer: {raw!r}") from None


# --------------------------------------------------------------------------- surveys


def _parse_survey(row: dict) -> dict:
    errors: list[str] = []
    out: dict = {}
    for name, fn in (
        ("id", lambda: _int(row.get("survey_id"), "survey_id")),
        ("invitations_sent", lambda: _int(row.get("invitations_sent"), "invitations_sent")),
    ):
        try:
            out[name] = fn()
        except RowError as e:
            errors.append(str(e))
    if out.get("invitations_sent", 0) < 0:
        errors.append(f"invitations_sent is negative: {out['invitations_sent']}")

    out["name"] = _text(row.get("survey_name"))
    out["client_name"] = _text(row.get("client_name"))
    if out["name"] is None:
        errors.append("missing survey_name")
    if out["client_name"] is None:
        errors.append("missing client_name")

    raw_date = row.get("created_date")
    if is_empty(raw_date):
        errors.append("missing created_date")
    else:
        try:
            out["created_date"] = datetime.strptime(raw_date.strip(), "%d/%m/%Y").date()
        except ValueError:
            errors.append(f"created_date is not a real DD/MM/YYYY date: {raw_date!r}")

    if errors:
        raise RowError("; ".join(errors))
    return out


def load_surveys(db: Session, rejects: Rejects) -> set[int]:
    parsed: dict[int, dict] = {}
    for line, row in _read_csv(SURVEYS_CSV):
        try:
            s = _parse_survey(row)
        except RowError as e:
            rejects.add("surveys.csv", line, row.get("survey_id", ""), str(e).split("; "), row)
            continue
        if s["id"] in parsed:
            rejects.add("surveys.csv", line, str(s["id"]), ["duplicate survey_id"], row)
            continue
        parsed[s["id"]] = s

    # Insert only names not already there, so a re-run doesn't burn sequence values.
    existing = set(db.scalars(select(Client.name)))
    clients = sorted({s["client_name"] for s in parsed.values()} - existing)
    if clients:
        db.execute(
            insert(Client).on_conflict_do_nothing(index_elements=["name"]),
            [{"name": c} for c in clients],
        )
    client_ids = dict(db.execute(select(Client.name, Client.id)).all())

    rows = [
        {
            "id": s["id"],
            "client_id": client_ids[s["client_name"]],
            "name": s["name"],
            "invitations_sent": s["invitations_sent"],
            "created_date": s["created_date"],
        }
        for s in parsed.values()
    ]
    if rows:
        stmt = insert(Survey)
        cols = ["client_id", "name", "invitations_sent", "created_date"]
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={c: stmt.excluded[c] for c in cols},
                where=_changed(Survey, stmt, cols),
            ),
            rows,
        )
    return set(db.scalars(select(Survey.id)))


# ------------------------------------------------------------------------- responses


def _parse_response(row: dict, survey_ids: set[int], now: datetime) -> dict:
    """Validate one responses.csv row. Collects every problem, not just the first."""
    errors: list[str] = []
    out: dict = {}

    def attempt(name, fn):
        try:
            out[name] = fn()
        except (RowError, ValueError) as e:
            errors.append(str(e))

    attempt("id", lambda: _int(row.get("response_id"), "response_id"))
    attempt("survey_id", lambda: _int(row.get("survey_id"), "survey_id"))
    if "survey_id" in out and out["survey_id"] not in survey_ids:
        errors.append(f"unknown survey_id {out['survey_id']}")

    def email():
        try:
            e = normalise_email(row.get("respondent_email"))
        except ValueError:
            raise RowError(f"invalid respondent_email: {row.get('respondent_email')!r}") from None
        if e is None:
            raise RowError("missing respondent_email")
        return e

    attempt("email", email)

    def started():
        try:
            v = parse_client_datetime(row.get("started_at"))
        except ValueError:
            raise RowError(f"started_at is not a real timestamp: {row.get('started_at')!r}") from None
        if v is None:
            raise RowError("missing started_at")
        if v > now:
            raise RowError(f"started_at is in the future: {row.get('started_at')!r}")
        return v

    def completed():
        try:
            v = parse_client_datetime(row.get("completed_at"))
        except ValueError:
            raise RowError(
                f"completed_at is not a real timestamp: {row.get('completed_at')!r}"
            ) from None
        if v is not None and v > now:
            raise RowError(f"completed_at is in the future: {row.get('completed_at')!r}")
        return v

    attempt("started_at", started)
    attempt("completed_at", completed)
    s, c = out.get("started_at"), out.get("completed_at")
    if s is not None and c is not None and c < s:
        errors.append("completed_at is before started_at")

    status = (row.get("status") or "").strip().lower()
    if status not in RESPONSE_STATUSES:
        errors.append(f"unknown status: {row.get('status')!r}")
    else:
        out["status"] = status
        if "completed_at" in out:
            if status == "completed" and c is None:
                errors.append("status is completed but completed_at is missing")
            if status in ("started", "abandoned") and c is not None:
                errors.append(f"status is {status} but completed_at is set")

    def rating():
        raw = row.get("rating")
        if is_empty(raw):
            return None
        try:
            v = int(raw.strip())
        except ValueError:
            raise RowError(f"rating is not an integer: {raw!r}") from None
        if not 1 <= v <= 5:
            raise RowError(f"rating out of range 1-5: {v}")
        return v

    attempt("rating", rating)

    def duration():
        try:
            v = parse_duration_seconds(row.get("duration_seconds"))
        except ValueError:
            raise RowError(
                f"duration_seconds is not a number: {row.get('duration_seconds')!r}"
            ) from None
        if v is not None and v < 0:
            raise RowError(f"duration_seconds is negative: {v}")
        return v

    attempt("duration_seconds", duration)

    channel = _text(row.get("channel"))
    if channel is not None:
        channel = channel.lower()
        if channel not in CHANNELS:
            errors.append(f"unknown channel: {row.get('channel')!r}")
    out["channel"] = channel

    out["respondent_name"] = _text(row.get("respondent_name"))
    out["free_text"] = _text(row.get("free_text"))

    if errors:
        raise RowError("; ".join(errors))
    return out


RESPONSE_COLS = [
    "survey_id",
    "respondent_id",
    "respondent_name",
    "started_at",
    "completed_at",
    "status",
    "rating",
    "duration_seconds",
    "channel",
    "free_text",
]


def load_responses(db: Session, survey_ids: set[int], rejects: Rejects) -> int:
    now = datetime.now(timezone.utc)
    parsed: dict[int, tuple[dict, dict]] = {}  # id -> (parsed, raw)
    for line, row in _read_csv(RESPONSES_CSV):
        rid = row.get("response_id", "")
        try:
            r = _parse_response(row, survey_ids, now)
        except RowError as e:
            rejects.add("responses.csv", line, rid, str(e).split("; "), row)
            continue
        if r["id"] in parsed:
            same = parsed[r["id"]][1] == row
            reason = (
                "duplicate response_id (identical row already loaded)"
                if same
                else "duplicate response_id with different data (first occurrence loaded)"
            )
            rejects.add("responses.csv", line, rid, [reason], row)
            continue
        parsed[r["id"]] = (r, row)

    _stage_and_upsert(db, [r for r, _ in parsed.values()])
    return len(parsed)


STAGE_COLS = ["id", "email"] + [c for c in RESPONSE_COLS if c != "respondent_id"]


def _stage_and_upsert(db: Session, rows: list[dict]) -> None:
    """COPY valid rows into a temp table, then upsert set-based in SQL.

    Bulk INSERT ... VALUES through SQLAlchemy spent most of a ~100 s run
    compiling bind parameters; COPY + two INSERT ... SELECT takes seconds.
    """
    db.execute(
        text(
            """
            CREATE TEMP TABLE stage_response ON COMMIT DROP AS
            SELECT r.id, ''::text AS email, r.survey_id, r.respondent_name,
                   r.started_at, r.completed_at, r.status, r.rating,
                   r.duration_seconds, r.channel, r.free_text
            FROM response r WITH NO DATA
            """
        )
    )
    cur = db.connection().connection.driver_connection.cursor()
    with cur.copy(f"COPY stage_response ({', '.join(STAGE_COLS)}) FROM STDIN") as copy:
        for r in rows:
            copy.write_row([r[c] for c in STAGE_COLS])

    # Only emails not already there, so a re-run doesn't burn sequence values.
    db.execute(
        text(
            """
            INSERT INTO respondent (email)
            SELECT DISTINCT s.email FROM stage_response s
            WHERE NOT EXISTS (SELECT 1 FROM respondent r WHERE r.email = s.email)
            ORDER BY s.email
            """
        )
    )

    cols = ", ".join(RESPONSE_COLS)
    db.execute(
        text(
            f"""
            INSERT INTO response (id, {cols})
            SELECT s.id, s.survey_id, p.id, s.respondent_name, s.started_at,
                   s.completed_at, s.status, s.rating, s.duration_seconds,
                   s.channel, s.free_text
            FROM stage_response s JOIN respondent p ON p.email = s.email
            ON CONFLICT (id) DO UPDATE
               SET ({cols}) = ROW({", ".join("EXCLUDED." + c for c in RESPONSE_COLS)})
               -- only rewrite a row whose values actually differ
               WHERE (response.{", response.".join(RESPONSE_COLS)})
                     IS DISTINCT FROM ({", ".join("EXCLUDED." + c for c in RESPONSE_COLS)})
            """
        )
    )


def _changed(model, stmt, cols):
    """ON CONFLICT ... WHERE: only rewrite a row whose values actually differ."""
    return tuple_(*(getattr(model, c) for c in cols)).is_distinct_from(
        tuple_(*(stmt.excluded[c] for c in cols))
    )


def _recount(db: Session) -> None:
    """Mark exactly one counted attempt per (survey, respondent).

    Two statements, not one: Postgres checks a non-deferrable unique index row
    by row during an UPDATE, so flipping the old and new pick together can
    trip uq_response_counted_per_respondent mid-statement. Both statements
    skip rows already in the right state, so a re-run writes nothing.
    """
    rank = " ".join(f"WHEN '{s}' THEN {n}" for s, n in STATUS_RANK.items())
    picks = f"""
        SELECT DISTINCT ON (survey_id, respondent_id) id
        FROM response
        ORDER BY survey_id, respondent_id,
                 CASE status {rank} END, started_at DESC, id DESC
    """
    db.execute(text(f"UPDATE response SET is_counted = false WHERE is_counted AND id NOT IN ({picks})"))
    db.execute(text(f"UPDATE response SET is_counted = true WHERE NOT is_counted AND id IN ({picks})"))


# ------------------------------------------------------------------------------ main


def run() -> int:
    """Load both CSVs. Return a process exit code."""
    print(f"surveys:   {SURVEYS_CSV}")
    print(f"responses: {RESPONSES_CSV}")
    create_all()
    rejects = Rejects()
    with SessionLocal() as db, db.begin():
        survey_ids = load_surveys(db, rejects)
        loaded = load_responses(db, survey_ids, rejects)
        _recount(db)
        totals = db.execute(
            text("SELECT count(*), count(*) FILTER (WHERE is_counted) FROM response")
        ).one()
    rejects.write(REJECTS_CSV)
    # Fresh statistics for the planner, and a current visibility map so the
    # summary query can use ix_response_summary as an index-only scan.
    # VACUUM can't run inside a transaction, hence AUTOCOMMIT.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM ANALYZE"))

    reasons = Counter(
        reason for r in rejects.rows for reason in r["reason"].split("; ")
    )
    print(f"surveys in db:        {len(survey_ids)}")
    print(f"responses valid:      {loaded}")
    print(f"responses in db:      {totals[0]} ({totals[1]} counted)")
    print(f"rows rejected:        {len(rejects.rows)}  -> {REJECTS_CSV}")
    for reason, n in reasons.most_common():
        print(f"  {n:5d}  {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
