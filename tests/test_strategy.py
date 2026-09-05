from datetime import datetime, timedelta, timezone

from apebot.config import StrategyConfig
from apebot.strategy import decide, exit_orders, force_close_time, leg_pnl
from apebot.types import Balances, Leg, PoolQuote, RefQuote, Side, Tick

T0 = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)   # Saturday


def tick(mid, ref=100.0, fee=0.003, impact=0.002, market_open=False, stale=False, qty=2.5):
    pool = PoolQuote("X", "0xP", 3000, mid, mid * (1 - fee - impact), qty, mid * (1 + fee + impact), qty, 1, T0)
    return Tick(T0, "X", RefQuote("X", ref, T0), pool, market_open, market_open, stale)


def cfg(**kw):
    return StrategyConfig(**kw)


def test_no_entry_when_premium_below_threshold():
    d = decide(tick(102.0), [], Balances(5000, {"X": 10}), T0, cfg(), None, True)
    assert d.orders == []


def test_sell_entry_on_premium_closed_market_needs_extra():
    c = cfg(sell_entry_premium=0.03, closed_market_extra=0.01)
    # 104.6 * (1 - 0.5% fee+impact) = 104.077 => 4.08% exec premium: passes 3%+1%
    d = decide(tick(104.6), [], Balances(5000, {"X": 10}), T0, c, None, True)
    assert len(d.orders) == 1 and d.orders[0].side == Side.SELL and d.orders[0].qty_base == 2.5
    # 3.5% mid => 3.0% exec: fails 4% closed-market threshold
    d = decide(tick(103.5), [], Balances(5000, {"X": 10}), T0, c, None, True)
    assert d.orders == []
    # ...but passes during the regular session
    d = decide(tick(103.6, market_open=True), [], Balances(5000, {"X": 10}), T0, c, None, True)
    assert len(d.orders) == 1


def test_buy_entry_on_discount_and_quote_balance_cap():
    d = decide(tick(95.0), [], Balances(5000, {"X": 0}), T0, cfg(), None, True)
    assert len(d.orders) == 1 and d.orders[0].side == Side.BUY
    d = decide(tick(95.0), [], Balances(10, {"X": 0}), T0, cfg(), None, True)
    assert d.orders == [] and "no quote balance" in d.notes[0]


def test_no_entry_when_inventory_missing_or_stale_or_halted():
    assert decide(tick(106.0), [], Balances(5000, {"X": 0}), T0, cfg(), None, True).orders == []
    assert decide(tick(106.0, stale=True), [], Balances(5000, {"X": 10}), T0, cfg(), None, True).orders == []
    assert decide(tick(106.0), [], Balances(5000, {"X": 10}), T0, cfg(), None, False).orders == []


def test_cooldown_and_max_legs():
    b = Balances(5000, {"X": 10})
    assert decide(tick(106.0), [], b, T0, cfg(cooldown_sec=300), T0 - timedelta(seconds=100), True).orders == []
    legs = [Leg(i, "X", Side.SELL, 2.5, 105, 100, T0, T0 + timedelta(days=1)) for i in range(3)]
    assert decide(tick(106.0), legs, b, T0, cfg(max_legs_per_ticker=3), None, True).orders == []


def test_exit_revert_stop_force():
    leg = Leg(1, "X", Side.SELL, 2.5, entry_px=105.0, entry_ref=100.0, entry_ts=T0,
              force_close_at=T0 + timedelta(hours=5))
    c = cfg(exit_band=0.005, stop_loss=0.06)
    # pool back at ref: buy_exec = 100.2*(1.005) = 100.7 > 100.5 -> not yet
    assert exit_orders(tick(100.2), [leg], T0, c) == []
    o = exit_orders(tick(99.8), [leg], T0, c)           # buy_exec 100.3 <= 100.5
    assert o and o[0].reason == "revert" and o[0].side == Side.BUY and o[0].leg_id == 1
    o = exit_orders(tick(111.0), [leg], T0, c)          # buy_exec 111.55 >= 105*1.06=111.3
    assert o and o[0].reason == "stop"
    o = exit_orders(tick(104.0), [leg], T0 + timedelta(hours=6), c)
    assert o and o[0].reason == "force"


def test_exit_revert_needs_fresh_ref_only_when_market_open():
    leg = Leg(1, "X", Side.SELL, 2.5, 105.0, 100.0, T0, T0 + timedelta(hours=5))
    assert exit_orders(tick(99.8, stale=True, market_open=True), [leg], T0, cfg()) == []
    assert exit_orders(tick(99.8, stale=True, market_open=False), [leg], T0, cfg())[0].reason == "revert"


def test_exits_take_priority_over_entries():
    leg = Leg(1, "X", Side.SELL, 2.5, 105.0, 100.0, T0, T0 - timedelta(minutes=1))
    d = decide(tick(106.0), [leg], Balances(5000, {"X": 10}), T0, cfg(), None, True)
    assert len(d.orders) == 1 and d.orders[0].reason == "force"


def test_force_close_time_after_weekend():
    c = cfg(force_close_after_open_min=45, intraday_max_hold_min=120)
    fc = force_close_time(T0, False, c)                   # Sat -> Mon 09:30 ET + 45 = 10:15 ET = 14:15 UTC
    assert fc == datetime(2026, 9, 14, 14, 15, tzinfo=timezone.utc)
    assert force_close_time(T0, True, c) == T0 + timedelta(hours=2)


def test_leg_pnl():
    sell = Leg(1, "X", Side.SELL, 2.0, 105.0, 100.0, T0, T0, fees_usd=0.05)
    assert abs(leg_pnl(sell, 100.0, 0.05) - (10.0 - 0.10)) < 1e-9
    buy = Leg(2, "X", Side.BUY, 2.0, 95.0, 100.0, T0, T0, fees_usd=0.05)
    assert abs(leg_pnl(buy, 100.0, 0.05) - (10.0 - 0.10)) < 1e-9
