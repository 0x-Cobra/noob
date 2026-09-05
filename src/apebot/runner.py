"""Main loop. Runs the same strategy in paper or live mode, evaluates the promotion gate daily,
and enforces risk halts. All dependencies are injected so the simulator and tests can drive it."""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta, timezone

from . import gate as gate_mod
from .chain import Market
from .config import Config, Secrets
from .execution import ExecutionError, Executor, LiveExecutor, PaperExecutor
from .market_hours import is_extended_open, is_regular_open
from .notify import Notifier
from .reference import ReferenceProvider
from .sim import Clock
from .store import Store
from .strategy import decide, force_close_time, leg_pnl
from .types import Balances, Fill, Leg, LegStatus, Order, PoolQuote, RefQuote, Side, Tick

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, cfg: Config, store: Store, market: Market, ref: ReferenceProvider,
                 paper_exec: Executor, notifier: Notifier, clock: Clock,
                 live_exec_factory=None, on_fill=None):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.ref = ref
        self.notifier = notifier
        self.clock = clock
        self.executors: dict[str, Executor] = {"paper": paper_exec}
        self.live_exec_factory = live_exec_factory     # callable -> LiveExecutor, or None
        self.on_fill = on_fill                          # simulator hook
        self.last_trade_ts: dict[str, datetime] = {}
        self.last_mid: dict[str, float] = {}
        self.consecutive_errors = 0
        self.mode: str = store.get("mode", "paper")
        self._last_report_day: str | None = store.get("last_report_day")
        self._paper_balances = self._load_paper_balances()
        self._last_pool: dict[str, PoolQuote] = {}

    # ------------------------------------------------------------------ balances / state
    def _load_paper_balances(self) -> Balances:
        saved = self.store.get("paper_balances")
        if saved:
            return Balances(quote_usd=saved["quote_usd"], base=dict(saved["base"]))
        b = Balances(quote_usd=self.cfg.paper_quote_usd,
                     base={t.symbol: t.paper_inventory for t in self.cfg.enabled_tickers})
        self._save_paper_balances(b)
        return b

    def _save_paper_balances(self, b: Balances) -> None:
        self.store.set("paper_balances", {"quote_usd": b.quote_usd, "base": b.base})

    def balances(self, mode: str) -> Balances:
        if mode == "live":
            ex = self.executors.get("live")
            assert isinstance(ex, LiveExecutor)
            quote, base = ex.balances()
            return Balances(quote_usd=quote, base=base)
        return self._paper_balances

    def capital_base(self, refs: dict[str, RefQuote]) -> float:
        cb = self.store.get("capital_base")
        if cb:
            return float(cb)
        if len(refs) < len(self.cfg.enabled_tickers):
            return 0.0
        b = self._load_paper_balances()
        cb = b.quote_usd + sum(b.base.get(s, 0.0) * q.price for s, q in refs.items())
        self.store.set("capital_base", cb)
        return cb

    def realized(self, mode: str) -> float:
        return float(self.store.get(f"realized_cum_{mode}", 0.0))

    def _add_realized(self, mode: str, pnl: float, now: datetime) -> None:
        self.store.set(f"realized_cum_{mode}", self.realized(mode) + pnl)
        day = now.strftime("%Y-%m-%d")
        d = self.store.get(f"day_pnl_{mode}", {"day": day, "pnl": 0.0})
        if d["day"] != day:
            d = {"day": day, "pnl": 0.0}
        d["pnl"] += pnl
        self.store.set(f"day_pnl_{mode}", d)

    def day_pnl(self, mode: str, now: datetime) -> float:
        d = self.store.get(f"day_pnl_{mode}")
        return d["pnl"] if d and d["day"] == now.strftime("%Y-%m-%d") else 0.0

    # ------------------------------------------------------------------ halts
    def entries_allowed(self, mode: str, now: datetime) -> tuple[bool, str | None]:
        sticky = self.store.get("halt")
        if sticky:
            if sticky["mode"] == "paper" and (now - datetime.fromisoformat(sticky["ts"])) > timedelta(hours=24):
                self.store.delete("halt")            # paper halts self-clear after a day
                self.event(now, "info", "halt", "paper halt auto-cleared after 24h")
            else:
                return False, f"halted: {sticky['reason']}"
        if self.day_pnl(mode, now) <= -self.cfg.risk.daily_loss_limit_usd:
            return False, "daily loss limit"
        return True, None

    def halt(self, now: datetime, reason: str) -> None:
        if self.store.get("halt"):
            return
        self.store.set("halt", {"ts": now.isoformat(), "reason": reason, "mode": self.mode})
        self.event(now, "error", "halt", reason)
        self.notifier.send(f"⛔ apebot HALTED ({self.mode}): {reason}", "error")
        if self.mode == "live" and self.cfg.risk.demote_live_to_paper_on_halt:
            self.set_mode("paper", now, f"demoted after halt: {reason}")

    def event(self, now: datetime, level: str, kind: str, msg: str) -> None:
        self.store.add_event(now, level, kind, msg)
        getattr(log, level if level in ("info", "warning", "error") else "info")("%s: %s", kind, msg)

    # ------------------------------------------------------------------ mode switching
    def set_mode(self, mode: str, now: datetime, why: str) -> None:
        if mode == self.mode:
            return
        if mode == "live":
            if self.live_exec_factory is None:
                self.event(now, "error", "mode", "cannot go live: no private key / live executor")
                return
            try:
                if "live" not in self.executors:
                    self.executors["live"] = self.live_exec_factory()
                ex = self.executors["live"]
                quote, base = ex.balances()
                if quote < self.cfg.strategy.tranche_usd and not any(v > 0 for v in base.values()):
                    raise RuntimeError(f"wallet has no inventory and only {quote:.2f} quote")
            except Exception as e:
                self.event(now, "error", "mode", f"cannot go live: {e}")
                self.notifier.send(f"⚠️ apebot: gate passed but cannot go live: {e}", "warning")
                return
            self._close_all_legs("paper", now, "mode_switch")
        self.mode = mode
        self.store.set("mode", mode)
        self.store.set("mode_since", now.isoformat())
        self.event(now, "warning", "mode", f"mode -> {mode}: {why}")
        self.notifier.send(f"🔁 apebot mode -> {mode.upper()} ({why})", "warning")

    def _close_all_legs(self, mode: str, now: datetime, reason: str) -> None:
        for leg in self.store.open_legs(mode):
            pool = self._last_pool.get(leg.symbol)
            if pool is None:
                continue
            side = Side.BUY if leg.side == Side.SELL else Side.SELL
            px = (pool.buy_exec if side == Side.BUY else pool.sell_exec) or pool.mid
            try:
                self._execute(Order(leg.symbol, side, leg.qty, px, reason, leg_id=leg.id), mode, now, {leg.id: leg})
            except ExecutionError as e:
                self.event(now, "warning", "exec_reject", f"close {leg.symbol} on {reason} failed: {e}")

    # ------------------------------------------------------------------ one step
    def step(self) -> None:
        now = self.clock.now()
        refs: dict[str, RefQuote] = {}
        open_legs = self.store.open_legs()
        legs_by_id = {l.id: l for l in open_legs}
        allowed, why = self.entries_allowed(self.mode, now)
        self._step_error = False
        with self.store.tx():
            for t in self.cfg.enabled_tickers:
                try:
                    tick = self._build_tick(t.symbol, t.reference_symbol, now)
                    if tick.ref:
                        refs[t.symbol] = tick.ref
                    if tick.pool:
                        self._last_pool[t.symbol] = tick.pool
                    self.store.add_tick(tick)
                    if tick.pool is None:
                        continue
                    sym_legs = [l for l in open_legs if l.symbol == t.symbol]
                    d = decide(tick, sym_legs, self.balances(self.mode), now, self.cfg.strategy,
                               self.last_trade_ts.get(t.symbol), allowed)
                    for order in d.orders:
                        mode = legs_by_id[order.leg_id].mode if order.leg_id else self.mode
                        try:
                            self._execute(order, mode, now, legs_by_id)
                            self.consecutive_errors = 0
                        except ExecutionError as e:
                            self.event(now, "warning", "exec_reject", f"{order.side.value} {order.symbol}: {e}")
                except Exception as e:  # feed / rpc problems: log, count, keep going
                    self._step_error = True
                    self.event(now, "error", "error", f"{t.symbol}: {type(e).__name__}: {e}")
                    log.debug(traceback.format_exc())
            self._snapshot(now)
        self.consecutive_errors = self.consecutive_errors + 1 if self._step_error else 0
        if self.consecutive_errors >= self.cfg.risk.max_consecutive_errors:
            self.halt(now, f"{self.consecutive_errors} consecutive error steps")
        self._daily(now, refs)

    def _build_tick(self, symbol: str, ref_symbol: str, now: datetime) -> Tick:
        ext = is_extended_open(now)
        reg = is_regular_open(now)
        ref: RefQuote | None = None
        stale = False
        try:
            ref = self.ref.quote(ref_symbol)
            ref.symbol = symbol
            age = (now - ref.ts).total_seconds()
            stale = reg and age > self.cfg.reference.max_staleness_sec
        except Exception as e:
            self._step_error = True
            self.event(now, "error", "error", f"{symbol}: reference unavailable: {e}")
        px = ref.price if ref else self.last_mid.get(symbol)
        qty = round(self.cfg.strategy.tranche_usd / px, 6) if px else 0.0
        pool = self.market.pool_quote(symbol, qty, qty, now)
        self.last_mid[symbol] = pool.mid
        return Tick(ts=now, symbol=symbol, ref=ref, pool=pool, market_open=reg, extended_open=ext, ref_stale=stale)

    def _execute(self, order: Order, mode: str, now: datetime, legs_by_id: dict[int, Leg]) -> Fill:
        ex = self.executors.get(mode)
        if ex is None:
            raise ExecutionError(f"no {mode} executor loaded (is APEBOT_PRIVATE_KEY set?)")
        fill = ex.execute(order, now)
        self.store.add_fill(fill, mode)
        self.last_trade_ts[order.symbol] = now
        if self.on_fill:
            self.on_fill(order.symbol, order.side == Side.SELL, fill.qty_base)
        if mode == "paper":
            b = self._paper_balances
            if order.side == Side.SELL:
                b.base[order.symbol] = b.base.get(order.symbol, 0.0) - fill.qty_base
                b.quote_usd += fill.qty_base * fill.px - fill.fees_usd
            else:
                b.base[order.symbol] = b.base.get(order.symbol, 0.0) + fill.qty_base
                b.quote_usd -= fill.qty_base * fill.px + fill.fees_usd
            self._save_paper_balances(b)
        if order.leg_id:
            leg = legs_by_id[order.leg_id]
            leg.exit_px, leg.exit_ts, leg.exit_reason = fill.px, now, order.reason
            leg.pnl_usd = leg_pnl(leg, fill.px, fill.fees_usd)
            leg.fees_usd += fill.fees_usd
            leg.status = LegStatus.CLOSED
            self.store.close_leg(leg)
            self._add_realized(mode, leg.pnl_usd, now)
            self.event(now, "info", "trade", f"[{mode}] close {leg.side.value} {leg.symbol} qty={leg.qty:.4f} "
                       f"entry={leg.entry_px:.3f} exit={fill.px:.3f} pnl={leg.pnl_usd:+.2f} ({order.reason})")
            if mode == "live":
                self.notifier.send(f"✅ LIVE close {leg.side.value} {leg.symbol} pnl {leg.pnl_usd:+.2f} ({order.reason})")
        else:
            ref_px = self._last_ref_price(order.symbol)
            leg = Leg(id=None, symbol=order.symbol, side=order.side, qty=fill.qty_base, entry_px=fill.px,
                      entry_ref=ref_px, entry_ts=now, fees_usd=fill.fees_usd,
                      force_close_at=force_close_time(now, is_regular_open(now), self.cfg.strategy), mode=mode)
            self.store.add_leg(leg)
            self.event(now, "info", "trade", f"[{mode}] open {leg.side.value} {leg.symbol} qty={leg.qty:.4f} "
                       f"px={leg.entry_px:.3f} ref={ref_px:.3f} ({order.reason})")
            if mode == "live":
                self.notifier.send(f"🟢 LIVE open {leg.side.value} {leg.symbol} qty {leg.qty:.4f} "
                                   f"@ {leg.entry_px:.3f} ({order.reason})")
        return fill

    def _last_ref_price(self, symbol: str) -> float:
        rows = self.store.recent_ticks(symbol, 1)
        return float(rows[0]["ref"]) if rows and rows[0]["ref"] is not None else self.last_mid.get(symbol, 0.0)

    def _snapshot(self, now: datetime) -> None:
        for mode in self.executors:
            unreal = 0.0
            for leg in self.store.open_legs(mode):
                pool = self._last_pool.get(leg.symbol)
                if pool:
                    unreal += leg.unrealized(pool) or 0.0
            try:
                b = self.balances(mode)
            except Exception as e:
                self.event(now, "warning", "feed", f"balance read failed ({mode}): {e}")
                continue
            inv = sum(q * self.last_mid.get(s, 0.0) for s, q in b.base.items())
            realized = self.realized(mode)
            self.store.add_equity(now, mode, realized, unreal, b.quote_usd, inv)
            self._check_drawdown(mode, realized + unreal, now)

    def _check_drawdown(self, mode: str, equity: float, now: datetime) -> None:
        peak = float(self.store.get(f"equity_peak_{mode}", 0.0))
        if equity > peak:
            self.store.set(f"equity_peak_{mode}", equity)
            peak = equity
        cb = float(self.store.get("capital_base", 0.0))
        if cb > 0 and (peak - equity) / cb > self.cfg.risk.max_drawdown_halt_pct and mode == self.mode:
            self.halt(now, f"drawdown {(peak - equity) / cb:.2%} of capital in {mode}")

    # ------------------------------------------------------------------ daily
    def _daily(self, now: datetime, refs: dict[str, RefQuote]) -> None:
        day = now.strftime("%Y-%m-%d")
        if self._last_report_day == day or now.hour < self.cfg.notify.daily_report_hour_utc:
            return
        self._last_report_day = day
        self.store.set("last_report_day", day)
        cb = self.capital_base(refs)
        report = gate_mod.evaluate(self.store, self.cfg.gate, cb, now)
        self.store.set("last_gate_report", report.to_json())
        m = report.metrics
        text = (f"📊 apebot daily [{self.mode}] {day}\n"
                f"paper realized: {self.realized('paper'):+.2f}  live realized: {self.realized('live'):+.2f}\n"
                f"round trips: {m['round_trips']}  win rate: {m['win_rate']:.0%}  PF: {m['profit_factor']}\n"
                f"max DD: {m['max_drawdown_pct']:.2%}  days: {m['days']}  open legs: {m['open_legs']}\n"
                f"gate: {'PASS ✅' if report.passed else 'not yet: ' + '; '.join(report.failures[:3])}")
        self.notifier.send(text)
        self._maybe_promote(report, now)

    def _maybe_promote(self, report: gate_mod.GateReport, now: datetime) -> None:
        if self.mode == "live":
            return
        armed = self.store.get("live_armed")
        if armed:
            self.set_mode("live", now, f"armed manually at {armed['ts']}")
            return
        if report.passed and self.cfg.gate.auto_promote:
            self.store.set("live_armed", {"ts": now.isoformat(), "fingerprint": report.fingerprint(), "auto": True})
            self.set_mode("live", now, f"gate passed (auto_promote) fp={report.fingerprint()}")
        elif report.passed:
            self.notifier.send("✅ Gate PASSED. Run `apebot arm-live` to enable live trading (auto_promote is off).")

    # ------------------------------------------------------------------ loop
    def startup(self) -> None:
        now = self.clock.now()
        if self.store.get("live_armed") and self.mode != "live":
            self.set_mode("live", now, "armed at startup")
        self.event(now, "info", "start", f"apebot started in {self.mode} mode; tickers="
                   f"{[t.symbol for t in self.cfg.enabled_tickers]}")
        self.notifier.send(f"🚀 apebot started ({self.mode})")

    def run_forever(self) -> None:
        self.startup()
        while True:
            try:
                self.step()
            except Exception as e:
                log.error("step failed: %s\n%s", e, traceback.format_exc())
                self.consecutive_errors += 1
                if self.consecutive_errors >= self.cfg.risk.max_consecutive_errors:
                    self.halt(self.clock.now(), f"{self.consecutive_errors} consecutive step failures: {e}")
            self.clock.sleep(self.cfg.poll_interval_sec)


def build_runner(cfg: Config, secrets: Secrets) -> Runner:
    """Wire the real thing: RPC, Uniswap v3, reference provider, Telegram."""
    from .chain import UniswapV3Market, connect
    from .reference import build_reference_provider

    rpc = secrets.rpc_url_override or cfg.chain.rpc_url
    w3 = connect(rpc, cfg.chain.chain_id)
    market = UniswapV3Market(w3, cfg.chain, cfg.enabled_tickers)
    ref = build_reference_provider(cfg.reference.provider, secrets, cfg.reference.poll_min_interval_sec)
    store = Store(cfg.db_path)
    notifier = Notifier(secrets.telegram_bot_token, secrets.telegram_chat_id, cfg.notify.telegram_enabled)
    paper = PaperExecutor(market, cfg.strategy, cfg.chain)
    live_factory = None
    if secrets.private_key:
        live_factory = lambda: LiveExecutor(market, cfg.strategy, cfg.chain, secrets.private_key)  # noqa: E731
    return Runner(cfg, store, market, ref, paper, notifier, Clock(), live_exec_factory=live_factory)


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
