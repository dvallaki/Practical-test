"""
normalize.py against the values that actually occur in data/responses.csv.

The parametrised cases are copied from the file. The sweep at the bottom runs
every distinct value of each column through its parser and checks that the
only failure mode is ValueError (skipped if the CSV isn't mounted).
"""

import csv
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.normalize import (
    normalise_email,
    parse_client_datetime,
    parse_duration_seconds,
)

KOLKATA = ZoneInfo("Asia/Kolkata")

# Every "no value" spelling found across the columns of responses.csv.
EMPTY = ["", "   ", "N/A", "n/a", "NULL", "null", "-", "none", None]


# ---------------------------------------------------------------- empties


@pytest.mark.parametrize("raw", EMPTY)
@pytest.mark.parametrize(
    "fn", [parse_client_datetime, normalise_email, parse_duration_seconds]
)
def test_empty_markers_are_none_everywhere(fn, raw):
    assert fn(raw) is None


# --------------------------------------------------------------- datetime


def test_datetime_is_day_first_kolkata():
    got = parse_client_datetime("04/03/2026 08:40:00")
    assert got == datetime(2026, 3, 4, 8, 40, tzinfo=KOLKATA)
    assert got.utcoffset() == timedelta(hours=5, minutes=30)
    assert got.astimezone(timezone.utc) == datetime(2026, 3, 4, 3, 10, tzinfo=timezone.utc)


def test_datetime_day_above_12_cannot_be_month_first():
    assert parse_client_datetime("31/12/2027 23:59:59").month == 12


def test_datetime_strips_whitespace():
    assert parse_client_datetime(" 17/03/2026 09:15:00 ") == parse_client_datetime(
        "17/03/2026 09:15:00"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "29/02/2023 10:00:00",  # not a leap year (row 900006)
        "31/04/2026 10:00:00",
        "17/03/2026 25:00:00",
        "17/03/2026",  # date only
        "2026-03-17 09:15:00",  # ISO is not what the client writes
        "17/03/2026 09:15:00+05:30",
        "١٧/٠٣/٢٠٢٦ ٠٩:١٥:٠٠",  # non-ASCII digits
        "yesterday",
    ],
)
def test_datetime_present_but_invalid_raises(raw):
    with pytest.raises(ValueError):
        parse_client_datetime(raw)


# ------------------------------------------------------------------ email


@pytest.mark.parametrize(
    "a, b",
    [
        ("Priya.Sharma22@example.com", "priya.sharma22@example.com "),  # 900001 / 900002
        ("ASHA.RAO@EXAMPLE.COM", "asha.rao@example.com"),
        ("\tasha.rao@example.com\n", "asha.rao@example.com"),
    ],
)
def test_email_same_person_same_canonical_form(a, b):
    assert normalise_email(a) == normalise_email(b)


def test_email_canonical_form_is_trimmed_lowercase():
    assert normalise_email(" Priya.Sharma22@Example.COM ") == "priya.sharma22@example.com"


@pytest.mark.parametrize(
    "raw",
    ["0091.pooja@example.com", "j.mueller@example.com", "sean.obrien@example.com"],
)
def test_email_valid_addresses_kept_verbatim(raw):
    assert normalise_email(raw) == raw


def test_email_does_not_merge_dots_or_plus_tags():
    assert normalise_email("a.b@example.com") != normalise_email("ab@example.com")
    assert normalise_email("ab+x@example.com") != normalise_email("ab@example.com")


@pytest.mark.parametrize(
    "raw",
    ["not-an-email", "a@b", "@example.com", "a@@example.com", "a b@example.com", "a@example."],
)
def test_email_garbage_raises(raw):
    with pytest.raises(ValueError):
        normalise_email(raw)


# --------------------------------------------------------------- duration


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("479", 479),
        ("0", 0),
        (" 1980 ", 1980),
        ("2,460", 2460),  # row 900009: timestamps 41 min apart
        ("-2400", -2400),  # a number; plausibility is the ingest's job
        ("12.5", 13),
        ("12.0", 12),
    ],
)
def test_duration_parses(raw, expected):
    assert parse_duration_seconds(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "12s", "1,2", "1,23,456", "nan", "inf", "١٢"])
def test_duration_not_a_number_raises(raw):
    with pytest.raises(ValueError):
        parse_duration_seconds(raw)


# ---------------------------------------------------- sweep over the file

RESPONSES_CSV = os.path.join(os.getenv("DATA_DIR", "/data"), "responses.csv")


def _distinct(column):
    with open(RESPONSES_CSV, newline="", encoding="utf-8-sig") as f:
        return sorted({row[column] for row in csv.DictReader(f)})


@pytest.mark.skipif(not os.path.exists(RESPONSES_CSV), reason="responses.csv not mounted")
@pytest.mark.parametrize(
    "column, fn",
    [
        ("started_at", parse_client_datetime),
        ("completed_at", parse_client_datetime),
        ("respondent_email", normalise_email),
        ("duration_seconds", parse_duration_seconds),
    ],
)
def test_every_value_in_the_file_parses_or_raises_value_error(column, fn):
    failures = []
    for raw in _distinct(column):
        try:
            got = fn(raw)
        except ValueError:
            failures.append(raw)
            continue
        if isinstance(got, datetime):
            assert got.tzinfo is not None
    # The only unparseable values in the file are the known bad rows.
    assert failures == {
        "started_at": ["29/02/2023 10:00:00"],
        "completed_at": ["29/02/2023 10:20:00"],
        "respondent_email": ["not-an-email"],
        "duration_seconds": [],
    }[column]
