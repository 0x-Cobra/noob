"""Market adapter: Uniswap v3 pools on Robinhood Chain.

Only reads happen here (slot0, liquidity, quoter). Transactions live in execution.LiveExecutor.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from web3 import Web3

from . import abi
from .config import ChainConfig, TickerConfig
from .types import PoolQuote

log = logging.getLogger(__name__)

Q96 = Decimal(2) ** 96
ZERO = "0x0000000000000000000000000000000000000000"


@dataclass
class TokenInfo:
    address: str
    symbol: str
    decimals: int


@dataclass
class PoolInfo:
    address: str
    fee: int
    base: TokenInfo
    quote: TokenInfo
    base_is_token0: bool
    liquidity: int


class Market(ABC):
    """What the strategy needs from a venue. Implemented for Uniswap v3 and for the simulator."""

    @abstractmethod
    def mid(self, symbol: str) -> float: ...

    @abstractmethod
    def quote_sell(self, symbol: str, qty_base: float) -> float | None:
        """Effective quote-per-base price for selling qty_base into the pool. None if unquotable."""

    @abstractmethod
    def quote_buy(self, symbol: str, qty_base: float) -> float | None:
        """Effective quote-per-base price for buying qty_base from the pool. None if unquotable."""

    @abstractmethod
    def pool_quote(self, symbol: str, sell_qty: float, buy_qty: float, now: datetime) -> PoolQuote: ...


def sqrt_price_to_mid(sqrt_price_x96: int, base_is_token0: bool, dec_base: int, dec_quote: int) -> float:
    """Convert slot0.sqrtPriceX96 to a human 'quote per base' price."""
    raw = (Decimal(sqrt_price_x96) / Q96) ** 2      # token1 per token0, raw units
    if base_is_token0:
        return float(raw * Decimal(10) ** (dec_base - dec_quote))
    if raw == 0:
        return float("inf")
    return float((Decimal(1) / raw) * Decimal(10) ** (dec_base - dec_quote))


def to_raw(amount: float, decimals: int) -> int:
    return int(Decimal(str(amount)) * (Decimal(10) ** decimals))


def from_raw(raw: int, decimals: int) -> float:
    return float(Decimal(raw) / (Decimal(10) ** decimals))


class UniswapV3Market(Market):
    def __init__(self, w3: Web3, chain: ChainConfig, tickers: list[TickerConfig]):
        self.w3 = w3
        self.cfg = chain
        for name in ("factory", "quoter_v2", "quote_token"):
            if not getattr(chain, name):
                raise ValueError(f"chain.{name} is not configured")
        self.factory = w3.eth.contract(address=Web3.to_checksum_address(chain.factory), abi=abi.V3_FACTORY)
        self.quoter = w3.eth.contract(address=Web3.to_checksum_address(chain.quoter_v2), abi=abi.QUOTER_V2)
        self.quote_token = self.token_info(chain.quote_token)
        self.tokens: dict[str, TokenInfo] = {}
        self.pools: dict[str, PoolInfo] = {}
        for t in tickers:
            self.tokens[t.symbol] = self.token_info(t.token)
            self.pools[t.symbol] = self.discover_pool(self.tokens[t.symbol])
            p = self.pools[t.symbol]
            log.info("%s: token %s (%s, %dd) pool %s fee=%d liquidity=%d",
                     t.symbol, p.base.address, p.base.symbol, p.base.decimals, p.address, p.fee, p.liquidity)

    # --- setup -----------------------------------------------------------------------------
    def token_info(self, address: str) -> TokenInfo:
        c = self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi.ERC20)
        code = self.w3.eth.get_code(Web3.to_checksum_address(address))
        if not code or code == b"":
            raise ValueError(f"no contract code at token address {address}")
        return TokenInfo(address=Web3.to_checksum_address(address),
                         symbol=c.functions.symbol().call(), decimals=int(c.functions.decimals().call()))

    def discover_pool(self, base: TokenInfo) -> PoolInfo:
        """Pick the base/quote pool with the most in-range liquidity across configured fee tiers."""
        best: PoolInfo | None = None
        for fee in self.cfg.fee_tiers:
            addr = self.factory.functions.getPool(base.address, self.quote_token.address, fee).call()
            if addr == ZERO:
                continue
            pool = self.w3.eth.contract(address=addr, abi=abi.V3_POOL)
            liq = int(pool.functions.liquidity().call())
            token0 = pool.functions.token0().call()
            info = PoolInfo(address=addr, fee=fee, base=base, quote=self.quote_token,
                            base_is_token0=(token0.lower() == base.address.lower()), liquidity=liq)
            if best is None or liq > best.liquidity:
                best = info
        if best is None or best.liquidity == 0:
            raise ValueError(f"no {base.symbol}/{self.quote_token.symbol} v3 pool with liquidity found")
        return best

    # --- reads -----------------------------------------------------------------------------
    def _pool_contract(self, symbol: str):
        return self.w3.eth.contract(address=self.pools[symbol].address, abi=abi.V3_POOL)

    def mid(self, symbol: str) -> float:
        p = self.pools[symbol]
        slot0 = self._pool_contract(symbol).functions.slot0().call()
        return sqrt_price_to_mid(int(slot0[0]), p.base_is_token0, p.base.decimals, p.quote.decimals)

    def quote_sell(self, symbol: str, qty_base: float) -> float | None:
        p = self.pools[symbol]
        amount_in = to_raw(qty_base, p.base.decimals)
        if amount_in <= 0:
            return None
        try:
            out = self.quoter.functions.quoteExactInputSingle(
                (p.base.address, p.quote.address, amount_in, p.fee, 0)).call()
        except Exception as e:  # insufficient liquidity etc.
            log.debug("quote_sell %s failed: %s", symbol, e)
            return None
        amount_out = int(out[0])
        return from_raw(amount_out, p.quote.decimals) / qty_base if amount_out > 0 else None

    def quote_buy(self, symbol: str, qty_base: float) -> float | None:
        p = self.pools[symbol]
        amount_out = to_raw(qty_base, p.base.decimals)
        if amount_out <= 0:
            return None
        try:
            res = self.quoter.functions.quoteExactOutputSingle(
                (p.quote.address, p.base.address, amount_out, p.fee, 0)).call()
        except Exception as e:
            log.debug("quote_buy %s failed: %s", symbol, e)
            return None
        amount_in = int(res[0])
        return from_raw(amount_in, p.quote.decimals) / qty_base if amount_in > 0 else None

    def pool_quote(self, symbol: str, sell_qty: float, buy_qty: float, now: datetime) -> PoolQuote:
        p = self.pools[symbol]
        return PoolQuote(symbol=symbol, pool=p.address, fee=p.fee, mid=self.mid(symbol),
                         sell_exec=self.quote_sell(symbol, sell_qty) if sell_qty > 0 else None, sell_qty=sell_qty,
                         buy_exec=self.quote_buy(symbol, buy_qty) if buy_qty > 0 else None, buy_qty=buy_qty,
                         liquidity=int(self._pool_contract(symbol).functions.liquidity().call()), ts=now)

    def erc20_balance(self, token: TokenInfo, owner: str) -> float:
        c = self.w3.eth.contract(address=token.address, abi=abi.ERC20)
        return from_raw(int(c.functions.balanceOf(Web3.to_checksum_address(owner)).call()), token.decimals)


def connect(rpc_url: str, expected_chain_id: int, timeout: float = 20.0) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
    if not w3.is_connected():
        raise ConnectionError(f"cannot connect to RPC {rpc_url}")
    cid = w3.eth.chain_id
    if cid != expected_chain_id:
        raise ConnectionError(f"RPC chain id {cid} != expected {expected_chain_id}")
    return w3


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
