"""Command line: run / simulate / check / report / gate / arm-live / disarm-live / resume / reset-paper."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import gate as gate_mod
from .config import Config, Secrets, load_config
from .store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------------- commands
def cmd_run(args: argparse.Namespace) -> int:
    from .runner import build_runner
    cfg = load_config(args.config)
    secrets = Secrets.from_env()
    runner = build_runner(cfg, secrets)
    if cfg.mode == "live" and runner.mode != "live" and not runner.store.get("live_armed"):
        logging.warning("config says mode=live but the gate is not armed; running PAPER. "
                        "Use `apebot arm-live` after the gate passes, or set gate.auto_promote: true.")
    runner.run_forever()
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from .execution import PaperExecutor
    from .notify import Notifier
    from .runner import Runner
    from .sim import build_world

    cfg = load_config(args.config) if args.config else Config()
    symbols = [t.symbol for t in cfg.enabled_tickers] or ["HIMS", "PLTR", "MSTR"]
    if not cfg.enabled_tickers:
        from .config import TickerConfig
        cfg.tickers = [TickerConfig(symbol=s, token="0x" + "0" * 40, paper_inventory=10.0) for s in symbols]
    db = Path(args.db)
    if db.exists():
        db.unlink()
    db.parent.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    clock, market, ref = build_world(symbols, start, seed=args.seed, depth_usd=args.depth)
    if args.feed_outage:
        ref.outage_prob = args.feed_outage
    cfg.poll_interval_sec = args.poll
    cfg.notify.telegram_enabled = False
    store = Store(db)
    runner = Runner(cfg, store, market, ref, PaperExecutor(market, cfg.strategy, cfg.chain),
                    Notifier(None, None, False), clock, on_fill=market.on_fill)
    runner.startup()
    end = start + timedelta(days=args.days)
    steps = 0
    while clock.now() < end:
        runner.step()
        clock.sleep(cfg.poll_interval_sec)
        steps += 1
    cb = runner.capital_base({})
    report = gate_mod.evaluate(store, cfg.gate, cb, clock.now())
    print(f"\nsimulated {args.days} days, {steps} steps, capital base {cb:,.2f}")
    print(report.to_json())
    _print_legs(store, "paper", limit=args.show_legs)
    return 0 if report.passed else 2


def _print_legs(store: Store, mode: str, limit: int = 15) -> None:
    legs = store.closed_legs(mode)
    if not legs:
        print("no closed legs")
        return
    print(f"\nlast {min(limit, len(legs))} of {len(legs)} closed {mode} legs:")
    for l in legs[-limit:]:
        print(f"  {l.entry_ts:%m-%d %H:%M} {l.side.value:4} {l.symbol:5} qty={l.qty:8.4f} "
              f"in={l.entry_px:9.3f} out={l.exit_px:9.3f} pnl={l.pnl_usd:+8.2f} {l.exit_reason}")


def cmd_check(args: argparse.Namespace) -> int:
    from web3 import Web3
    from .chain import UniswapV3Market, connect
    from .reference import build_reference_provider
    from .notify import Notifier
    cfg = load_config(args.config)
    secrets = Secrets.from_env()
    ok = True
    print(f"mode requested: {cfg.mode}; tickers: {[t.symbol for t in cfg.enabled_tickers]}")
    try:
        w3 = connect(secrets.rpc_url_override or cfg.chain.rpc_url, cfg.chain.chain_id)
        print(f"[ok] rpc connected, chain id {w3.eth.chain_id}, block {w3.eth.block_number}")
        for name in ("factory", "quoter_v2", "swap_router02", "quote_token"):
            addr = getattr(cfg.chain, name)
            if not addr:
                print(f"[!!] chain.{name} not set")
                ok = False
                continue
            code = w3.eth.get_code(Web3.to_checksum_address(addr))
            print(f"[{'ok' if code else '!!'}] chain.{name} {addr} {'has code' if code else 'NO CODE'}")
            ok = ok and bool(code)
        if ok:
            market = UniswapV3Market(w3, cfg.chain, cfg.enabled_tickers)
            for sym, p in market.pools.items():
                q = market.pool_quote(sym, 1.0, 1.0, _now())
                print(f"[ok] {sym}: pool {p.address} fee {p.fee} liq {p.liquidity} mid {q.mid:.4f} "
                      f"sell1 {q.sell_exec} buy1 {q.buy_exec}")
            if secrets.private_key:
                acct = w3.eth.account.from_key(secrets.private_key)
                eth = w3.eth.get_balance(acct.address) / 1e18
                print(f"[ok] wallet {acct.address}: {eth:.5f} ETH for gas")
                print(f"     {market.quote_token.symbol}: {market.erc20_balance(market.quote_token, acct.address):.4f}")
                for sym, tok in market.tokens.items():
                    print(f"     {sym}: {market.erc20_balance(tok, acct.address):.6f}")
            else:
                print("[--] APEBOT_PRIVATE_KEY not set: live mode unavailable (fine for paper)")
    except Exception as e:
        print(f"[!!] chain check failed: {e}")
        ok = False
    try:
        ref = build_reference_provider(cfg.reference.provider, secrets, 0)
        for t in cfg.enabled_tickers:
            q = ref.quote(t.reference_symbol)
            print(f"[ok] ref {t.symbol}: {q.price} at {q.ts:%Y-%m-%d %H:%M:%S}Z ({q.source})")
    except Exception as e:
        print(f"[!!] reference provider failed: {e}")
        ok = False
    n = Notifier(secrets.telegram_bot_token, secrets.telegram_chat_id, cfg.notify.telegram_enabled)
    print(f"[{'ok' if n.enabled else '--'}] telegram {'configured' if n.enabled else 'not configured'}")
    if n.enabled and args.ping:
        n.send("apebot check: hello")
    print("\nRESULT:", "OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def _open_store(args) -> tuple[Config, Store]:
    cfg = load_config(args.config)
    return cfg, Store(cfg.db_path)


def cmd_report(args: argparse.Namespace) -> int:
    cfg, store = _open_store(args)
    mode = store.get("mode", "paper")
    cb = float(store.get("capital_base", 0.0))
    print(f"mode: {mode}   capital base: {cb:,.2f}   halt: {store.get('halt')}   armed: {store.get('live_armed')}")
    for m in ("paper", "live"):
        met = gate_mod.compute_metrics(store, m, cb, _now())
        if met["ticks"] == 0 and met["round_trips"] == 0:
            continue
        print(f"\n[{m}] " + json.dumps(met, default=str))
        _print_legs(store, m, limit=args.limit)
    print("\nopen legs:")
    for l in store.open_legs():
        print(f"  #{l.id} [{l.mode}] {l.side.value} {l.symbol} qty={l.qty:.4f} entry={l.entry_px:.3f} "
              f"since {l.entry_ts:%m-%d %H:%M} force-close {l.force_close_at:%m-%d %H:%M}")
    for t in cfg.enabled_tickers:
        rows = store.recent_ticks(t.symbol, 1)
        if rows:
            r = rows[0]
            print(f"  {t.symbol}: mid {r['mid']} ref {r['ref']} sell-prem {r['sell_premium']} "
                  f"buy-disc {r['buy_discount']} at {r['ts']}")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    cfg, store = _open_store(args)
    report = gate_mod.evaluate(store, cfg.gate, float(store.get("capital_base", 0.0)), _now())
    print(report.to_json())
    return 0 if report.passed else 2


def cmd_arm_live(args: argparse.Namespace) -> int:
    cfg, store = _open_store(args)
    report = gate_mod.evaluate(store, cfg.gate, float(store.get("capital_base", 0.0)), _now())
    if not report.passed and not args.force:
        print("gate NOT passed; refusing to arm. Failures:\n  " + "\n  ".join(report.failures))
        print("(use --force to override; you are then trading live on an unproven record)")
        return 2
    store.set("live_armed", {"ts": _now().isoformat(), "fingerprint": report.fingerprint(), "forced": not report.passed})
    store.delete("halt")
    print("armed. The running bot switches to LIVE at its next daily check (or on restart).")
    return 0


def cmd_disarm_live(args: argparse.Namespace) -> int:
    _, store = _open_store(args)
    store.delete("live_armed")
    store.set("mode", "paper")
    print("disarmed; mode set to paper (takes effect on restart; open live legs still exit live if a key is present).")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    _, store = _open_store(args)
    store.delete("halt")
    print("halt cleared.")
    return 0


def cmd_reset_paper(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg.db_path.exists():
        cfg.db_path.unlink()
        print(f"deleted {cfg.db_path}")
    return 0


# ---------------------------------------------------------------------------------- parser
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apebot")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        p = sub.add_parser(name, **kw)
        p.add_argument("-c", "--config", default="config.yaml")
        p.set_defaults(fn=fn)
        return p

    add("run", cmd_run, help="run the bot (paper until the gate passes/armed)")
    s = add("simulate", cmd_simulate, help="offline synthetic run, no network")
    s.add_argument("--days", type=int, default=21)
    s.add_argument("--poll", type=int, default=60)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--depth", type=float, default=150_000.0, help="virtual pool depth in USD")
    s.add_argument("--feed-outage", type=float, default=0.0, help="probability a reference call fails")
    s.add_argument("--db", default="data/sim.sqlite3")
    s.add_argument("--show-legs", type=int, default=15)
    s.set_defaults(config=None)
    c = add("check", cmd_check, help="verify RPC, contracts, pools, reference feed, telegram, wallet")
    c.add_argument("--ping", action="store_true", help="send a telegram test message")
    r = add("report", cmd_report, help="print metrics, legs and latest ticks")
    r.add_argument("--limit", type=int, default=15)
    add("gate", cmd_gate, help="evaluate the promotion gate on the paper record")
    a = add("arm-live", cmd_arm_live, help="allow live trading (requires the gate to pass)")
    a.add_argument("--force", action="store_true")
    add("disarm-live", cmd_disarm_live, help="revoke live trading")
    add("resume", cmd_resume, help="clear a risk halt")
    add("reset-paper", cmd_reset_paper, help="delete the database and start the paper record over")

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
