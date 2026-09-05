"""Premium/discount mean-reversion on tokenized-stock pools. Pure decision logic, no I/O.

SELL leg:  pool trades at a premium to the reference  -> sell inventory, rebuy when it reverts.
BUY  leg:  pool trades at a discount to the reference -> buy with quote, sell back when it reverts.
Every leg has a stop (pool moves further against us) and a force-close time (after next open).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import StrategyConfig
from .market_hours import next_regular_open
from .types import Balances, Leg, Order, Side, Tick


@dataclass
class Decision:
    orders: list[Order]
    notes: list[str]


def force_close_time(now: datetime, market_open: bool, cfg: StrategyConfig) -> datetime:
    if market_open:
        return now + timedelta(minutes=cfg.intraday_max_hold_min)
    return next_regular_open(now) + timedelta(minutes=cfg.force_close_after_open_min)


def exit_orders(tick: Tick, legs: list[Leg], now: datetime, cfg: StrategyConfig) -> list[Order]:
    out: list[Order] = []
    pool, ref = tick.pool, tick.ref
    if pool is None:
        return out
    # A stale reference during market hours means "don't trust the reference"; the last close
    # while the market is closed is legitimately the best reference we have.
    ref_ok = ref is not None and (not tick.ref_stale or not tick.market_open)
    for leg in legs:
        if leg.symbol != tick.symbol:
            continue
        if leg.side == Side.SELL:
            px = pool.buy_exec
            if px is None:
                continue
            reason = None
            if now >= leg.force_close_at:
                reason = "force"
            elif px >= leg.entry_px * (1 + cfg.stop_loss):
                reason = "stop"
            elif ref_ok and px <= ref.price * (1 + cfg.exit_band):
                reason = "revert"
            if reason:
                out.append(Order(tick.symbol, Side.BUY, leg.qty, px, reason, leg_id=leg.id))
        else:
            px = pool.sell_exec
            if px is None:
                continue
            reason = None
            if now >= leg.force_close_at:
                reason = "force"
            elif px <= leg.entry_px * (1 - cfg.stop_loss):
                reason = "stop"
            elif ref_ok and px >= ref.price * (1 - cfg.exit_band):
                reason = "revert"
            if reason:
                out.append(Order(tick.symbol, Side.SELL, leg.qty, px, reason, leg_id=leg.id))
    return out


def entry_order(tick: Tick, legs: list[Leg], balances: Balances, now: datetime, cfg: StrategyConfig,
                last_trade_ts: datetime | None) -> tuple[Order | None, str | None]:
    pool, ref = tick.pool, tick.ref
    if pool is None or ref is None:
        return None, "no data"
    if tick.ref_stale:
        return None, "reference stale"
    sym_legs = [l for l in legs if l.symbol == tick.symbol]
    if len(sym_legs) >= cfg.max_legs_per_ticker:
        return None, "max legs"
    if last_trade_ts is not None and (now - last_trade_ts).total_seconds() < cfg.cooldown_sec:
        return None, "cooldown"
    extra = 0.0 if tick.market_open else cfg.closed_market_extra

    # Never stack both directions on one ticker.
    sides_open = {l.side for l in sym_legs}

    if cfg.enable_sell_side and Side.BUY not in sides_open:
        prem = tick.sell_premium
        if prem is not None and prem >= cfg.sell_entry_premium + extra:
            qty = min(pool.sell_qty, balances.base.get(tick.symbol, 0.0))
            if qty <= 0 or qty * pool.sell_exec < cfg.tranche_usd * 0.25:
                return None, "no inventory to sell"
            return Order(tick.symbol, Side.SELL, qty, pool.sell_exec, f"premium {prem:.3%}"), None

    if cfg.enable_buy_side and Side.SELL not in sides_open:
        disc = tick.buy_discount
        if disc is not None and disc >= cfg.buy_entry_discount + extra:
            affordable = balances.quote_usd / pool.buy_exec if pool.buy_exec else 0.0
            qty = min(pool.buy_qty, affordable)
            if qty <= 0 or qty * pool.buy_exec < cfg.tranche_usd * 0.25:
                return None, "no quote balance to buy"
            return Order(tick.symbol, Side.BUY, qty, pool.buy_exec, f"discount {disc:.3%}"), None

    return None, None


def decide(tick: Tick, legs: list[Leg], balances: Balances, now: datetime, cfg: StrategyConfig,
           last_trade_ts: datetime | None, entries_allowed: bool) -> Decision:
    notes: list[str] = []
    orders = exit_orders(tick, legs, now, cfg)
    if orders:
        return Decision(orders, notes)          # exits first; never enter and exit in the same tick
    if not entries_allowed:
        return Decision([], ["entries halted"])
    order, why = entry_order(tick, legs, balances, now, cfg, last_trade_ts)
    if order:
        orders.append(order)
    elif why:
        notes.append(why)
    return Decision(orders, notes)


def leg_pnl(leg: Leg, exit_px: float, exit_fees_usd: float) -> float:
    gross = (leg.entry_px - exit_px) * leg.qty if leg.side == Side.SELL else (exit_px - leg.entry_px) * leg.qty
    return gross - leg.fees_usd - exit_fees_usd
