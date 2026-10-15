"""
/api/surveys/{id}/summary against the client's rules, on the loaded database.

The expected numbers are recomputed in Python from the raw counted rows, with
an independent week rule (zoneinfo + date.weekday), so a bug in the SQL
bucketing shows up as a mismatch. Skipped if the database isn't reachable or
the ingest hasn't been run.
"""

from collections import defaultdict
from datetime import timedelta
from statistics import median
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.db import engine
from app.main import app

client = TestClient(app)


def _loaded() -> bool:
    try:
        with engine.connect() as c:
            return c.execute(text("SELECT count(*) FROM response")).scalar() > 0
    except (OperationalError, ProgrammingError):
        return False


pytestmark = pytest.mark.skipif(not _loaded(), reason="database not loaded")


def _expected(survey_id: int) -> tuple[int, dict]:
    with engine.connect() as c:
        invitations, tz = c.execute(
            text(
                "SELECT s.invitations_sent, c.timezone FROM survey s "
                "JOIN client c ON c.id = s.client_id WHERE s.id = :id"
            ),
            {"id": survey_id},
        ).one()
        rows = c.execute(
            text(
                "SELECT started_at, status, duration_seconds FROM response "
                "WHERE survey_id = :id AND is_counted"
            ),
            {"id": survey_id},
        ).all()
    weeks = defaultdict(lambda: {"started": 0, "completed": 0, "durations": []})
    for started_at, status, duration in rows:
        day = started_at.astimezone(ZoneInfo(tz)).date()
        saturday = day - timedelta(days=(day.weekday() - 5) % 7)  # Mon=0 .. Sat=5
        w = weeks[saturday]
        if status in ("completed", "partial"):
            w["started"] += 1
        if status == "completed":
            w["completed"] += 1
            if duration is not None:
                w["durations"].append(duration)
    return invitations, weeks


def _survey_ids() -> list[int]:
    return [s["survey_id"] for s in client.get("/api/surveys").json()]


def test_list_returns_every_survey_with_what_the_page_needs():
    surveys = client.get("/api/surveys").json()
    assert len(surveys) == 12
    assert set(surveys[0]) == {"survey_id", "survey_name", "client_name", "invitations_sent"}


@pytest.mark.parametrize("survey_id", range(1, 13))
def test_summary_matches_independent_recomputation(survey_id):
    invitations, expected = _expected(survey_id)
    got = client.get(f"/api/surveys/{survey_id}/summary").json()["weeks"]
    by_week = {w["week_start"]: w for w in got}

    for saturday, e in expected.items():
        w = by_week[saturday.isoformat()]
        assert w["responses_started"] == e["started"]
        assert w["responses_completed"] == e["completed"]
        assert w["completion_rate"] == (
            round(e["completed"] / invitations, 4) if invitations else None
        )
        assert w["median_duration_seconds"] == (
            round(median(e["durations"]), 1) if e["durations"] else None
        )

    # Weeks with no responses at all are present as zero rows, not missing.
    for w in got:
        if w["week_start"] not in {d.isoformat() for d in expected}:
            assert (w["responses_started"], w["responses_completed"]) == (0, 0)


@pytest.mark.parametrize("survey_id", range(1, 13))
def test_rule_1_weeks_start_on_saturday_and_are_contiguous(survey_id):
    from datetime import date

    weeks = [date.fromisoformat(w["week_start"]) for w in
             client.get(f"/api/surveys/{survey_id}/summary").json()["weeks"]]
    assert all(d.weekday() == 5 for d in weeks)
    assert all(b - a == timedelta(days=7) for a, b in zip(weeks, weeks[1:]))


def test_rule_2_completion_rate_is_over_invitations_not_responses():
    weeks = client.get("/api/surveys/1/summary").json()["weeks"]
    total_completed = sum(w["responses_completed"] for w in weeks)
    # Weekly rates share one denominator, so they add up to the survey's rate.
    assert sum(w["completion_rate"] for w in weeks) == pytest.approx(
        total_completed / 4200, abs=1e-3
    )


def test_rule_3_partial_counts_as_response_not_completion():
    with engine.connect() as c:
        by_status = dict(
            c.execute(
                text(
                    "SELECT status, count(*) FROM response "
                    "WHERE survey_id = 5 AND is_counted GROUP BY status"
                )
            ).all()
        )
    weeks = client.get("/api/surveys/5/summary").json()["weeks"]
    assert sum(w["responses_started"] for w in weeks) == by_status["completed"] + by_status["partial"]
    assert sum(w["responses_completed"] for w in weeks) == by_status["completed"]


def test_survey_without_invitations_or_responses():
    got = client.get("/api/surveys/9/summary")
    assert got.status_code == 200
    assert got.json()["weeks"] == []


def test_unknown_survey_is_404():
    assert client.get("/api/surveys/999/summary").status_code == 404
