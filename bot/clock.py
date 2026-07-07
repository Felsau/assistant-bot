"""Timezone-aware 'now' for the bot.

Server clocks run in UTC; the user lives in a real timezone. Everything that
means "today" or "now" (digests, due dates, reminders) should go through here.
Set TIMEZONE in the environment to change it (default Asia/Bangkok).
"""

from __future__ import annotations

import calendar
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Bangkok"))


def now() -> datetime:
    return datetime.now(_TZ)


def today() -> date:
    return now().date()


def to_aware_iso(value: str | None) -> str | None:
    """Attach the configured timezone to a naive local datetime string.

    The classifier resolves "at 3pm" to a naive ``"YYYY-MM-DD HH:MM"`` in the
    user's local timezone. Stored straight into a Postgres ``timestamptz`` that
    gets read as UTC, so the reminder fires hours off. This stamps the local
    offset on, yielding e.g. ``"2026-06-30T15:00:00+07:00"`` — the right instant.

    Values that already carry an offset pass through; anything unparseable is
    returned unchanged so we never lose the reminder.
    """
    if not value:
        return value
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=_TZ).isoformat()
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    return dt.isoformat()


def _add_month(dt: datetime) -> datetime:
    """Add one calendar month, clamping the day (e.g. Jan 31 → Feb 28)."""
    month = dt.month + 1
    year = dt.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def next_occurrence(remind_at_iso: str, repeat: str | None) -> str | None:
    """Next future fire time for a repeating reminder, as an ISO string.

    ``repeat`` is 'daily' | 'weekly' | 'monthly'. Returns None for a one-off or
    an unknown repeat, so the caller can retire the reminder instead."""
    if repeat not in ("daily", "weekly", "monthly"):
        return None
    try:
        dt = datetime.fromisoformat(str(remind_at_iso))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    current = now()
    if repeat == "monthly":
        while dt <= current:
            dt = _add_month(dt)
    else:
        step = timedelta(days=1 if repeat == "daily" else 7)
        while dt <= current:
            dt += step
    return dt.isoformat()
