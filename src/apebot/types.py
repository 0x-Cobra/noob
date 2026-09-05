"""Shared value types passed between modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    SELL = "sell"   # we sold base tokens into the pool (short the premium)
    BUY = "buy"     # we bought base tokens from the pool (long the discount)


class LegStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class RefQuote:
    symbol: str
    price: float
    ts: datetime            # time of the last trade the price reflects
    prev_close: float | None = None
    source: str = ""


@dataclass
class PoolQuote:
    """Snapshot of one pool for one tranche size."""
    symbol: str
    pool: str
    fee: int                    # Uniswap fee tier in hundredths of a bip (3000 = 0.30%)
    mid: float                  # quote per base from slot0
    sell_exec: float | None     # effective price if we sell `sell_qty` base (after impact)
    sell_qty: float
    buy_exec: float | None      # effective price if we buy `buy_qty` base (after impact)
    buy_qty: float
    liquidity: int
    ts: datetime


@dataclass
class Tick:
    ts: datetime
    symbol: str
    ref: RefQuote | None
    pool: PoolQuote | None
    market_open: bool
    extended_open: bool
    ref_stale: bool

    @property
    def sell_premium(self) -> float | None:
        if self.ref is None or self.pool is None or self.pool.sell_exec is None:
            return None
        return self.pool.sell_exec / self.ref.price - 1.0

    @property
    def buy_discount(self) -> float | None:
        if self.ref is None or self.pool is None or self.pool.buy_exec is None:
            return None
        return 1.0 - self.pool.buy_exec / self.ref.price

    @property
    def mid_premium(self) -> float | None:
        if self.ref is None or self.pool is None:
            return None
        return self.pool.mid / self.ref.price - 1.0


@dataclass
class Leg:
    id: int | None
    symbol: str
    side: Side
    qty: float
    entry_px: float
    entry_ref: float
    entry_ts: datetime
    force_close_at: datetime
    status: LegStatus = LegStatus.OPEN
    exit_px: float | None = None
    exit_ts: datetime | None = None
    exit_reason: str | None = None
    fees_usd: float = 0.0
    pnl_usd: float | None = None
    mode: str = "paper"

    def unrealized(self, pool: PoolQuote) -> float | None:
        if self.side == Side.SELL:
            px = pool.buy_exec or pool.mid
            return (self.entry_px - px) * self.qty
        px = pool.sell_exec or pool.mid
        return (px - self.entry_px) * self.qty


@dataclass
class Order:
    symbol: str
    side: Side          # SELL = base->quote, BUY = quote->base
    qty_base: float     # for SELL: base to sell; for BUY: base we want (approx, exact-out not used)
    expected_px: float  # quoter price used for slippage floor
    reason: str
    leg_id: int | None = None   # set when the order closes an existing leg


@dataclass
class Fill:
    order: Order
    qty_base: float
    px: float           # effective quote per base actually achieved
    fees_usd: float
    tx_hash: str | None = None
    ts: datetime | None = None


@dataclass
class Balances:
    quote_usd: float
    base: dict[str, float] = field(default_factory=dict)
