"""
Pure functions that turn one raw CSV field into one clean Python value.

READ THIS, IT IS GRADED SEPARATELY.

Your ingest must import and use these three functions rather than doing the
same work inline. After you submit, we drop our own pytest file into
`backend/tests/` and run it against this module on your repo, unchanged. It
imports only these three names. Keep the signatures exactly as they are.

Keep them pure: no database, no I/O, no logging, no global state. Add private
helpers in this file if you want to.

Two shared rules for all three functions:

* The client's export has more than one way of writing "there is no value
  here". Returning `None` for all of them is correct. The same handful of empty
  markers turns up across the columns of `data/responses.csv`, and we only test
  the ones that actually occur in that file, so reading the file is a
  legitimate and expected way to find out what they are. Treat the whole set as
  empty in all three functions.
* A value that is *present* but not parseable is a data error, not an empty
  value. Raise `ValueError` for those, so your ingest can put them in
  `rejects.csv` with a reason instead of silently turning them into `None`.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

__all__ = ["parse_client_datetime", "normalise_email", "parse_duration_seconds"]

# Every "no value here" marker that occurs in data/responses.csv, compared
# after stripping and case-folding: '', 'N/A', 'n/a', 'NULL', 'null', '-',
# 'none'. Whitespace-only counts as ''.
_EMPTY_MARKERS = frozenset({"", "n/a", "null", "-", "none"})

_CLIENT_TZ = ZoneInfo("Asia/Kolkata")
_CLIENT_DATETIME_FORMAT = "%d/%m/%Y %H:%M:%S"

# Pragmatic shape check, not RFC 5322: one '@', no whitespace, a dot in the
# domain, no empty labels.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s.]+(\.[^@\s.]+)+")

# Optional sign, digits with optional comma thousands groups ("2,460"),
# optional fractional part.
# ASCII digits only: \d would also accept e.g. Arabic-Indic digits.
_NUMBER_RE = re.compile(r"[+-]?([0-9]{1,3}(,[0-9]{3})+|[0-9]+)(\.[0-9]+)?")


def is_empty(raw: str | None) -> bool:
    """True if `raw` is one of the export's ways of writing "no value"."""
    return raw is None or raw.strip().casefold() in _EMPTY_MARKERS


def parse_client_datetime(raw: str | None) -> datetime | None:
    """Parse one timestamp exactly as the client's export writes them.

    Returns a timezone-aware `datetime`, or `None` if there is no value.
    Raises `ValueError` if a value is present but is not a real timestamp.

    The brief tells you the format the client writes and which local time it
    is in. Do not guess a different one.
    """
    if is_empty(raw):
        return None
    # strptime raises ValueError for wrong shapes and impossible dates
    # (29/02/2023, 31/04/2026, 25:00:00).
    text = raw.strip()
    if not text.isascii():  # strptime's %d etc. accept non-ASCII digits
        raise ValueError(f"not a client timestamp: {raw!r}")
    naive = datetime.strptime(text, _CLIENT_DATETIME_FORMAT)
    return naive.replace(tzinfo=_CLIENT_TZ)


def normalise_email(raw: str | None) -> str | None:
    """Return a canonical form of an email address.

    The canonical form is what you would compare two rows on to decide whether
    they came from the same person. Returns `None` if there is no value.
    Raises `ValueError` if a value is present but is not usable as an email
    address at all.

    You decide what "canonical" means here. Say why in your README.
    """
    if is_empty(raw):
        return None
    email = raw.strip().lower()
    if not _EMAIL_RE.fullmatch(email):
        raise ValueError(f"not an email address: {raw!r}")
    return email


def parse_duration_seconds(raw: str | None) -> int | None:
    """Return a duration as a whole number of seconds.

    Returns `None` if there is no value. Raises `ValueError` if a value is
    present but is not a number.

    Whether a particular number is a *plausible* duration is a business rule
    and belongs in your ingest, not in here.
    """
    if is_empty(raw):
        return None
    text = raw.strip()
    if not _NUMBER_RE.fullmatch(text):
        raise ValueError(f"not a number: {raw!r}")
    value = Decimal(text.replace(",", ""))
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
