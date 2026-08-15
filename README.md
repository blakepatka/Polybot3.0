# Polybot 3.0

A local dashboard and paper-trading engine for Polymarket's short-window
**"Up or Down"** crypto markets — 5-minute and 15-minute windows across
**BTC, ETH, SOL, XRP, BNB, DOGE and HYPE**.

Paper trading starts automatically and needs no credentials, no API keys, and
no wallet. Everything it reads is public.

```bash
pip install -r requirements.txt
python run.py
# -> http://127.0.0.1:8848
```

---

## What it does

The engine runs one loop, about three times a second:

1. **Spot prices** from Coinbase and Kraken, seeded on startup with 30 minutes
   of 1-minute candles so it can trade immediately rather than idling.
2. **Window discovery.** Markets use the deterministic slug
   `{asset}-updown-{5m|15m}-{unix_start}`, so the live window's slug is
   *computed* rather than searched for. This matters: Polymarket pre-creates
   about a day of windows, and listing endpoints sorted by start date return
   tomorrow's markets, not the one trading now.
3. **Order books** straight from the CLOB, both sides, every pass.
4. **Scoring**, then entry where the model and the risk layer agree.
5. **Settlement** when the window closes.

### The model

A window resolves Up if the closing price is at or above the opening price. So
the question is whether the drift accumulated so far survives the time left. We
model the remaining move as driftless geometric Brownian motion:

```
P(Up) = Phi( ln(P_now / P_open) / sigma_remaining )
```

A big lead with seconds left is nearly certain; the same lead with minutes left
is close to a coin flip; a high-volatility regime shrinks every edge toward 0.5.

Both sides are then priced against the book, and the engine takes whichever the
market **underprices** — not whichever is more likely. Always buying the
favourite is structurally unprofitable here, because the market has already
repriced the drift.

---

## The strategy: a measured replication of one wallet

