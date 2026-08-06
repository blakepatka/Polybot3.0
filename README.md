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

## What was measured about strategy viability

A live profile trading these same windows shows $150k across 79,304 trades. Its
activity — both sides of one window, fills well inside the spread, 1c tail buys
— pointed at market making. Three things were then measured directly.

**1. A two-sided basket is only profitable as a maker.** Up + Down always pays
exactly $1, so a pair bought below $1 is locked profit with no directional risk.
Across 532 samples of live books (7 assets, 5m/15m, 4 minutes):

| | Below $1 | Median |
|---|---|---|
| Crossing the spread (`ask + ask`) | **0 / 532** | 1.020 |
| Resting at the bid (`bid + bid`) | **532 / 532** | 0.980 |

**2. Takers pay ~7%; makers pay nothing.** The market's own
`feeSchedule` is `{rate: 0.07, takerOnly: true}` with
`makerRebatesFeeShareBps: 10000`. The fee scales with `min(p, 1-p)`, so it is
worst at coin-flip prices — exactly where a directional model wants to trade. A
taker at 50c needs **53.5%** to break even; a maker needs 50%.

**3. Liquidity rewards do not apply here.** Gamma reports
`rewardsMaxSpread: 4.5` and `rewardsMinSize: 50` on these markets, but the
venue's reward register (`/rewards/markets/current`, 8,960 markets over 18
pages) contains **none** of the short crypto windows, and the per-market lookup
returns empty. Those Gamma fields are template defaults. No reward income is
modelled anywhere in this project.

### Measured results (16 minutes, same live data, separate databases)

| Strategy | Settled | Win rate | P/L | ROI |
|---|---|---|---|---|
| Directional (fees modelled) | 18 | 61.1% | −$1.89 | **−2.10%** |
| Maker | 16 | 56.2% | +$120.61 | +67.32% |

**The maker figure is one trade, not an edge.** A single DOGE 5m fill of 125
shares at 3.7c paid $125.00 for $4.65. The other fifteen settlements together
came to **+$0.26**. The losing settlements are the same bet missing: −$13.85 at
13.2c, −$11.95 at 7.7c, −$8.00 at 12.2c. That is a lottery-ticket distribution,
and sixteen samples cannot show whether it is profitable.

The directional result is more informative: a 61% win rate that still loses
money is the taker fee doing exactly what the arithmetic above predicts.

**Neither strategy here is demonstrated to be profitable.** Treat both as
instrumented experiments.

### Confirmed side switching

Directional Paper trading has an optional path that reserves one entry slot for
a single opposite-side leg when the model changes direction. The new signal must
remain tradeable for three scans spanning at least six seconds, clear the
configured confidence and fee-adjusted expected-ROI floors, and pass the normal
per-trade, timed-window, open-exposure, turnover, and loss controls. The second
leg uses the normal clip; it does not buy equal shares and call the result a
hedge, because crossing both asks plus taker fees usually locks a loss.

Historical explicit-reversal windows have not passed the two-block evidence
gate, so both `reversal_enabled` and `reversal_live_enabled` default to `false`.
The code remains available for a future controlled Paper forward test. A side
switch can improve or worsen a result; it does not guarantee a profit or an
hourly ROI.

When `require_evidence_qualified_market` is enabled, a static allowlist is not
enough to place an entry. The exact asset/window Paper cohort is checked again
against the latest 100 settlements: both the newest 50 and preceding 50 must
have positive fee-adjusted ROI. This rolling gate prevents a previously strong
market from continuing to trade after its recent evidence turns negative.

An exact cohort can be listed in `paper_experimental_markets` for controlled
forward testing before it qualifies, but only in Paper and only at or above
`paper_experimental_min_confidence` (90% by default). `asset_min_confidence` supplies the matching
per-asset floor. The exception never bypasses static exclusions, price/timing
filters, order-book checks, or any exposure and loss control, and it does not
make the cohort Live-eligible.

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
until 300 settled windows, because a 12-window run at 83% is noise. When a
strategy's returns are dominated by rare large payoffs — as the maker's are —
even 300 windows may not be enough.

**Maker fill rates are optimistic.** A resting order is modelled as filled when
the market trades strictly through its price, which stands in for the whole
queue at that level clearing. Real queue priority is not simulated, and the
observed 2.8% fill rate came with ~130 re-quotes per minute — a rate that would
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

The server has **no authentication** and can hold a wallet key. It binds to
loopback and refuses any other interface unless you pass
`--i-understand-the-risk`. Do not put it behind a tunnel or reverse proxy
without adding auth first.

## Disclaimer

Trading prediction markets involves real financial risk. This software is
provided as-is, without warranty, and is not financial advice.
