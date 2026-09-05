"""Configuration model. Loaded from YAML, secrets from environment variables."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

Mode = Literal["paper", "live"]


class TickerConfig(BaseModel):
    symbol: str                      # e.g. "NVDA" (used for reference price lookups)
    token: str                       # Stock Token ERC-20 address on Robinhood Chain
    ref_symbol: str | None = None    # override for the reference-price provider; defaults to symbol
    paper_inventory: float = 10.0    # tokens the paper account starts with
    enabled: bool = True

    @property
    def reference_symbol(self) -> str:
        return self.ref_symbol or self.symbol


class ChainConfig(BaseModel):
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    chain_id: int = 4663
    # Uniswap v3 periphery on Robinhood Chain. Fill from
    # https://developers.uniswap.org/docs/protocols/v3/deployments/v3-robinhood-chain-deployments
    factory: str = ""
    quoter_v2: str = ""
    swap_router02: str = ""
    quote_token: str = ""            # USDG (or whichever stable the pools use)
    fee_tiers: list[int] = Field(default_factory=lambda: [500, 3000, 10000])
    gas_cost_usd_per_swap: float = 0.05   # rough L2 swap cost used by the paper executor
    eth_price_usd: float = 3000.0         # only used to express live gas spend in USD
    max_gas_gwei: float = 5.0             # live: refuse to send if base fee above this
    tx_deadline_sec: int = 120


class ReferenceConfig(BaseModel):
    provider: Literal["finnhub", "polygon", "synthetic"] = "finnhub"
    max_staleness_sec: int = 900     # during (extended) market hours, reject older quotes
    poll_min_interval_sec: int = 10  # rate-limit protection


class StrategyConfig(BaseModel):
    # Entry: execution premium (after price impact) needed to sell inventory into the pool.
    sell_entry_premium: float = 0.03
    # Entry: execution discount needed to buy from the pool with quote currency.
    buy_entry_discount: float = 0.03
    # Extra threshold applied while the reference market is closed (stale reference).
    closed_market_extra: float = 0.01
    # Exit: close the leg when the pool has reverted to within this band of reference.
    exit_band: float = 0.005
    # Stop: close the leg if the pool moves this much further against us versus entry.
    stop_loss: float = 0.06
    # Force close this many minutes after the next regular-session open following entry.
    force_close_after_open_min: int = 45
    # If entered while the regular session is open, force close after this many minutes.
    intraday_max_hold_min: int = 120
    # Sizing
    tranche_usd: float = 250.0
    max_legs_per_ticker: int = 3
    cooldown_sec: int = 300
    enable_sell_side: bool = True
    enable_buy_side: bool = True
    # Extra slippage buffer applied on top of the quoter's price-impact-inclusive quote.
    slippage_buffer: float = 0.003


class RiskConfig(BaseModel):
    daily_loss_limit_usd: float = 50.0      # realized; halts new entries until next UTC day
    max_drawdown_halt_pct: float = 0.10     # equity drawdown from peak that halts trading
    max_consecutive_errors: int = 10        # execution/feed errors before halting
    demote_live_to_paper_on_halt: bool = True


class GateConfig(BaseModel):
    """Criteria the paper record must satisfy before live trading is allowed."""
    min_days: int = 14
    min_round_trips: int = 20
    min_net_pnl_usd: float = 0.0
    min_profit_factor: float = 1.5
    max_drawdown_pct: float = 0.05
    min_win_rate: float = 0.55
    max_error_rate: float = 0.02
    min_feed_uptime: float = 0.95
    auto_promote: bool = False              # if true the runner flips to live by itself


class NotifyConfig(BaseModel):
    daily_report_hour_utc: int = 7
    telegram_enabled: bool = True


class Config(BaseModel):
    mode: Mode = "paper"
    poll_interval_sec: int = 30
    data_dir: Path = Path("./data")
    paper_quote_usd: float = 5000.0
    chain: ChainConfig = ChainConfig()
    reference: ReferenceConfig = ReferenceConfig()
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    gate: GateConfig = GateConfig()
    notify: NotifyConfig = NotifyConfig()
    tickers: list[TickerConfig] = Field(default_factory=list)

    @field_validator("tickers")
    @classmethod
    def _unique_symbols(cls, v: list[TickerConfig]) -> list[TickerConfig]:
        seen = set()
        for t in v:
            if t.symbol in seen:
                raise ValueError(f"duplicate ticker {t.symbol}")
            seen.add(t.symbol)
        return v

    @property
    def enabled_tickers(self) -> list[TickerConfig]:
        return [t for t in self.tickers if t.enabled]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "apebot.sqlite3"


class Secrets(BaseModel):
    finnhub_api_key: str | None = None
    polygon_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    private_key: str | None = None
    rpc_url_override: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        _load_dotenv()
        return cls(
            finnhub_api_key=os.getenv("FINNHUB_API_KEY"),
            polygon_api_key=os.getenv("POLYGON_API_KEY"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            private_key=os.getenv("APEBOT_PRIVATE_KEY"),
            rpc_url_override=os.getenv("APEBOT_RPC_URL"),
        )


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so we don't need python-dotenv. Does not override existing env."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    cfg = Config.model_validate(raw)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
