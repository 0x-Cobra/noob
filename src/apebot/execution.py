"""Order execution: paper (simulated fills from real quotes) and live (Uniswap v3 SwapRouter02)."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime

from web3 import Web3

from . import abi
from .chain import Market, UniswapV3Market, from_raw, to_raw
from .config import ChainConfig, StrategyConfig
from .types import Fill, Order, Side

log = logging.getLogger(__name__)

MAX_UINT256 = 2**256 - 1


class ExecutionError(RuntimeError):
    pass


class Executor(ABC):
    mode: str

    @abstractmethod
    def execute(self, order: Order, now: datetime) -> Fill: ...


class PaperExecutor(Executor):
    """Fills at a fresh quoter price (which already includes pool fee + price impact),
    shaded by `slippage_buffer` against us, plus a flat gas cost."""
    mode = "paper"

    def __init__(self, market: Market, strat: StrategyConfig, chain: ChainConfig):
        self.market = market
        self.strat = strat
        self.chain = chain

    def execute(self, order: Order, now: datetime) -> Fill:
        if order.side == Side.SELL:
            px = self.market.quote_sell(order.symbol, order.qty_base)
            if px is None:
                raise ExecutionError(f"paper: no sell quote for {order.symbol}")
            px *= (1 - self.strat.slippage_buffer)
            floor = order.expected_px * (1 - self.strat.slippage_buffer * 2)
            if px < floor:
                raise ExecutionError(f"paper: sell price {px:.4f} below floor {floor:.4f} (moved)")
        else:
            px = self.market.quote_buy(order.symbol, order.qty_base)
            if px is None:
                raise ExecutionError(f"paper: no buy quote for {order.symbol}")
            px *= (1 + self.strat.slippage_buffer)
            cap = order.expected_px * (1 + self.strat.slippage_buffer * 2)
            if px > cap:
                raise ExecutionError(f"paper: buy price {px:.4f} above cap {cap:.4f} (moved)")
        return Fill(order=order, qty_base=order.qty_base, px=px, fees_usd=self.chain.gas_cost_usd_per_swap,
                    tx_hash=None, ts=now)


class LiveExecutor(Executor):
    mode = "live"

    def __init__(self, market: UniswapV3Market, strat: StrategyConfig, chain: ChainConfig, private_key: str):
        if not chain.swap_router02:
            raise ValueError("chain.swap_router02 is not configured")
        self.market = market
        self.strat = strat
        self.chain = chain
        self.w3 = market.w3
        self.account = self.w3.eth.account.from_key(private_key)
        self.router = self.w3.eth.contract(address=Web3.to_checksum_address(chain.swap_router02),
                                           abi=abi.SWAP_ROUTER02)
        self._approved: set[str] = set()

    @property
    def address(self) -> str:
        return self.account.address

    # --- helpers ---------------------------------------------------------------------------
    def _erc20(self, address: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi.ERC20)

    def _ensure_allowance(self, token: str, amount: int) -> None:
        if token in self._approved:
            return
        c = self._erc20(token)
        current = int(c.functions.allowance(self.address, self.router.address).call())
        if current >= amount:
            self._approved.add(token)
            return
        log.info("approving router for %s", token)
        tx = c.functions.approve(self.router.address, MAX_UINT256).build_transaction(self._tx_fields())
        self._send(tx)
        self._approved.add(token)

    def _tx_fields(self) -> dict:
        base_fee = self.w3.eth.get_block("latest").get("baseFeePerGas")
        if base_fee is None:
            gas_price = self.w3.eth.gas_price
            if gas_price / 1e9 > self.chain.max_gas_gwei:
                raise ExecutionError(f"gas price {gas_price/1e9:.2f} gwei above limit")
            fees = {"gasPrice": gas_price}
        else:
            if base_fee / 1e9 > self.chain.max_gas_gwei:
                raise ExecutionError(f"base fee {base_fee/1e9:.2f} gwei above limit")
            prio = max(self.w3.eth.max_priority_fee, 1)
            fees = {"maxFeePerGas": int(base_fee * 2 + prio), "maxPriorityFeePerGas": int(prio)}
        return {"from": self.address, "nonce": self.w3.eth.get_transaction_count(self.address, "pending"),
                "chainId": self.chain.chain_id, **fees}

    def _send(self, tx: dict):
        if "gas" not in tx:
            tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.25)
        signed = self.account.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=self.chain.tx_deadline_sec + 60)
        if receipt["status"] != 1:
            raise ExecutionError(f"tx {h.hex()} reverted")
        return receipt

    def _gas_usd(self, receipt) -> float:
        gas_eth = receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0) / 1e18
        return gas_eth * self.chain.eth_price_usd

    # --- execute ---------------------------------------------------------------------------
    def execute(self, order: Order, now: datetime) -> Fill:
        p = self.market.pools[order.symbol]
        base_c, quote_c = self._erc20(p.base.address), self._erc20(p.quote.address)
        base_before = int(base_c.functions.balanceOf(self.address).call())
        quote_before = int(quote_c.functions.balanceOf(self.address).call())
        deadline = int(time.time()) + self.chain.tx_deadline_sec

        if order.side == Side.SELL:
            amount_in = to_raw(order.qty_base, p.base.decimals)
            if amount_in > base_before:
                raise ExecutionError(f"insufficient {p.base.symbol} balance for sell")
            min_out = to_raw(order.qty_base * order.expected_px * (1 - self.strat.slippage_buffer), p.quote.decimals)
            self._ensure_allowance(p.base.address, amount_in)
            fn = self.router.functions.exactInputSingle(
                (p.base.address, p.quote.address, p.fee, self.address, amount_in, min_out, 0))
        else:
            amount_out = to_raw(order.qty_base, p.base.decimals)
            max_in = to_raw(order.qty_base * order.expected_px * (1 + self.strat.slippage_buffer), p.quote.decimals)
            if max_in > quote_before:
                raise ExecutionError(f"insufficient {p.quote.symbol} balance for buy")
            self._ensure_allowance(p.quote.address, max_in)
            fn = self.router.functions.exactOutputSingle(
                (p.quote.address, p.base.address, p.fee, self.address, amount_out, max_in, 0))

        tx = fn.build_transaction({**self._tx_fields(), "value": 0})
        # SwapRouter02 has no deadline param on single-hop calls; we bound our own wait instead.
        receipt = self._send(tx)
        base_after = int(base_c.functions.balanceOf(self.address).call())
        quote_after = int(quote_c.functions.balanceOf(self.address).call())
        d_base = from_raw(abs(base_after - base_before), p.base.decimals)
        d_quote = from_raw(abs(quote_after - quote_before), p.quote.decimals)
        if d_base <= 0:
            raise ExecutionError("swap mined but base balance did not change")
        px = d_quote / d_base
        fill = Fill(order=order, qty_base=d_base, px=px, fees_usd=self._gas_usd(receipt),
                    tx_hash=receipt["transactionHash"].hex(), ts=now)
        log.info("LIVE FILL %s %s qty=%.6f px=%.4f tx=%s (deadline was %d)",
                 order.side.value, order.symbol, d_base, px, fill.tx_hash, deadline)
        return fill

    def balances(self) -> tuple[float, dict[str, float]]:
        quote = self.market.erc20_balance(self.market.quote_token, self.address)
        base = {sym: self.market.erc20_balance(tok, self.address) for sym, tok in self.market.tokens.items()}
        return quote, base
