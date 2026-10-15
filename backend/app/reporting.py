"""
Read-side queries for the API. Plain SQL: the weekly summary is one statement
and is easier to check against the client's rules written this way.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

SURVEYS_SQL = text(
    """
    SELECT s.id AS survey_id, s.name AS survey_name, c.name AS client_name,
           s.invitations_sent
    FROM survey s JOIN client c ON c.id = s.client_id
    ORDER BY s.id
    """
)

SURVEY_SQL = text(
    """
    SELECT s.id, s.invitations_sent, c.timezone
    FROM survey s JOIN client c ON c.id = s.client_id
    WHERE s.id = :survey_id
    """
)

# Rules, from the client's email:
#
# 1. Week runs Saturday-Friday, cut in the client's local time (Postgres dow:
#    Sun=0..Sat=6, so (dow + 1) % 7 is "days since Saturday").
# 2. completion_rate = completed / survey.invitations_sent; NULL when there
#    are no invitations rather than a divide-by-zero.
# 3. responses_started counts completed + partial; responses_completed counts
#    completed; started and abandoned count in neither.
#
# Plus: only the counted attempt per respondent (is_counted); a response
# belongs to the week it was *started* in; median duration is over completed
# responses; weeks inside the survey's range with no responses come back as
# zero rows rather than being missing.
SUMMARY_SQL = text(
    """
    WITH r AS (
        SELECT (started_at AT TIME ZONE :tz)::date AS local_day,
               status, duration_seconds
        FROM response
        WHERE survey_id = :survey_id AND is_counted
    ),
    w AS (
        SELECT local_day - ((extract(dow FROM local_day)::int + 1) % 7) AS week_start,
               status, duration_seconds
        FROM r
    ),
    agg AS (
        SELECT week_start,
               count(*) FILTER (WHERE status IN ('completed', 'partial')) AS responses_started,
               count(*) FILTER (WHERE status = 'completed') AS responses_completed,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_seconds)
                   FILTER (WHERE status = 'completed') AS median_duration_seconds
        FROM w
        GROUP BY week_start
    ),
    weeks AS (
        SELECT generate_series(min(week_start), max(week_start), interval '7 days')::date
               AS week_start
        FROM agg
    )
    SELECT weeks.week_start,
           coalesce(agg.responses_started, 0) AS responses_started,
           coalesce(agg.responses_completed, 0) AS responses_completed,
           agg.median_duration_seconds
    FROM weeks LEFT JOIN agg USING (week_start)
    ORDER BY weeks.week_start
    """
)


def list_surveys(db: Session) -> list[dict]:
    return [dict(row) for row in db.execute(SURVEYS_SQL).mappings()]


def survey_summary(db: Session, survey_id: int) -> dict | None:
    survey = db.execute(SURVEY_SQL, {"survey_id": survey_id}).one_or_none()
    if survey is None:
        return None
    invitations = survey.invitations_sent
    weeks = []
    for row in db.execute(SUMMARY_SQL, {"survey_id": survey_id, "tz": survey.timezone}):
        weeks.append(
            {
                "week_start": row.week_start.isoformat(),
                "responses_started": row.responses_started,
                "responses_completed": row.responses_completed,
                "completion_rate": (
                    round(row.responses_completed / invitations, 4) if invitations else None
                ),
                "median_duration_seconds": (
                    None
                    if row.median_duration_seconds is None
                    else round(row.median_duration_seconds, 1)
                ),
            }
        )
    return {
        "survey_id": survey.id,
        "invitations_sent": invitations,
        "timezone": survey.timezone,
        "weeks": weeks,
    }

