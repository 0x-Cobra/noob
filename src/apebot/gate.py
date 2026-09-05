"""Promotion gate: decides whether the paper record justifies switching to live."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .config import GateConfig
from .store import Store


@dataclass
class GateReport:
    evaluated_at: str
    mode_evaluated: str
    passed: bool
    metrics: dict = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]


def max_drawdown(series: list[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for v in series:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


def compute_metrics(store: Store, mode: str, capital_base: float, now: datetime) -> dict:
    legs = store.closed_legs(mode)
    ts = store.tick_stats()
    first = ts["first_ts"]
    days = (now - first).total_seconds() / 86400 if first else 0.0
    pnls = [l.pnl_usd or 0.0 for l in legs]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    eq = [v for _, v in store.equity_series(mode)]
    mdd = max_drawdown([0.0] + eq) if eq else 0.0
    errors = store.count_events("error")
    reasons: dict[str, int] = {}
    for l in legs:
        reasons[l.exit_reason or "?"] = reasons.get(l.exit_reason or "?", 0) + 1
    return {
        "days": round(days, 2),
        "round_trips": len(legs),
        "net_pnl_usd": round(sum(pnls), 2),
        "gross_win_usd": round(gross_win, 2),
        "gross_loss_usd": round(gross_loss, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0),
        "win_rate": round(len(wins) / len(pnls), 3) if pnls else 0.0,
        "avg_pnl_usd": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "max_drawdown_usd": round(mdd, 2),
        "max_drawdown_pct": round(mdd / capital_base, 4) if capital_base > 0 else 0.0,
        "capital_base_usd": round(capital_base, 2),
        "return_on_capital": round(sum(pnls) / capital_base, 4) if capital_base > 0 else 0.0,
        "ticks": ts["n"],
        "error_events": errors,
        "error_rate": round(errors / ts["n"], 4) if ts["n"] else 0.0,
        "feed_uptime_market_hours": round(ts["ext_ok"] / ts["ext"], 4) if ts["ext"] else 1.0,
        "exec_rejects": store.count_events("exec_reject"),
        "exit_reasons": reasons,
        "open_legs": len(store.open_legs(mode)),
    }


def evaluate(store: Store, cfg: GateConfig, capital_base: float, now: datetime, mode: str = "paper") -> GateReport:
    m = compute_metrics(store, mode, capital_base, now)
    fails: list[str] = []
    if m["days"] < cfg.min_days:
        fails.append(f"only {m['days']} days of record (need {cfg.min_days})")
    if m["round_trips"] < cfg.min_round_trips:
        fails.append(f"only {m['round_trips']} round trips (need {cfg.min_round_trips})")
    if m["net_pnl_usd"] <= cfg.min_net_pnl_usd:
        fails.append(f"net pnl {m['net_pnl_usd']} <= {cfg.min_net_pnl_usd}")
    if m["profit_factor"] < cfg.min_profit_factor:
        fails.append(f"profit factor {m['profit_factor']} < {cfg.min_profit_factor}")
    if m["max_drawdown_pct"] > cfg.max_drawdown_pct:
        fails.append(f"max drawdown {m['max_drawdown_pct']:.2%} > {cfg.max_drawdown_pct:.2%}")
    if m["win_rate"] < cfg.min_win_rate:
        fails.append(f"win rate {m['win_rate']:.1%} < {cfg.min_win_rate:.1%}")
    if m["error_rate"] > cfg.max_error_rate:
        fails.append(f"error rate {m['error_rate']:.2%} > {cfg.max_error_rate:.2%}")
    if m["feed_uptime_market_hours"] < cfg.min_feed_uptime:
        fails.append(f"feed uptime {m['feed_uptime_market_hours']:.1%} < {cfg.min_feed_uptime:.1%}")
    return GateReport(evaluated_at=now.isoformat(), mode_evaluated=mode, passed=not fails, metrics=m, failures=fails)


def recent_window(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)
