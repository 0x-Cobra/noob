"""Offline simulator: a synthetic reference feed and a synthetic pool, driven by a virtual clock.

Reference: geometric random walk that only moves during the regular session plus an overnight gap
at each open. Pool premium: mean-reverting noise that is wider when the market is closed, with
occasional retail "pushes" (jumps) that decay over hours. Execution price includes pool fee and a
linear price impact against a virtual depth. Our own fills nudge the pool.
"""
from __future__ import annotations

import math
import random
import time as _time
from datetime import datetime, timedelta, timezone

from .chain import Market
from .market_hours import is_regular_open, to_ny
from .reference import ReferenceProvider
from .types import PoolQuote, RefQuote


class Clock:
    def now(self) -> datetime:
        return datetime.now(tz=timezone.utc)

    def sleep(self, seconds: float) -> None:
        _time.sleep(seconds)


class VirtualClock(Clock):
    def __init__(self, start: datetime):
        self._t = start

    def now(self) -> datetime:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)


class SyntheticWorld:
    """Shared state for one symbol: reference price and pool premium, advanced on demand."""

    def __init__(self, symbol: str, start_price: float, clock: VirtualClock, rng: random.Random,
                 daily_vol: float = 0.025, premium_sigma_open: float = 0.004, premium_sigma_closed: float = 0.012,
                 push_prob_per_hour_closed: float = 0.08, push_prob_per_hour_open: float = 0.01,
                 depth_usd: float = 150_000.0, fee: float = 0.003):
        self.symbol = symbol
        self.clock = clock
        self.rng = rng
        self.ref = start_price
        self.ref_ts = clock.now()
        self.premium = 0.0
        self.last_t = clock.now()
        self.daily_vol = daily_vol
        self.sig_open, self.sig_closed = premium_sigma_open, premium_sigma_closed
        self.push_open, self.push_closed = push_prob_per_hour_open, push_prob_per_hour_closed
        self.depth_usd = depth_usd
        self.fee = fee
        self._was_open = is_regular_open(self.last_t)

    def advance(self) -> None:
        now = self.clock.now()
        dt = (now - self.last_t).total_seconds()
        if dt <= 0:
            return
        open_now = is_regular_open(now)
        hours = dt / 3600
        if open_now:
            if not self._was_open:                       # overnight / weekend gap at the open
                self.ref *= math.exp(self.rng.gauss(0, self.daily_vol * 0.8))
            step_vol = self.daily_vol * math.sqrt(hours / 6.5)
            self.ref *= math.exp(self.rng.gauss(0, step_vol))
            self.ref_ts = now
        self._was_open = open_now
        # premium: OU toward 0; slower reversion + wider noise when closed
        kappa = 2.0 if open_now else 0.15                # per hour
        sigma = self.sig_open if open_now else self.sig_closed
        self.premium += -kappa * self.premium * hours + self.rng.gauss(0, sigma * math.sqrt(hours))
        p_push = (self.push_open if open_now else self.push_closed) * hours
        if self.rng.random() < p_push:
            self.premium += self.rng.choice([1, 1, -1]) * self.rng.uniform(0.03, 0.15)
        self.last_t = now

    @property
    def mid(self) -> float:
        return self.ref * (1 + self.premium)

    def impact(self, notional_usd: float) -> float:
        return notional_usd / self.depth_usd

    def sell_exec(self, qty: float) -> float:
        return self.mid * (1 - self.fee - self.impact(qty * self.mid))

    def buy_exec(self, qty: float) -> float:
        return self.mid * (1 + self.fee + self.impact(qty * self.mid))

    def apply_fill(self, side_sell: bool, qty: float) -> None:
        shift = self.impact(qty * self.mid) * 2
        self.premium += -shift if side_sell else shift


class SyntheticReference(ReferenceProvider):
    name = "synthetic"

    def __init__(self, worlds: dict[str, SyntheticWorld], clock: VirtualClock, outage_prob: float = 0.0):
        self.worlds = worlds
        self.clock = clock
        self.outage_prob = outage_prob
        self.rng = random.Random(7)

    def quote(self, symbol: str) -> RefQuote:
        if self.outage_prob and self.rng.random() < self.outage_prob:
            raise RuntimeError("synthetic feed outage")
        w = self.worlds[symbol]
        w.advance()
        return RefQuote(symbol=symbol, price=w.ref, ts=w.ref_ts, source=self.name)


class SyntheticMarket(Market):
    def __init__(self, worlds: dict[str, SyntheticWorld]):
        self.worlds = worlds

    def mid(self, symbol: str) -> float:
        w = self.worlds[symbol]
        w.advance()
        return w.mid

    def quote_sell(self, symbol: str, qty_base: float) -> float | None:
        w = self.worlds[symbol]
        w.advance()
        return w.sell_exec(qty_base) if qty_base > 0 else None

    def quote_buy(self, symbol: str, qty_base: float) -> float | None:
        w = self.worlds[symbol]
        w.advance()
        return w.buy_exec(qty_base) if qty_base > 0 else None

    def pool_quote(self, symbol: str, sell_qty: float, buy_qty: float, now: datetime) -> PoolQuote:
        w = self.worlds[symbol]
        w.advance()
        return PoolQuote(symbol=symbol, pool="0xSIM", fee=int(w.fee * 1_000_000), mid=w.mid,
                         sell_exec=w.sell_exec(sell_qty) if sell_qty > 0 else None, sell_qty=sell_qty,
                         buy_exec=w.buy_exec(buy_qty) if buy_qty > 0 else None, buy_qty=buy_qty,
                         liquidity=int(w.depth_usd), ts=now)

    def on_fill(self, symbol: str, side_sell: bool, qty: float) -> None:
        self.worlds[symbol].apply_fill(side_sell, qty)


def build_world(symbols: list[str], start: datetime, seed: int = 42,
                start_prices: dict[str, float] | None = None, **kw) -> tuple[VirtualClock, SyntheticMarket, SyntheticReference]:
    clock = VirtualClock(start)
    worlds = {s: SyntheticWorld(s, (start_prices or {}).get(s, 100.0 + 50 * i), clock, random.Random(seed + i), **kw)
              for i, s in enumerate(symbols)}
    return clock, SyntheticMarket(worlds), SyntheticReference(worlds, clock)


def ny_label(ts: datetime) -> str:
    return to_ny(ts).strftime("%a %Y-%m-%d %H:%M ET")
