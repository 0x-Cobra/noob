from datetime import datetime, timedelta, timezone

from apebot.config import GateConfig
from apebot.gate import evaluate, max_drawdown
from apebot.store import Store
from apebot.types import Leg, LegStatus, PoolQuote, RefQuote, Side, Tick

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _seed(store: Store, n_win: int, n_loss: int, days: int, win=10.0, loss=-4.0):
    for i in range(days * 24):
        ts = T0 + timedelta(hours=i)
        pool = PoolQuote("X", "p", 3000, 100, 99.5, 1, 100.5, 1, 1, ts)
        store.add_tick(Tick(ts, "X", RefQuote("X", 100, ts), pool, True, True, False))
    cum = 0.0
    for i in range(n_win + n_loss):
        pnl = win if i < n_win else loss
        leg = Leg(None, "X", Side.SELL, 1, 105, 100, T0 + timedelta(hours=i), T0 + timedelta(hours=i + 1))
        store.add_leg(leg)
        leg.exit_px, leg.exit_ts, leg.exit_reason = 100, T0 + timedelta(hours=i + 1), "revert"
        leg.pnl_usd, leg.status = pnl, LegStatus.CLOSED
        store.close_leg(leg)
        cum += pnl
        store.add_equity(T0 + timedelta(hours=i + 1), "paper", cum, 0.0, 5000, 1000)


def test_max_drawdown():
    assert max_drawdown([0, 10, 5, 12, 2, 20]) == 10


def test_gate_passes_on_good_record(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed(store, 20, 5, days=15)
    r = evaluate(store, GateConfig(), 6000.0, T0 + timedelta(days=15))
    assert r.passed, r.failures
    assert r.metrics["round_trips"] == 25 and r.metrics["win_rate"] == 0.8


def test_gate_fails_on_short_or_losing_record(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed(store, 5, 10, days=3, loss=-8.0)
    r = evaluate(store, GateConfig(), 6000.0, T0 + timedelta(days=3))
    assert not r.passed
    assert any("days" in f for f in r.failures)
    assert any("net pnl" in f for f in r.failures)
    assert any("win rate" in f for f in r.failures)


def test_gate_counts_errors_not_rejects(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed(store, 20, 5, days=15)
    for i in range(100):
        store.add_event(T0, "warning", "exec_reject", "moved")
    r = evaluate(store, GateConfig(), 6000.0, T0 + timedelta(days=15))
    assert r.passed and r.metrics["exec_rejects"] == 100
    for i in range(50):
        store.add_event(T0, "error", "error", "rpc down")
    r = evaluate(store, GateConfig(), 6000.0, T0 + timedelta(days=15))
    assert not r.passed and any("error rate" in f for f in r.failures)


def test_store_kv_and_open_legs(tmp_path):
    store = Store(tmp_path / "t.db")
    store.set("mode", "paper")
    assert store.get("mode") == "paper"
    leg = store.add_leg(Leg(None, "X", Side.BUY, 1, 95, 100, T0, T0 + timedelta(hours=1), mode="live"))
    assert leg.id == 1
    assert [l.id for l in store.open_legs("live")] == [1]
    assert store.open_legs("paper") == []
