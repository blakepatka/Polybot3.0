# Efficiency review — 2026-08-15

Written after rebuilding the bot around the @antsaslyku replication. Ordered by
measured impact. Every number here is either measured from the 113-day wallet
scrape or from this machine.

---

## 1. Latency is the whole strategy, and this machine is 20x too far away

The cloned wallet's entire edge is **~3 points of probability** captured in the
seconds before the book reprices a spot move. That is a latency trade. Measured
from this machine, with connection reuse (i.e. what the engine actually pays):

| Endpoint | Role | Median RTT |
|---|---|---|
| `clob.polymarket.com` | **order submission** | **260 ms** |
| `gamma-api.polymarket.com` | window discovery | 116 ms |
| `api.exchange.coinbase.com` | spot (predictor) | 117 ms |
| `api.kraken.com` | spot (predictor) | 120 ms |

So the round trip is roughly **117 ms to learn the price + 260 ms to act on
it ≈ 380 ms of blindness** on every entry, before any processing. Against a
3-point edge on a 300-second window, that is a large fraction of the signal
given away.

**Recommendation: host in AWS `us-east-1` (N. Virginia).** Polymarket's CLOB and
Coinbase's API both resolve into us-east-1; Kraken peers well there too. Typical
intra-region RTT is single-digit to low-tens of milliseconds, which would cut
the figures above by roughly **10–20x**.

This is a genuine expectation, not a measurement — it cannot be verified without
deploying there. Verify it by running `scratchpad/latency.py` (or the equivalent)
from the target region *before* committing, and keep the region that wins. If the
existing Railway service is not already in a US-East region, move it.

Two caveats worth holding:
* Lower latency improves *execution*, not *forecasting*. See §2.
* The venue is behind Cloudflare, so some of the 260 ms is edge-to-origin and
  will not disappear entirely.

---

## 2. The real bottleneck is the forecaster, not the plumbing

Measured 2026-08-15 over 27,679 reconciled paper settlements:

| Forecaster | Brier score |
|---|---|
| **Market price** | **0.2283** |
| Always guess 60% | 0.2399 |
| `polybot/signal.py` | 0.2495 |

Our model is beaten by a constant, and it gets *worse* the more confident it is:
a claimed edge of 0.30–0.40 predicted 74.9% and realised 35.2%.

The wallet's edge lives entirely in **entry selection**. Sizing, laddering,
hedging and the market universe are all now faithfully cloned — that part was
easy. Reproducing the entry is the whole problem, and the current selector is
worse than the thing it is trying to beat.

**Recommendations, in order of expected value:**
1. **Use Chainlink Data Streams as the predictor, not just the settlement
   source.** It is what the venue resolves on, which removes the basis risk
   entirely. `polybot/feeds/chainlink.py` already supports it; it needs the
   credential.
2. **Move to websockets for spot.** Coinbase and Kraken both publish ticker
   streams. Polling once a second on a 117 ms RTT means the price is up to
   ~1.1 s stale at worst. A stream removes the poll interval from the error.
3. **Do not tune the model against paper P/L.** Small positive cells are noise;
   the memory note on this is explicit.

---

## 3. Fixed during this rebuild

These were live defects, not suggestions.

* **`orderMinSize` is 5 *shares*, not $5.** The `min_trade_size_usd: 5.0` floor
  treated a share count as dollars. It rejected every legitimate sub-$5 order
  *and* forced a full $5 clip onto longshots. Now `0.05`, with the true
  per-price minimum computed in `strategy.min_stake_for()`.
* **Longshot over-sizing.** A flat $5 clip bought 467 shares at 1c on three
  windows and lost all three. The wallet's median fill under 2c is **$0.05**.
  Clips are now price-scaled from the measured medians.
* **Strategy state was never released on settlement.** `max_capital_usd` and
  `max_open_windows` are enforced from in-memory state that nothing cleared, so
  the strategy would ratchet itself shut after a few hours while the ledger
  showed no open exposure. Now released in `Engine._release_window()`.
* **`/api/reset` left that state behind**, so the dashboard reported $59.93 at
  work against an empty ledger.
