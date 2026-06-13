"""Time and misc helpers. All times are IST (Asia/Kolkata)."""
from __future__ import annotations

from datetime import datetime, date, time, timedelta

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    import pytz
    IST = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def at_time(d: date, t: time) -> datetime:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=IST)


def minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def floor_to_candle(ts: datetime, minutes: int) -> datetime:
    """Floor a timestamp to its candle bucket start."""
    discard = timedelta(
        minutes=ts.minute % minutes, seconds=ts.second, microseconds=ts.microsecond
    )
    return ts - discard


def pct(a: float, b: float) -> float:
    """Percent change from b to a."""
    if not b:
        return 0.0
    return (a - b) / b * 100.0


def round_tick(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)
