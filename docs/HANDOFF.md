# apebot handoff document

Purpose: everything known, assumed and unverified about this project, written for a reader (human or
agent) who knows Robinhood and Robinhood Chain better than the author did. Sections 4 and 5 are the
ones to act on. Written 2026-09-05 from a sandbox that could **not** reach the chain RPC, Uniswap
docs, Robinhood docs, or the reference product's site; every chain fact below came from web search
snippets and is tagged with a confidence level.

---

## 1. Origin and goal

The user asked how hard it would be to copy https://www.arbitrageape.app/ for personal use, then
asked for a fully autonomous prototype that paper trades for days or weeks and only goes live if the
record looks good. The result is the `apebot` package in this repo.

What Arbitrage Ape claims to be (from its own marketing, via search snippets):

- Watches every pool where tokenized stocks trade on Robinhood Chain.
- When a pool trades far above the real stock price, it sells into it.
- Describes itself as "an autonomous market-making desk for tokenized equities" that "holds
  inventory where dislocations occur".
- Pays realized profit to holders of an `$AA` token every 15 minutes, on-chain.
- Thesis: while the US market is open, arbitrage bots buy the real share and sell the token, so gaps
  close in minutes. When the market is closed that trade does not exist, so pushed pools stay
  dislocated longer, especially nights and weekends.

Our version deliberately drops the token and the profit distribution (securities-law exposure) and
keeps only the trading loop, for one wallet, on a subset of tickers.

---

## 2. Facts gathered about Robinhood Chain

Confidence: **H** = multiple independent sources agree, **M** = one source or a snippet,
**L** = inferred. Verify anything M or L before relying on it.

### Chain

| Fact | Conf. | Source |
|---|---|---|
| Public mainnet launched 2026-07-01 | H | The Block, TechTimes, Robinhood newsroom |
| Arbitrum Orbit (Nitro) L2, settles to Ethereum with blob DA, ETH is the gas token | H | dwellir.com, eco.com, blockchainhub |
| Chain ID **4663** mainnet, **46630** testnet | H | trustswap, nodeflare, robinhoodchain.wiki |
| Public RPC `https://rpc.mainnet.chain.robinhood.com` (shared, rate limited; Robinhood points production traffic to Alchemy / QuickNode) | H | quicknode builders guide, nockterminal |
| Explorers: `robinhoodchain.blockscout.com`, `robinscan.io`, `explorer.bitquery.io/robinhood` | M | search snippets |
| First-come-first-served transaction ordering, account abstraction supported | M | dwellir |
| Day-one partners: Uniswap (primary AMM), Chainlink, BitGo, Alchemy, LayerZero, Arcus (dYdX-built spot DEX), Lighter (perps), Morpho (USDG Earn vault ~7% APY) | M | eco.com, The Block |
| Uniswap **v2, v3, v4 and UniswapX** all deployed on the chain; v3 periphery (`v3-core`, `v3-periphery`, `swap-router-contracts`) has a dedicated deployments page | H | developers.uniswap.org (page exists; contents not readable from sandbox) |
| Chainlink price feeds exist on the chain | M | partner list only; feed addresses unknown |
| Volume: top-5 chain by DEX volume in first two weeks (~$3.1B / 7 days), daily DEX volume records around $869M–$1.49B in Aug 2026, TVL ~$740M on Sept 1 | M | cryptobriefing, dropstab, chaincatcher |

### Stock Tokens

