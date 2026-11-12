"""
Field normalisation and validation.

Covers every messy format the assignment spec calls out:
  - Phones in 6+ formats
  - Dates in 4 formats (day-first, YYYY-MM-DD, DD-Mon-YYYY)
  - Decimals with ₹ / Rs / INR prefixes and comma thousands-separators
  - Enums compared case-insensitively
  - Header normalisation (lower-case, collapse whitespace/_)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))

# ── Header normalisation ───────────────────────────────────────────────────────

def normalise_header(h: str) -> str:
    """Lower-case, strip, replace runs of _ or whitespace with single space."""
    return re.sub(r"[_\s]+", " ", h.strip().lower())


# ── Phone ─────────────────────────────────────────────────────────────────────

_VALID_START = frozenset("6789")


def normalise_phone(raw: str) -> str | None:
    """
    Return canonical '+91XXXXXXXXXX' or None if invalid.

    Handles:
      +916463745499        (13 digits, leading +91)
      9932302346           (10 digits)
      +91 79328 96340      (spaces inside)
      (+ 91) 8196106462   (parentheses)
      91-77187-50036       (dashes, 12 digits starting 91)
      06832582386          (11 digits starting 0)
    """
    if not raw:
        return None

    # Strip everything that's not a digit
    digits = re.sub(r"\D", "", raw)

    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) != 10:
        return None
    if digits[0] not in _VALID_START:
        return None

    return f"+91{digits}"


# ── Date ──────────────────────────────────────────────────────────────────────

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5,  "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10,"nov": 11, "dec": 12,
}


def normalise_date(raw: str) -> str | None:
    """
    Accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY.
    Returns 'YYYY-MM-DD' or None for impossible/unrecognised dates.
    """
    raw = raw.strip()
    if not raw:
        return None

    day = month = year = None

    # YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        year, month, day = int(m[1]), int(m[2]), int(m[3])

    # DD/MM/YYYY
    if day is None:
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if m:
            day, month, year = int(m[1]), int(m[2]), int(m[3])

    # DD-MM-YYYY
    if day is None:
        m = re.fullmatch(r"(\d{1,2})-(\d{2})-(\d{4})", raw)
        if m:
            day, month, year = int(m[1]), int(m[2]), int(m[3])

    # DD-Mon-YYYY  (e.g. 22-Sep-2026)
    if day is None:
        m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", raw)
        if m:
            mon_str = m[2].lower()
            if mon_str not in _MONTH_MAP:
                return None
            day, month, year = int(m[1]), _MONTH_MAP[mon_str], int(m[3])

    if day is None:
        return None

    try:
        dt = datetime(year, month, day)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None  # impossible date e.g. 31/02


# ── Decimal ───────────────────────────────────────────────────────────────────

_CURRENCY_PREFIX = re.compile(
    r"^\s*(₹|rs\.?|inr)?\s*", re.IGNORECASE
)


def normalise_decimal(raw: str) -> Decimal | None:
    """
    Strip ₹/Rs/Rs./INR prefix and comma separators; return Decimal rounded to
    2 places, or None if invalid / negative.
    """
    raw = raw.strip()
    if not raw:
        return None

    cleaned = _CURRENCY_PREFIX.sub("", raw)
    cleaned = cleaned.replace(",", "").strip()

    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None

    if value < 0:
        return None

    return value.quantize(Decimal("0.01"))


# ── Enum ──────────────────────────────────────────────────────────────────────

def normalise_enum(raw: str, values: list[str]) -> str | None:
    """Case-insensitive match; store lower-case."""
    lower = raw.strip().lower()
    for v in values:
        if v.lower() == lower:
            return v.lower()
    return None


# ── Timestamp (events) ────────────────────────────────────────────────────────

_NAIVE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$")


def parse_event_timestamp(raw: str) -> datetime:
    """
    Parse UTC (Z), offset (+05:30), or naive (treated as IST).
    Always returns an aware datetime in UTC.
    """
    raw = raw.strip()

    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    if _NAIVE_RE.match(raw):
        # naive → IST
        raw_dt = datetime.fromisoformat(raw.replace(" ", "T"))
        return raw_dt.replace(tzinfo=IST).astimezone(timezone.utc)

    return datetime.fromisoformat(raw).astimezone(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    """Convert any aware datetime to IST."""
    return dt.astimezone(IST)


def ist_date_str(dt: datetime) -> str:
    """Return 'YYYY-MM-DD' in IST."""
    return to_ist(dt).strftime("%Y-%m-%d")


def ist_iso(dt: datetime) -> str:
    """Return ISO-8601 in IST, e.g. '2026-10-14T17:55:24+05:30'."""
    return to_ist(dt).isoformat(timespec="seconds")

