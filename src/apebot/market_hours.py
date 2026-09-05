"""US equity market calendar (NYSE) without external dependencies.

Regular session: 09:30-16:00 America/New_York.
Extended session: 04:00-20:00 America/New_York (used for reference-price staleness rules).
Holidays are a static list; extend `HOLIDAYS` / `HALF_DAYS` as years roll over.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
HALF_DAYS: set[date] = {
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
}

REG_OPEN = time(9, 30)
REG_CLOSE = time(16, 0)
HALF_CLOSE = time(13, 0)
EXT_OPEN = time(4, 0)
EXT_CLOSE = time(20, 0)


def to_ny(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts.astimezone(NY)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def regular_close(d: date) -> time:
    return HALF_CLOSE if d in HALF_DAYS else REG_CLOSE


def is_regular_open(ts: datetime) -> bool:
    ny = to_ny(ts)
    d = ny.date()
    return is_trading_day(d) and REG_OPEN <= ny.time() < regular_close(d)


def is_extended_open(ts: datetime) -> bool:
    ny = to_ny(ts)
    d = ny.date()
    return is_trading_day(d) and EXT_OPEN <= ny.time() < EXT_CLOSE


def next_regular_open(ts: datetime) -> datetime:
    """First regular-session open strictly after `ts`."""
    ny = to_ny(ts)
    d = ny.date()
    candidate = datetime.combine(d, REG_OPEN, tzinfo=NY)
    if not is_trading_day(d) or candidate <= ny:
        d = d + timedelta(days=1)
        while not is_trading_day(d):
            d += timedelta(days=1)
        candidate = datetime.combine(d, REG_OPEN, tzinfo=NY)
    return candidate


def last_regular_close(ts: datetime) -> datetime:
    """Most recent regular-session close at or before `ts`."""
    ny = to_ny(ts)
    d = ny.date()
    while True:
        if is_trading_day(d):
            c = datetime.combine(d, regular_close(d), tzinfo=NY)
            if c <= ny:
                return c
        d -= timedelta(days=1)