| Fact | Conf. | Source |
|---|---|---|
| ERC-20 "Stock Tokens", structured as **tokenized debt securities** giving economic exposure, no equity rights | H | KuCoin, Robinhood docs snippet, TechTimes |
| 190+ to 450+ tokens (numbers differ by date/source) covering big-cap US stocks and ETFs | M | docs snippet vs dwellir |
| **Not offered to residents of the US, Canada, UK, Switzerland**; not registered under US securities law | H | Robinhood docs, moneycheck |
| Purchase/redeem with the issuer requires KYC/AML; issuer may **suspend, freeze, restrict** | H | Robinhood docs snippet |
| Issuers can impose **wallet allowlists and transfer rules**; Uniswap v4 "Permissioned Pools" (July 2026) let an issuer allowlist swappers/LPs | H | Robinhood docs, crypto.news |
| 24/7 trading; while the off-chain market is closed, token price is purely on-chain supply/demand and "Robinhood or affiliated market makers carry the inventory risk" | H | laikalabs, eco.com |
| Pools are quoted in **USDG** (Paxos/Global Dollar); PAIR's aggregator uses USDG as the common in/out asset across "up to five canonical pools" | M | globenewswire (PAIR), fintechnize substack |
| Multiple pools per stock token exist, including "stock-paired" memecoin pools (e.g. an AI/NVDA pool holding 8,783 NVDA, 16% of on-chain NVDA, ~$6.2M daily volume) | M | KuCoin, chaincatcher |
| ~17% of on-chain supply of 19 high-liquidity tokens sits in 432 pools that use stock tokens as quote asset (Sept 1) | M | chaincatcher |
| **354 different contracts use the ticker GME**; fakes out-trade the real token 2:1 on $213M volume | H | Bitquery "63 million trades" study |
| Observed extreme dislocation: HIMS token at **+112%** over NYSE close during a weekend | M | KuCoin / search snippet |
| Genuine arbitrage across the whole chain measured at **$599k over 13 days across 4,403 wallets** (~1 in 20,000 dollars traded) | H | Bitquery study |
| Tokenized equities are 6.5% of chain trading; 49.5% of completed stock-token trades are profitable | M | Bitquery study |
| Existing third-party tools: `hoodpools.com` (pool scanner for stock tokens and stock-paired tokens), `stockhood.xyz` ("real prices for tokenized stocks") | M | search snippets |
| Robinhood allows self-custody of Stock Tokens on the chain (was a selling point of the launch); a public "Stock Token giveaway" support page exists for EU users | M | eco.com, robinhood.com/eu support |

### Things explicitly NOT known

- Any contract address: Uniswap v3 factory / QuoterV2 / SwapRouter02, USDG, any Stock Token.
- Whether the deepest stock-token liquidity is in v3 pools, v4 pools, or v2 pools. If it is v4
  (plausible given the "hook strategies" and "permissioned pools" coverage), the v3 adapter here
  will find nothing or only thin pools.
- Whether Stock Token contracts have on-chain transfer restrictions (allowlist in `_transfer`) that
  would block a fresh bot wallet from receiving or swapping them.
- Whether Robinhood accepts *deposits* of Stock Tokens back from a self-custody wallet (matters for
  a primary-market redemption loop; not needed for this bot which stays on-chain).
- Decimals of USDG and Stock Tokens (code reads them from the contract; nothing hardcoded).
- Whether Finnhub's free quote reflects pre/post-market trades. Code handles both cases.
- How Arbitrage Ape sources its reference price and whether it hedges.

---

## 3. Strategy specification (as implemented)

All numbers are config defaults in `config.example.yaml` / `StrategyConfig`.

**Universe**: a hand-picked list of tickers with verified token addresses. One pool per ticker: the
base/USDG Uniswap v3 pool with the highest `liquidity()` among fee tiers [500, 3000, 10000].

**Inputs per poll (default every 30s)**:

- `ref`: last trade price of the underlying from Finnhub (or Polygon), with its timestamp.
- `mid`: pool price from `slot0.sqrtPriceX96`.
- `sell_exec`: quote-per-base if we sell `tranche_qty` base now (QuoterV2 exactInputSingle).
- `buy_exec`: quote-per-base if we buy `tranche_qty` base now (QuoterV2 exactOutputSingle).
- `tranche_qty = tranche_usd / ref` (falls back to last mid if ref is missing).
- `market_open` (regular NYSE session), `extended_open` (04:00–20:00 ET), `ref_stale`
  (regular session open AND ref older than `max_staleness_sec`).

**Derived**: `sell_premium = sell_exec/ref - 1`, `buy_discount = 1 - buy_exec/ref`. Fee and price
impact are therefore inside the premium the strategy sees.

**Entry** (at most one per ticker per poll, none if a halt is active, ref is stale, cooldown not
elapsed, or `max_legs_per_ticker` reached; never both directions on one ticker at once):

- SELL leg if `sell_premium >= sell_entry_premium (+ closed_market_extra if market closed)` and the
  wallet holds base. Qty = min(tranche_qty, base balance). Skip if notional < 25% of tranche.
- BUY leg if `buy_discount >= buy_entry_discount (+ extra)` and quote balance suffices.

