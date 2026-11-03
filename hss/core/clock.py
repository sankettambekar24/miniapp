"""One time helper for the whole app.

* Source = the LAPTOP clock (datetime.now(timezone.utc)). The firmware has NO RTC, so device events are stamped
  with PC time when the PC receives them.
* Stored as ISO-8601 UTC ("2026-11-03T12:15:32.123Z"). Displayed in India time: "03-Nov-2026 05:45:32 PM IST".
* Time zone: zoneinfo "Asia/Kolkata" when the tz database exists (needs the `tzdata` package on Windows); otherwise a
  fixed +05:30 offset, which is exactly right for IST (India has no daylight saving time). `TZ_SOURCE` says which.
* Backend timestamps are parsed as UTC (naive values are assumed UTC) and converted.
* `set_now_for_tests` injects a clock; no other module calls datetime.now directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    TZ_SOURCE = "zoneinfo Asia/Kolkata"
except Exception:  # no tz database (typical on Windows without tzdata)
    IST = timezone(timedelta(hours=5, minutes=30), "IST")
    TZ_SOURCE = "fixed +05:30 fallback"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # noqa: E731


def set_now_for_tests(fn: Callable[[], datetime] | None) -> None:
    global _now
    _now = fn or (lambda: datetime.now(timezone.utc))


def now_utc() -> datetime:
    n = _now()
    return n if n.tzinfo else n.replace(tzinfo=timezone.utc)


def to_iso(dt: datetime | None = None) -> str:
    dt = (dt or now_utc())
    dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_utc(text: str | datetime | None) -> datetime | None:
    """ISO-8601 (Z / +hh:mm / naive, 'T' or space) -> aware datetime. Naive input is treated as UTC."""
    if text is None or text == "":
        return None
    if isinstance(text, datetime):
        return text if text.tzinfo else text.replace(tzinfo=timezone.utc)
    t = str(text).strip().replace(" ", "T", 1)
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_ist(value: str | datetime | None, empty: str = "n/a") -> str:
    """'03-Nov-2026 05:45:32 PM IST' (locale independent). Unparseable text is returned unchanged."""
    dt = parse_utc(value)
    if dt is None:
        return empty if value in (None, "") else str(value)
    l = dt.astimezone(IST)
    h12 = l.hour % 12 or 12
    return f"{l.day:02d}-{_MONTHS[l.month - 1]}-{l.year} {h12:02d}:{l.minute:02d}:{l.second:02d} {'AM' if l.hour < 12 else 'PM'} IST"
