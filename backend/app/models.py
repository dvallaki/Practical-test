"""
Schema for surveys and responses.

Four tables:

    client      one row per enterprise customer the surveys are run for
    survey      one row per survey in surveys.csv
    respondent  one row per *person*, identified by canonical email
    response    one row per attempt in responses.csv, every attempt kept

The reasoning for each key, constraint, index and nullable column is in the
README (section 2). Comments here say what, the README says why.

Create the tables with:

    docker compose exec backend python -m app.models
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, engine

# Kept as CHECK-constrained text rather than native Postgres ENUMs: adding a
# value later is a one-line constraint swap instead of ALTER TYPE.
RESPONSE_STATUSES = ("completed", "partial", "started", "abandoned")
CHANNELS = ("email", "sms", "whatsapp", "kiosk")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Client(Base):
    __tablename__ = "client"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    # IANA zone name. Reporting weeks are cut in the client's local time.
    timezone: Mapped[str] = mapped_column(
        Text, server_default=text("'Asia/Kolkata'")
    )

    surveys: Mapped[list[Survey]] = relationship(back_populates="client")

    __table_args__ = (CheckConstraint("btrim(name) <> ''", name="ck_client_name_not_blank"),)


class Survey(Base):
    __tablename__ = "survey"

    # Natural key: the client's own survey_id, so re-ingest upserts in place
    # and responses.csv can reference it directly.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(Text)
    invitations_sent: Mapped[int] = mapped_column(Integer)
    created_date: Mapped[date] = mapped_column(Date)

    client: Mapped[Client] = relationship(back_populates="surveys")
    responses: Mapped[list[Response]] = relationship(back_populates="survey")

    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_survey_client_name"),
        CheckConstraint("invitations_sent >= 0", name="ck_survey_invitations_non_negative"),
        CheckConstraint("btrim(name) <> ''", name="ck_survey_name_not_blank"),
        Index("ix_survey_client_id", "client_id"),
    )


class Respondent(Base):
    __tablename__ = "respondent"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Output of normalize.normalise_email(); the identity of a person.
    email: Mapped[str] = mapped_column(String(320), unique=True)

    responses: Mapped[list[Response]] = relationship(back_populates="respondent")

    __table_args__ = (
        # Guards against anything that bypassed normalise_email().
        CheckConstraint("email = lower(btrim(email))", name="ck_respondent_email_canonical"),
        CheckConstraint("position('@' in email) > 1", name="ck_respondent_email_has_at"),
    )


class Response(Base):
    __tablename__ = "response"

    # Natural key from the export: makes re-running the ingest idempotent
    # (ON CONFLICT (id)) and lets rejects.csv rows be traced back.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    survey_id: Mapped[int] = mapped_column(ForeignKey("survey.id", ondelete="RESTRICT"))
    respondent_id: Mapped[int] = mapped_column(
        ForeignKey("respondent.id", ondelete="RESTRICT")
    )
    # As written on this submission; the same person spells it differently
    # across source systems, so it belongs to the response, not the respondent.
    respondent_name: Mapped[str | None] = mapped_column(Text)

    # timestamptz: an absolute instant, stored as UTC. Kolkata today, Dubai
    # tomorrow, no re-migration.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String(16))
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    channel: Mapped[str | None] = mapped_column(String(16))
    free_text: Mapped[str | None] = mapped_column(Text)

    # "A respondent counts only once per survey." Every attempt is kept; the
    # ingest marks exactly one per (survey, respondent) as the counted one and
    # the partial unique index below makes a second counted one impossible.
    is_counted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    survey: Mapped[Survey] = relationship(back_populates="responses")
    respondent: Mapped[Respondent] = relationship(back_populates="responses")

    __table_args__ = (
        CheckConstraint(_in_list("status", RESPONSE_STATUSES), name="ck_response_status"),
        CheckConstraint(
            "channel IS NULL OR " + _in_list("channel", CHANNELS), name="ck_response_channel"
        ),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_response_rating_1_5"),
        CheckConstraint("duration_seconds >= 0", name="ck_response_duration_non_negative"),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ck_response_completed_after_started",
        ),
        CheckConstraint(
            "status <> 'completed' OR completed_at IS NOT NULL",
            name="ck_response_completed_has_timestamp",
        ),
        CheckConstraint(
            "status NOT IN ('started', 'abandoned') OR completed_at IS NULL",
            name="ck_response_unfinished_has_no_completion",
        ),
        # At most one counted attempt per person per survey.
        Index(
            "uq_response_counted_per_respondent",
            "survey_id",
            "respondent_id",
            unique=True,
            postgresql_where=text("is_counted"),
        ),
        # Weekly summary: survey filter + started_at range/grouping, with the
        # aggregated columns included so it is an index-only scan.
        Index(
            "ix_response_summary",
            "survey_id",
            "started_at",
            postgresql_include=["status", "duration_seconds"],
            postgresql_where=text("is_counted"),
        ),
        # FK lookups: survey -> all attempts (delete / audit), respondent -> attempts.
        Index("ix_response_survey_id", "survey_id"),
        Index("ix_response_respondent_id", "respondent_id"),
    )


def create_all() -> None:
    Base.metadata.create_all(engine)


if __name__ == "__main__":
    create_all()
    print("tables created:", ", ".join(sorted(Base.metadata.tables)))