**Exit** (checked before entries; first matching rule wins):

1. `force`: now ≥ `force_close_at`. For entries while the market is closed that is
   `next_regular_open + force_close_after_open_min` (45 min). For intraday entries it is
   `entry + intraday_max_hold_min` (120 min).
2. `stop`: SELL leg when `buy_exec >= entry_px * (1 + stop_loss)`; BUY leg when
   `sell_exec <= entry_px * (1 - stop_loss)`. 6% default.
3. `revert`: SELL leg when `buy_exec <= ref * (1 + exit_band)`; BUY leg when
   `sell_exec >= ref * (1 - exit_band)`. 0.5% default. Requires a non-stale ref while the market is
   open; while closed the last close is accepted as the reference.

**P&L per leg** = (entry_px − exit_px)·qty for SELL, (exit_px − entry_px)·qty for BUY, minus gas on
both fills. Strategy equity = cumulative realized + unrealized on open legs. Inventory mark-to-market
is recorded but **excluded** from gate metrics so that the underlying's drift doesn't pollute them.

**Risk**:

- Daily realized loss ≤ −`daily_loss_limit_usd` → no new entries until next UTC day.
- Strategy-equity drawdown from peak > `max_drawdown_halt_pct` × capital base → halt.
- `max_consecutive_errors` polling steps with a feed/RPC error → halt.
- Halt = no entries; exits continue. Paper halts self-clear after 24h. Live halts are sticky
  (`apebot resume`) and, by default, demote the bot to paper.

**Promotion gate** (evaluated daily at `daily_report_hour_utc`, and by `apebot gate`): all of
`min_days`, `min_round_trips`, `net_pnl > min_net_pnl_usd`, `profit_factor >= 1.5`,
`max_drawdown_pct <= 5%`, `win_rate >= 55%`, `error_rate <= 2%`, `feed_uptime >= 95%`.
Pass + `auto_promote` → live. Pass without auto → Telegram nudge to run `apebot arm-live`.

**Live execution**: SwapRouter02 `exactInputSingle` (sells) / `exactOutputSingle` (buys),
`amountOutMinimum` / `amountInMaximum` from the quoter price ± `slippage_buffer`, unlimited
approval once per token, EIP-1559 fees with a `max_gas_gwei` ceiling, fill measured from wallet
balance deltas before/after the receipt.

---

## 4. Assumptions baked into the code — verify each

| # | Assumption | Where | How to verify | If wrong |
|---|---|---|---|---|
| A1 | Stock-token liquidity is in **Uniswap v3** pools reachable through the standard v3 Factory/QuoterV2/SwapRouter02 | `chain.py`, `execution.py` | Look up a real token on robinscan / hoodpools: which DEX and version holds the deep pool? Run `apebot check`. | Add a v4 adapter (StateView/V4Quoter/UniversalRouter, PoolKey with hooks) or a v2 one behind the `Market` interface. |
| A2 | Pools are quoted in **USDG** and one stable is enough | `ChainConfig.quote_token` | Inspect real pools' token0/token1. | Make `quote_token` per ticker. |
| A3 | Stock Tokens are freely transferable ERC-20s from a fresh EOA | `LiveExecutor` | Read token source on the explorer; look for allowlist / `_beforeTokenTransfer` checks; try a 1-wei transfer from a test wallet. | Live mode impossible for that token; keep paper only or KYC the bot wallet with the issuer if there is a path. |
| A4 | QuoterV2 `quoteExactInputSingle`/`quoteExactOutputSingle` accept the v3 struct signature used in `abi.py` | `abi.py` | Compare with the deployed QuoterV2 ABI. | Adjust ABI. |
| A5 | The chain returns `baseFeePerGas` (EIP-1559); fallback to legacy `gasPrice` exists | `LiveExecutor._tx_fields` | `eth_getBlockByNumber latest`. | Nothing; fallback handles it. |
| A6 | Finnhub `/quote` `t` field is the last-trade time and `c` is a usable last price, including or excluding extended hours | `reference.py` | Call it at 18:00 ET and compare with a broker's after-hours print. | Switch `reference.provider` to polygon (paid) or add a provider. |
| A7 | 30-second polling with 5 RPC calls per ticker (slot0, liquidity, 2 quotes, occasionally balances) is within RPC limits | `runner.py` | Watch for 429s in `apebot run -v`. | Raise `poll_interval_sec`; use a dedicated RPC. |
| A8 | NYSE holiday list for 2026–2027 in `market_hours.py` is correct | `market_hours.py` | Compare against nyse.com calendar. | Edit the sets. |
| A9 | The last regular-session trade is an acceptable reference while the market is closed, with `closed_market_extra` as the only compensation | `strategy.py` | Judgment call. Bitquery/laikalabs describe exactly this regime. | Add a futures/pre-market adjustment as a second reference source. |
| A10 | Gas on the L2 is on the order of cents (`gas_cost_usd_per_swap: 0.05`) | `ChainConfig` | Look at a real swap receipt on the explorer. | Set the config value; only affects paper fee accounting. |
| A11 | One bot per wallet (fills computed from balance deltas) | `LiveExecutor.execute` | — | Parse `Transfer` logs from the receipt instead. |