* **15m windows were starving 5m ones.** They stay open 3x longer and sat on the
  exposure budget. Capped via `max_open_windows_15m` and `max_fills_by_window`.

---

## 3b. The clone needs ~$250–650 of working capital; at $50 it is capital-capped

Measured over a five-minute paper run on 2026-08-15 with the switching fixes in:
**458 tradeable intents produced 10 fills.** The rejections say why, and none of
them are the strategy:

| Blocker | Hits | What it is |
|---|---|---|
| `hard market-window cap` ($10) | 228 | 2 fills per window, vs the wallet's 5.94 |
| `hard shared-window cap` ($25) | 195 | 5 fills per timed interval across 4 assets |
| `max open 15m windows` (3) | 41 | intended |
| Circuit breaker (5 / 60s) | 24 | **mis-scaled — now 30** |
| Bankroll open exposure | — | **$49.95 of $50.00, i.e. full** |

The wallet runs a median $19.28 per window across up to 8 concurrent windows.
Reproducing that shape needs roughly **$250 minimum and ~$650 to match its
observed peak**. At $50 the bankroll cap binds before any strategy parameter
does, so the clone runs as a truncated version of itself no matter what
`max_fills_per_window` says.

Fixed here: the circuit breaker, which was a runaway-loop guard being used as a
ladder cap and tripped 24 times in five minutes.

**Decision for the operator, not for this code:** either fund it toward $250+
and let the ladder run, or accept the truncated clone and set
`max_fills_per_window: 2` + `hedge_enabled: false` — which is the
profit-maximising configuration anyway (+2.72% vs +2.05%) and happens to fit a
small bankroll. Raising the per-window caps *without* raising the bankroll just
concentrates the same $50 into fewer windows.

---

## 4. Bankroll settings that block evidence

At a $50 bankroll with $5 clips, `max_daily_loss_usd: 5.0` is **one losing
clip**. On 2026-08-15 the paper run hit it within ten minutes and then rejected
238 consecutive entries — no evidence could accumulate.

A daily loss stop should be a circuit breaker, not a routine occurrence. For a
strategy whose measured per-window ROI is ~3% and whose variance is dominated by
$5 binary outcomes, a stop at 10% of bankroll will trip constantly by chance.

**Recommendation:** keep the loss stop meaningful in Live, but let Paper run.
Paper's entire purpose is to accumulate the 300+ settlements the model-trust
card asks for. The stop is currently bypassed for the UTC day by operator
request; consider a permanently higher Paper-only limit instead of a daily
manual bypass.

---

## 5. Cheaper things worth doing

* **Discovery polls Gamma for every asset/window/next-window every 20 s** — 16
  requests where the slugs are deterministic and the metadata never changes once
  created. Cache static market metadata for the life of the window and only
  re-poll `acceptingOrders`/`closed`.
* **`signal_scans` is dead.** The engine counter is never incremented since the
  rewrite; the dashboard tile reads 0 while the strategy's own `scans` counter
  is correct. Either wire it through or drop the tile.
* **The settlement reconciler sweeps every 300 s over a 6-hour lookback.** Once
  a window is stamped `venue` it can never change, yet it is re-queried. Bound
  the sweep to rows still marked `spot`.
* **Data-api 429s.** The live wallet check was returning `429 Too Many Requests`
  and pausing the engine. Back off and cache the wallet snapshot for a few
  seconds rather than fetching per order.

---

## 6. What not to do

* **Do not re-enable the hedge's unbounded combined cost expecting profit.** It
  is currently on by operator decision to reproduce the wallet literally, and it
  is faithful — but the measurement is unambiguous: 5.9x the capital for the
  same return per dollar, at a median pair cost of $1.042 for something that
  pays $1.00. `max_fills_per_window: 2` + `hedge_enabled: false` is the
  profit-maximising configuration on the same 101,541 windows.
* **Do not raise `max_model_edge`.** It is clamped in code for a reason — see §2.
* **Do not treat the wallet's headline "$570k profit / 99.6% win rate" as real.**
  It comes from `/closed-positions`, which is survivorship-filtered: losing
  positions are never redeemed, so they never appear.
