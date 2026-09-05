"""End-to-end: drive the runner with the synthetic world and check the plumbing holds together."""
from datetime import datetime, timedelta, timezone

from apebot.config import Config, TickerConfig
from apebot.execution import PaperExecutor
from apebot.notify import Notifier
from apebot.runner import Runner
from apebot.sim import build_world
from apebot.store import Store


def make_runner(tmp_path, days_cfg=None, **world_kw):
    cfg = Config(tickers=[TickerConfig(symbol="AAA", token="0x" + "0" * 40, paper_inventory=10),
                          TickerConfig(symbol="BBB", token="0x" + "1" * 40, paper_inventory=10)],
                 poll_interval_sec=60)
    if days_cfg:
        days_cfg(cfg)
    start = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)   # Friday afternoon ET
    clock, market, ref = build_world(["AAA", "BBB"], start, seed=3, **world_kw)
    store = Store(tmp_path / "sim.db")
    runner = Runner(cfg, store, market, ref, PaperExecutor(market, cfg.strategy, cfg.chain),
                    Notifier(None, None, False), clock, on_fill=market.on_fill)
    return runner, clock, store, cfg


def run_days(runner, clock, cfg, days):
    end = clock.now() + timedelta(days=days)
    while clock.now() < end:
        runner.step()
        clock.sleep(cfg.poll_interval_sec)


def test_sim_produces_round_trips_and_consistent_ledger(tmp_path):
    runner, clock, store, cfg = make_runner(tmp_path)
    run_days(runner, clock, cfg, 5)
    legs = store.closed_legs("paper")
    assert len(legs) > 0
    # realized pnl in kv equals the sum over closed legs
    assert abs(runner.realized("paper") - sum(l.pnl_usd for l in legs)) < 1e-6
    # every open leg has a future force-close time and paper balances never go negative
    for l in store.open_legs("paper"):
        assert l.force_close_at > l.entry_ts
    b = runner.balances("paper")
    assert b.quote_usd > 0 and all(v >= -1e-9 for v in b.base.values())
    assert store.get("capital_base") > 0
    # no system errors in a clean simulated run (execution rejects are allowed)
    assert store.count_events("error") == 0


def test_daily_report_and_gate_written(tmp_path):
    runner, clock, store, cfg = make_runner(tmp_path)
    run_days(runner, clock, cfg, 2)
    assert store.get("last_report_day") is not None
    assert store.get("last_gate_report") is not None
    assert runner.mode == "paper"                       # gate can't pass in 2 days


def test_feed_outage_counts_as_errors_and_halts(tmp_path):
    def tweak(cfg):
        cfg.risk.max_consecutive_errors = 3
    runner, clock, store, cfg = make_runner(tmp_path, days_cfg=tweak)
    runner.ref.outage_prob = 1.0                        # every reference call fails
    run_days(runner, clock, cfg, 0.01)                  # ~15 steps
    assert store.get("halt") is not None
    assert store.count_events("error") > 0
    allowed, why = runner.entries_allowed("paper", clock.now())
    assert not allowed and "halted" in why
    # paper halts self-clear after 24h
    clock.sleep(25 * 3600)
    runner.ref.outage_prob = 0.0
    allowed, _ = runner.entries_allowed("paper", clock.now())
    assert allowed


def test_daily_loss_limit_blocks_entries(tmp_path):
    runner, clock, store, cfg = make_runner(tmp_path)
    runner._add_realized("paper", -cfg.risk.daily_loss_limit_usd - 1, clock.now())
    allowed, why = runner.entries_allowed("paper", clock.now())
    assert not allowed and why == "daily loss limit"
    clock.sleep(24 * 3600)
    assert runner.entries_allowed("paper", clock.now())[0]


def test_auto_promote_refuses_without_live_executor(tmp_path):
    def tweak(cfg):
        cfg.gate.auto_promote = True
        cfg.gate.min_days = 0
        cfg.gate.min_round_trips = 0
        cfg.gate.min_win_rate = 0
        cfg.gate.min_profit_factor = 0
        cfg.gate.min_net_pnl_usd = -1e9
    runner, clock, store, cfg = make_runner(tmp_path, days_cfg=tweak)
    run_days(runner, clock, cfg, 2)
    assert runner.mode == "paper"                       # no private key => cannot go live
    assert store.count_events("mode") >= 1


def test_arming_switches_to_live_with_fake_executor(tmp_path):
    runner, clock, store, cfg = make_runner(tmp_path)

    class FakeLive:
        mode = "live"
        def balances(self):
            return 1000.0, {"AAA": 5.0, "BBB": 5.0}
        def execute(self, order, now):
            raise AssertionError("should not execute in this test")
    runner.live_exec_factory = lambda: FakeLive()
    run_days(runner, clock, cfg, 1)
    store.set("live_armed", {"ts": clock.now().isoformat(), "fingerprint": "x"})
    runner.startup()
    assert runner.mode == "live"
    assert store.get("mode") == "live"
    assert store.open_legs("paper") == []               # paper legs closed on switch
