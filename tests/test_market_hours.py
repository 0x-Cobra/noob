from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apebot.market_hours import (is_extended_open, is_regular_open, last_regular_close, next_regular_open)

NY = ZoneInfo("America/New_York")


def ny(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def test_regular_hours_weekday():
    assert is_regular_open(ny(2026, 9, 9, 10))          # Wed 10:00
    assert not is_regular_open(ny(2026, 9, 9, 9, 29))
    assert not is_regular_open(ny(2026, 9, 9, 16))
    assert is_extended_open(ny(2026, 9, 9, 7))
    assert not is_extended_open(ny(2026, 9, 9, 21))


def test_weekend_and_holiday_closed():
    assert not is_regular_open(ny(2026, 9, 5, 12))      # Saturday
    assert not is_regular_open(ny(2026, 9, 7, 12))      # Labor Day 2026
    assert not is_extended_open(ny(2026, 9, 7, 12))


def test_next_open_skips_weekend_and_holiday():
    nxt = next_regular_open(ny(2026, 9, 4, 17))          # Fri after close
    assert nxt == ny(2026, 9, 8, 9, 30)                  # Tue (Mon is Labor Day)
    assert next_regular_open(ny(2026, 9, 9, 9, 0)) == ny(2026, 9, 9, 9, 30)
    assert next_regular_open(ny(2026, 9, 9, 9, 30)) == ny(2026, 9, 10, 9, 30)


def test_last_close():
    assert last_regular_close(ny(2026, 9, 6, 12)) == ny(2026, 9, 4, 16)
    assert last_regular_close(ny(2026, 11, 27, 15)) == ny(2026, 11, 27, 13)   # half day


def test_utc_input():
    assert is_regular_open(datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc))   # 11:00 ET
