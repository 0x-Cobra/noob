# apebot

An autonomous, hands-off bot that trades the premium/discount between tokenized stocks on
**Robinhood Chain** (Uniswap v3 pools) and the real stock price. It **paper trades first**, on real
on-chain quotes, and only switches to live once its own paper record passes a configurable gate.

Same code path in both modes: the strategy, sizing, stops and risk halts are identical. The only
difference is whether the executor signs transactions or records simulated fills at the quoter price.

## What the strategy does

While the US market is closed, nothing anchors a stock-token pool to the real price. Retail pushes
open premiums (and discounts) that tend to close when the market reopens.

- **SELL leg**: pool execution price ≥ `sell_entry_premium` over the reference → sell a tranche of
  inventory into the pool. Buy it back when the pool is within `exit_band` of the reference.
- **BUY leg**: pool ≥ `buy_entry_discount` under the reference → buy a tranche with stablecoin. Sell
  it back on reversion.
- Every leg has a **stop** (pool moves `stop_loss` further against it) and a **force close**
  (`force_close_after_open_min` after the next regular-session open, or `intraday_max_hold_min`
  if entered during the session).
- Entry thresholds are raised by `closed_market_extra` while the market is closed, because the
  reference is then the last close, not a live price.
- All quotes come from Uniswap's `QuoterV2`, so **price impact and pool fee are already in the
  premium** the bot sees. Paper fills use the same quote plus `slippage_buffer` and a gas estimate.

This is **not** riskless arbitrage. A sold leg loses if the stock gaps up at the open before the
premium reverts. That is exactly the risk the paper period measures.

You need inventory to sell: the wallet (paper or real) holds a few stock tokens and some USDG.
The bot's P&L is measured as **strategy P&L only** (realized + unrealized on its legs), so the
inventory's own price moves don't pollute the gate metrics.

## Paper → live lifecycle

1. `apebot run` starts in **paper** mode. Every poll it records ticks, opens/closes paper legs, and
   snapshots strategy equity.
2. Once a day it evaluates the **gate** (`gate:` in config): min days, min round trips, net P&L,
   profit factor, max drawdown, win rate, error rate, feed uptime. The daily Telegram report shows
   the current verdict.
3. When the gate passes:
   - `gate.auto_promote: true` → the bot switches itself to live (needs `APEBOT_PRIVATE_KEY`, a
     funded wallet, and at least one ticker with inventory). Paper legs are closed on the switch.
   - `auto_promote: false` (default) → it tells you; you run `apebot arm-live`.
4. In live mode, a risk halt (daily loss, drawdown, repeated errors) stops new entries, exits still
   run, and with `demote_live_to_paper_on_halt: true` it drops back to paper and tells you.

Nothing ever goes live without a passed gate or an explicit `arm-live --force`.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml
cp .env.example .env
```

Fill in `config.yaml`:

| field | where to get it |
|---|---|
| `chain.factory`, `chain.quoter_v2`, `chain.swap_router02` | Uniswap v3 deployments page for Robinhood Chain (developers.uniswap.org) |
| `chain.quote_token` | USDG contract on Robinhood Chain (robinscan.io / Robinhood docs) |
| `tickers[].token` | Stock Token contract addresses from the Robinhood Chain docs or robinscan token list. **Hundreds of fake tickers exist; never take an address from a pool listing.** |
| `FINNHUB_API_KEY` | finnhub.io, free tier (real-time US quotes, 60 req/min) |
| `TELEGRAM_*` | @BotFather + your chat id |

Then verify everything the bot depends on:

```bash
apebot check          # RPC, contract code, pool discovery + live quotes, reference feed, telegram, wallet
```

`check` discovers the deepest base/USDG pool per ticker across the configured fee tiers and prints
a real one-token quote each way. If any line says `!!`, fix it before running.

## Run

```bash
apebot run                       # foreground
docker compose up -d --build     # or as a container (restart: unless-stopped)
```

Or install `deploy/apebot.service` for systemd. A small VPS is enough; the loop is a handful of
RPC calls per ticker per poll.

Useful commands:

```bash
apebot report          # metrics per mode, recent legs, open legs, latest premium per ticker
apebot gate            # evaluate the gate now (exit code 0 = pass)
apebot arm-live        # allow live (refuses if gate not passed; --force to override)
apebot disarm-live     # back to paper
apebot resume          # clear a risk halt
apebot reset-paper     # wipe the record and start over
apebot simulate --days 21   # offline synthetic run, no network (plumbing test, not a backtest)
```

## Going live: checklist

- Use a **dedicated hot wallet**. Put in only the inventory you are willing to trade plus a little
  ETH for gas. `APEBOT_PRIVATE_KEY` goes in `.env` on the box, nowhere else.
- Robinhood Stock Tokens are not offered to US, Canada, UK or Swiss residents, and issuers may use
  Uniswap v4 permissioned pools / allowlists. This bot targets the **v3** pools. If a ticker's
  liquidity has moved to a permissioned pool, `check` will report no pool and you should drop it.
- The first live session: set `tranche_usd` small and `max_legs_per_ticker: 1`, watch a few round
  trips, then scale.

## What the simulator is and isn't

`apebot simulate` drives the exact runner with a synthetic reference (random walk, gaps at the open)
and a synthetic pool (mean-reverting premium, wider when closed, random retail pushes, linear
price impact, self-impact on fills). It proves the state machine, ledger, halts, daily report, gate
and mode switch work. **Its P&L says nothing about the real edge.** Only the paper period on real
quotes does.

## Layout

```
src/apebot/
  config.py        pydantic config + .env loading
  market_hours.py  NYSE calendar (regular/extended, holidays, next open)
  reference.py     Finnhub / Polygon reference prices
  chain.py         Uniswap v3 adapter: pool discovery, slot0 mid, QuoterV2 quotes
  execution.py     PaperExecutor (quoter-based fills) / LiveExecutor (SwapRouter02)
  strategy.py      pure decision logic: entries, reverts, stops, force closes
  runner.py        main loop, balances, risk halts, daily report, gate + mode switch
  gate.py          promotion metrics and verdict
  store.py         sqlite: ticks, legs, fills, equity, events, kv
  sim.py           virtual clock + synthetic market
  cli.py           commands
```

## Known limitations

- Reference prices while the market is closed are the last trade; the bot compensates with
  `closed_market_extra` rather than with futures or pre-market data.
- One pool per ticker (the deepest v3 pool). No routing across pools or v2/v4.
- Live fills are measured from wallet balance deltas around the transaction; run one wallet per bot.
- Not tax or compliance advice. Personal use only; no token, no profit sharing.
