"""Timezone-aware 'now' for the bot.

Server clocks run in UTC; the user lives in a real timezone. Everything that
means "today" or "now" (digests, due dates, reminders) should go through here.
Set TIMEZONE in the environment to change it (default Asia/Bangkok).
"""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Bangkok"))


def now() -> datetime:
    return datetime.now(_TZ)


def today() -> date:
    return now().date()