There is exactly one strategy in this project. It replicates Polymarket wallet
[`0x3c58…776b`](https://polymarket.com/profile/@antsaslyku) (@antsaslyku), and
every parameter is measured from a complete scrape of that wallet's public
history rather than chosen:

| | |
|---|---|
| Activity rows | **654,200** |
| Period | 2026-04-24 → 2026-08-15 (**113 days**) |
| Fills / redemptions | 569,015 / 85,001 |
| Turnover | **$4,190,784** |
| Windows reconstructed to a known winner | **101,541** |

**What it does.** btc/eth/sol/xrp on 5m (86.7% of fills) and 15m — 184 of
654,200 rows touch any other slug. Flat $5 clips. Every one of the 569,015
fills is a taker BUY; there is not a single SELL, so positions are held to
resolution and redeemed. First fill lands a median 53s into the window (p10 7s,
p90 291s, earliest 2s) at a median price of 0.540, and entries run the full
length of the window.

**Where the edge is.** It buys at price `p` and wins at about `p + 0.03`:

| Entry price | n | Avg paid | Win rate | Edge |
|---|---|---|---|---|
| 0.05–0.10 | 493 | 0.074 | 0.128 | **+0.054** |
| 0.20–0.30 | 3,039 | 0.251 | 0.295 | +0.044 |
| 0.50–0.55 | 19,834 | 0.519 | 0.554 | +0.035 |
| 0.70–0.75 | 6,016 | 0.718 | 0.742 | +0.024 |
| 0.80–0.85 | 1,630 | 0.816 | 0.814 | −0.002 |

That is a latency edge — lifting asks the spot move has already invalidated but
the book has not yet repriced — worth roughly three points of probability, and
it is **gone above 0.80**. Hence the 0.02–0.85 band.

**It is not arbitrage.** Across its 52,816 two-sided windows the first fill and
the hedge together cost a median **$1.042**, and only 35.6% of those pairs came
in under $1.00. Buying both sides of a binary for more than $1 is not a lock; it
is paying a premium to flatten a losing window.

**The ladder and the hedge cost capital, not P/L.** Holding the wallet's own
entry decisions fixed and varying only the follow-through, over all 101,541
resolved windows:

| Follow-through | Cost | Net | ROI |
|---|---|---|---|
| As traded (ladder + hedge) | $4,188,506 | +$85,916 | +2.05% |
| First side only, no hedge | $2,734,654 | +$55,186 | +2.02% |
| First side, max 3 fills | $1,534,037 | +$39,316 | +2.56% |
| **First side, max 2 fills** | $1,188,936 | +$32,369 | **+2.72%** |
| First fill only | $708,244 | +$19,036 | +2.69% |

The hedge deploys 5.9x the capital for the same return per dollar. Note that
the widely-quoted "single-sided windows returned +23.5%" is a **selection
artefact**: the wallet only *stays* single-sided in windows that went its way,
so conditioning on that conditions on the outcome.

Replayed with a flat $5 clip on the wallet's own entries in the 0.02–0.85 band,
the rule returns **+3.50% ROI over 98,742 windows**, positive in all five
calendar months but decaying: +4.23% (May), +5.34% (Jun), +2.62% (Jul), +1.55%
(Aug).

**Takers pay ~7%; makers pay nothing.** The market's own `feeSchedule` is
`{rate: 0.07, takerOnly: true}`. The fee scales with `min(p, 1-p)`, so it is
worst at coin-flip prices — exactly where this strategy trades. A taker at 50c
needs **53.5%** to break even. Every ROI above is already fee-inclusive:
`usdcSize/(shares*price)` averages 1.0245 across the 569,015 real fills.

**Liquidity rewards do not apply here.** Gamma reports `rewardsMaxSpread: 4.5`
and `rewardsMinSize: 50` on these markets, but the venue's reward register
(`/rewards/markets/current`, 8,960 markets over 18 pages) contains **none** of
the short crypto windows. Those Gamma fields are template defaults. No reward
income is modelled anywhere in this project.

### The edge cap is the load-bearing safety gate

The strategy buys down to 2c, because the wallet's cheapest entries are its most
profitable. That removes the price floor this repo previously relied on, so
`max_model_edge` replaces it: any entry where our probability exceeds the book's
all-in cost by more than 0.25 is refused.

Our probability and the book's price are two estimates of the same quantity.
When they disagree by tens of points the near-certain explanation is that our
spot feed is wrong, not that the venue is mispricing a five-minute window by
50x. The wallet's largest real edge across 101,541 windows was **+0.054**. The
cap is clamped in code, not merely defaulted, so configuration cannot raise it.

### Two configurations

`config.json` ships the **literal clone** — `max_fills_per_window: 40`,
`hedge_enabled: true`, `hedge_max_combined_cost: 99.0` — by operator decision on
2026-08-15, reproducing the wallet including the parts that lose money. To trade
the measured-profitable subset instead, set `max_fills_per_window: 2` and
`hedge_enabled: false`; or keep hedging but set `hedge_max_combined_cost: 1.0`
so the second leg can only ever lock a profit.

At a $50 bankroll the clone's ladder is truncated by the mandatory bankroll
controls long before its own fill cap: the wallet's median window costs $19.28
and its p90 is $96.43.

## Honest limitations

Read these before drawing conclusions from a P/L number.

**Basis risk is real.** These markets resolve against a Chainlink data stream.
We anchor and settle against Coinbase/Kraken spot, which leads Chainlink but is
not identical to it. On a near-flat close the two can disagree, and the paper
result will be wrong in a way live trading would not have been. The model is
never allowed to claim more than 98.5% confidence for exactly this reason.

**Paper settlement is immediate, not authoritative.** The venue publishes
resolution minutes after a window closes. We settle at once against our own
spot reference so the dashboard stays responsive. It approximates the venue's
resolution; it does not reproduce it.

**Paper fills are deliberately pessimistic, but still optimistic.** Fills walk
the real book rather than assuming the mid, and a clip the visible book cannot
fill in full is rejected rather than partially filled at a fictional price. But
no simulation models the market moving away from you as you take it.

**Rebates are excluded.** Polymarket pays account-tier taker rebates. Counting
them would let an unearned tier turn a losing strategy into a profitable-looking
one, so the dashboard reports trading P/L only.

**A small sample means nothing.** The "Candidate model trust" card stays red
until 300 settled windows, because a 12-window run at 83% is noise.

**The measurement is of the wallet, not of this bot.** Every ROI quoted above
replays the wallet's *own* entries. This project has to generate its own from
`polybot/signal.py`, and whether our feed is fast enough to capture the same
three points is exactly the open question Paper exists to answer. An edge of
+2 to +3% per settled window does not survive much extra latency or cost, and
the wallet's own edge has been shrinking month over month.

**Legacy note on fill modelling.** An earlier build modelled resting maker
orders as filled when the market traded strictly through their price, which
stood in for the whole queue at that level clearing. Real queue priority was not
simulated, and the observed 2.8% fill rate came with ~130 re-quotes per minute — a rate that would
meet API limits in live trading.

---

## Risk controls

Three independent brakes, each able to stop entries alone:

| Brake | Trips when | Effect |
|---|---|---|
| Volatility regime | Realized vol > 60bps (per asset) | That asset opens no new windows; resumes below 50bps |
| Drawdown breaker | Worst peak-to-trough over the last 25 settlements exceeds the limit | Everything halts for a cooldown |
| Exposure caps | Per trade, per window, per UTC day | Clip is clamped, or the entry is refused |

Plus a depth guard, a trade floor, a profit lock, and a rate limiter on entries.
Every brake reports a human-readable reason — a bot that silently stops trading
is indistinguishable from one that is broken.

Sliders in the dashboard apply to the next entry and persist across restarts.
Paper and Live use one shared saved limit set, so switching execution mode
changes only the broker and never changes the configured risk budget. The
per-window limit is one shared budget across all assets in the same timed
5-minute or 15-minute interval.

---

## Credentials

The **Runtime & keys** tab imports a wallet key and optional CLOB L2
credentials. Keys are validated locally (key-to-address derivation, and an
optional authenticated round-trip to Polymarket), written to a gitignored
`.env`, and never echoed back to the browser — the API returns only a masked
view.

Live integration uses `py-clob-client-v2` and pUSD collateral. Credentials
saved before Polymarket's April 2026 CLOB V2 cutover must be re-derived and
verified from the dashboard; legacy V1 credentials never open the live gate.

**Storing keys does not enable live trading.** See
[LIVE_TRADING.md](LIVE_TRADING.md).

---

## Configuration

`config.json` mirrors the schema of the upstream
[HarrierOnChain/Polymarket](https://github.com/HarrierOnChain/Polymarket) repo
(`venue`, `enable_trading`, `risk.circuit_breaker`, `risk.depth_guard`,
`risk.trade_floor`). That repo is a launcher shell whose engine lives in a
private crate, so the strategy here is implemented from scratch; the config
shape and the dry-run-by-default posture are what carried over.

One deliberate divergence: `min_orderbook_depth_usd` is 50, not 250. The
upstream value assumed far larger sizing and would exclude most of this venue's
short-window books at a $5 clip.

---

## Layout

```
run.py                  launcher (refuses non-loopback binds)
config.json             durable defaults
polybot/
  settings.py           config layering + mutable runtime state
  vault.py              credential validation and storage
  store.py              SQLite: positions, settlements, activity
  signal.py             the probability model
  risk.py               regimes, breakers, sizing
  engine.py             the loop
  server.py             FastAPI + SSE
  feeds/spot.py         Coinbase + Kraken, backfill, volatility
  feeds/polymarket.py   window discovery + CLOB books
  broker/paper.py       simulated fills against the real book
  broker/live.py        real orders (gated)
web/                    dashboard (no build step, no dependencies)
```

## Security

The server can start the engine, move every risk limit, and hold a wallet key.
On loopback it authenticates nobody, so it refuses any other interface — a
routable bind without a password is a remote control for a funded bot.

Set `POLYBOT_PASSWORD` and every route moves behind HTTP Basic (any username;
`/healthz` stays open so a platform healthcheck can reach it), and the bind is
then permitted. `--i-understand-the-risk` still forces a bind through without a
password, for a host that is private by other means.

That password is the only thing between the public internet and the engine.
There is no rate limiting, no lockout, and no second factor. Use a long random
value, and serve it over TLS only — Basic auth sends the password on every
request.

## Deploying

`railway.json` builds with Nixpacks and starts `python run.py --host 0.0.0.0`,
which picks up the platform's `$PORT`. Two things need setting beyond that:

| Variable | Why |
|---|---|
| `POLYBOT_PASSWORD` | Required. Without it the launcher refuses the bind. |
| `POLYBOT_DATA_DIR` | Point at a mounted volume, e.g. `/data`. |
| `POLYBOT_ENV_FILE` | Point at the same volume, e.g. `/data/.env`. |

The last two matter more than they look. A container's filesystem is rebuilt on
every deploy, so with the defaults the SQLite ledger — every position, every
settlement, the entire measured record — and any credentials saved from the
dashboard are discarded on each push. A volume keeps them.

Run **one replica**. Two instances share no state and would both trade the same
windows against the same bankroll, so the per-window and daily caps would each
be enforced twice over rather than once.

`LIVE_TRADING_ENABLED` is deliberately not part of a deploy checklist. Read
[LIVE_TRADING.md](LIVE_TRADING.md) before it goes anywhere near a hosted
instance: a cloud box you are not watching is the worst place to first exercise
an order path that has never run against real funds.

## Disclaimer

Trading prediction markets involves real financial risk. This software is
provided as-is, without warranty, and is not financial advice.
