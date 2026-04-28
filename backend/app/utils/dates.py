from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def today_et():
    return now_et().date()


def timestamp_et() -> str:
    stamp = now_et().strftime("%I:%M %p ET")
    return stamp.lstrip("0")