---

## 5. Open questions for the Robinhood-aware side

1. Which contract addresses are canonical for HIMS, PLTR, MSTR, HOOD, COIN (or whichever tickers
   you prefer)? Where is the authoritative token list (Robinhood docs page "Building with Stock
   Tokens" apparently has one)?
2. USDG address and decimals on chain 4663.
3. Uniswap v3 Factory, QuoterV2, SwapRouter02 addresses on chain 4663 (the deployments page
   exists at developers.uniswap.org under protocols/v3/deployments/v3-robinhood-chain-deployments).
4. For each chosen ticker: which single pool is deepest, and is it v2/v3/v4? Is it permissioned?
5. Does the Robinhood EU app let a user send Stock Tokens to an arbitrary self-custody address, and
   accept them back? What are the limits/fees? (Needed for restocking inventory, not for the bot.)
6. Is there a Chainlink feed per Stock Token on-chain? If yes, it would be a better closed-market
   reference than Finnhub's last trade and removes an external dependency.
7. Has anyone measured the actual distribution of after-hours premiums per ticker? Even a week of
   `ticks` from this bot in paper mode answers that (`SELECT symbol, AVG(sell_premium), MAX(...)`).
8. Is the user a resident of an eligible jurisdiction (not US/CA/UK/CH)? Everything downstream is
   moot otherwise.

---

## 6. Architecture

```
poll loop (Runner.step)
  for each ticker:
    RefQuote  <- ReferenceProvider.quote()           reference.py
    PoolQuote <- Market.pool_quote(sym, qty, qty)    chain.py (UniswapV3Market) | sim.py (SyntheticMarket)
    Tick      -> Store.add_tick                      store.py
    Decision  <- strategy.decide(tick, open legs, balances, halts)   strategy.py (pure)
    for order: Executor.execute -> Fill              execution.py (Paper | Live)
               update balances, legs, realized pnl, events
  equity snapshot per mode -> drawdown check
  once per UTC day: gate.evaluate -> Telegram report -> maybe promote to live
```

Extension points (all abstract base classes):

- `chain.Market`: `mid`, `quote_sell`, `quote_buy`, `pool_quote`. Implement for Uniswap v4 or
  another DEX and pass it to `Runner`.
- `reference.ReferenceProvider`: `quote(symbol) -> RefQuote`. Implement for Chainlink, IBKR, etc.
- `execution.Executor`: `execute(order, now) -> Fill`. Implement for another router.
- `sim.Clock`: real or virtual time. The runner never calls `datetime.now` directly.

### Data model (sqlite, `data/apebot.sqlite3`)

- `ticks(ts, symbol, mid, sell_exec, buy_exec, ref, ref_ts, market_open, extended_open, ref_stale, sell_premium, buy_discount, liquidity)`
  — one row per ticker per poll. This is the dataset for measuring the real premium distribution.
- `legs(id, symbol, side, qty, entry_px, entry_ref, entry_ts, force_close_at, status, exit_px, exit_ts, exit_reason, fees_usd, pnl_usd, mode)`
- `fills(ts, symbol, side, qty, px, fees_usd, tx_hash, mode, leg_id, reason)`
- `equity(ts, mode, realized_cum, unrealized, strategy_equity, quote_usd, inventory_usd)`
- `events(ts, level, kind, msg)` — kinds: `start`, `trade`, `error` (feed/RPC, counted by the gate),
  `exec_reject` (slippage/moved, not counted), `halt`, `mode`, `feed`.
- `kv` keys: `mode`, `mode_since`, `paper_balances`, `capital_base`, `realized_cum_{mode}`,
  `day_pnl_{mode}`, `equity_peak_{mode}`, `halt`, `live_armed`, `last_report_day`, `last_gate_report`.

### Mode/leg semantics

- Each leg carries the mode it was opened in and is always closed by that mode's executor. So a
  live→paper demotion keeps exiting live positions live.
- On paper→live promotion, open paper legs are closed at the current quote (`mode_switch`).
- `live_armed` in kv (set by `arm-live` or by auto-promote) is what makes the runner go live, at the
  next daily check or on restart. `config.mode: live` alone does nothing.

---

## 7. Verification status

- 29 tests (`pytest`): market calendar, sqrtPriceX96 math both token orders, every strategy rule,
  gate pass/fail and error-vs-reject accounting, store, and runner integration on the synthetic
  world (ledger consistency, halts, self-clearing paper halt, daily loss limit, refusal to go live
  without a key, arming with a fake live executor).
- `apebot simulate --days 21`: ~30k steps, 221 round trips, zero system errors, exit mix
  revert/stop/force = 120/40/61, gate passes. **The synthetic premium process is invented; this
  proves the plumbing only.**
- Never run against the real chain. `apebot check` is the first thing to run once addresses exist.

---

## 8. Backlog / ideas, in priority order

1. Fill addresses, run `check`, run paper for 2–4 weeks. Everything else waits on that.
2. Query `ticks` after a week: premium distribution per ticker by hour-of-week. Tune
   `sell_entry_premium`, `closed_market_extra`, `tranche_usd` from data, not defaults.
3. Uniswap v4 adapter if that's where liquidity is.
4. Chainlink on-chain reference as fallback / cross-check to catch a bad Finnhub print.
5. Hedge option: short the underlying via Lighter perps or a CFD broker when a SELL leg opens, to
   remove the Monday gap risk. Turns the trade from directional to nearly pure premium capture.
6. Multi-pool routing (the PAIR aggregator suggests 5 canonical pools per token).
7. Restocking inventory automatically is out of scope (needs the Robinhood app).

---

## 9. Sources (search snippets, 2026-09-05)

- https://www.arbitrageape.app/ (blocked from sandbox; text via search snippets)
- https://bitquery.io/investigations/robinhood-chain-tokenized-stocks — "What 63 million trades reveal"
- https://docs.robinhood.com/chain/stock-tokens/ and /chain/building-with-stock-tokens/
- https://developers.uniswap.org/docs/protocols/v3/deployments/v3-robinhood-chain-deployments
- https://www.dwellir.com/blog/what-is-robinhood-chain
- https://eco.com/support/en/articles/15083160-robinhood-tokenized-stocks-what-s-live-and-how-it-works
- https://eco.com/support/en/articles/15859739-what-is-robinhood-chain-inside-robinhood-s-arbitrum-l2
- https://www.theblock.co/news/business/2026-07-01-robinhood-chain-goes-live-mainnet-alongside-24-7-tokenized-stocks-lighter-perps-planned-crypto-agentic-trading-406918
- https://moneycheck.com/robinhood-chain-goes-live-as-stock-tokens-move-outside-us-market-access-first/
- https://crypto.news/uniswap-tokenized-stock-volume-robinhood-chain-hits-1b/
- https://cryptobriefing.com/robinhood-chain-uniswap-v4-hooks-tokenized-stocks/
- https://www.chaincatcher.com/en/article/2287034
- https://www.kucoin.com/blog/robinhood-stock-paired-meme-coins
- https://www.globenewswire.com/news-release/2026/08/31/3353221/0/en/pair-launches-the-first-multipool-rwa-launchpad-on-robinhood-chain-pairing-new-tokens-with-baskets-of-tokenized-stocks-partners-with-aws-to-scale-its-infrastructure.html
- https://laikalabs.ai/market-intelligence/robinhood-chain-stock-tokens-trading
- https://www.hoodpools.com/ , https://stockhood.xyz/
- https://www.quicknode.com/builders-guide/tools/robinhood-chain-public-rpc-by-robinhood-markets
- https://robinscan.io/ , https://robinhoodchain.blockscout.com
