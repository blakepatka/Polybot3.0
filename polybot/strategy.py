"""Behavioural replication of Polymarket wallet ``0x3c58...776b`` (@antsaslyku).

This is the only strategy in the project. It was rebuilt from a complete scrape
of the wallet's public history — **654,200 activity rows spanning 113 days**
(2026-04-24 → 2026-08-15): 569,015 fills, 85,001 redemptions, $4,190,784 of
turnover across 101,652 windows, of which 101,541 could be resolved to a
winning side and reconstructed end-to-end.

Every number below is measured from that scrape, not chosen.

WHAT THE WALLET DOES
--------------------
1. **One venue, eight markets.** btc/eth/sol/xrp on 5m (86.7% of fills) and
   15m (13.3%). Nothing else — 184 of 654,200 rows touch any other slug. It
   enters 37-69% of every window that exists, so it is selective, not
   indiscriminate.
2. **Flat $5 clips.** Median fill is $5.04 and — critically — the median is
   $5.04 at *every* ladder position from the 1st fill to the 26th. There is no
   escalation, no Kelly, no bankroll fraction. Mean drifts $6.97 → $8.45 only
   because of a thin tail; p90 is ~$15 throughout.
3. **Buys, never sells.** Zero SELL rows in 569,015 trades. Every position is
   held to resolution and redeemed. No take-profit, no stop-loss.
4. **Enters near a coin flip, throughout the window.** First fill lands a
   median 53s into the window (p10 7s, p90 291s, earliest 2s) at a median
   price of 0.540. Entries are spread across the whole window, with peaks in
   the first 10% (14.2% of fills) and the last 10% (14.1%).
5. **Ladders the same side, then often hedges.** Mean 5.6 fills per window
   (median 3). 52.0% of windows end up holding *both* sides, with the second
   leg opened a median 66s after the first.

WHERE THE EDGE ACTUALLY IS
--------------------------
The wallet buys at price ``p`` and wins at about ``p + 0.03``, consistently::

    entry price   n        avg paid   win rate   edge
    0.05-0.10       493      0.074      0.128    +0.054
    0.10-0.20     1,660      0.150      0.192    +0.042
    0.20-0.30     3,039      0.251      0.295    +0.044
    0.30-0.35     2,559      0.323      0.362    +0.039
    0.40-0.45     5,959      0.423      0.457    +0.034
    0.50-0.55    19,834      0.519      0.554    +0.035
    0.60-0.65    12,971      0.619      0.644    +0.026
    0.70-0.75     6,016      0.718      0.742    +0.024
    0.80-0.85     1,630      0.816      0.814    -0.002
    0.90-1.01       196      0.920      0.913    -0.007

That is a *latency* edge — it is lifting asks the spot move has already
invalidated but the book has not yet repriced — and it is worth roughly three
points of probability. It is emphatically **not** arbitrage, and it decays to
zero above 0.80. Hence :attr:`max_entry_price`.

IS IT ARBITRAGE? MOSTLY NO — BUT 7.31% OF THE TIME, YES
-------------------------------------------------------
Re-tested on 2026-08-15 after an operator challenge, and the original test was
wrong. It summed the two sides' VWAPs and asked whether a matched *pair* cost
under $1. That is equivalent only when the share counts match, and this
wallet's legs are lopsided by a median 2.11x (just 6.9% are within 1.1x).

The correct test is the worst case: whichever side resolves, you are paid
``shares`` on that side and nothing on the other, so the guaranteed payout is
``min(up_shares, down_shares)`` against the **total** cost. Across all 52,816
two-sided windows::

    genuine risk-free locks      3,863   (7.31%)
    median worst case              -$12.71
    median guaranteed / cost         0.623
    aggregate  $3,384,441 spent -> $2,054,308 guaranteed   (-39.30%)
    realised P/L on these windows                            -3.11%

The old VWAP test claimed 33.3%. A worked example of the disagreement, taken
from the live feed — BTC 1:30-1:35: Up 12.5 shares for $5.21, Down 37.2 for
$7.52, total $12.73. Combined VWAP is $0.619 and *looks* like a 38% lock, but
the guaranteed payout is min(12.5, 37.2) = $12.50 against $12.73 spent: a
-$0.23 worst case. Favourable, not free.

So this is a directional strategy that often ends up two-sided, not an
arbitrage strategy. But the 7.31% that genuinely are locks are free money, and
:meth:`AntsaslykuStrategy.lock_for` finds and sizes them deliberately.

THE LADDER AND THE HEDGE COST CAPITAL, NOT P/L
----------------------------------------------
Holding the wallet's own entry decisions fixed and varying only the
follow-through, across all 101,541 resolved windows::

    as traded (ladder + hedge)   cost $4,188,506   net $+85,916   ROI +2.05%
    first side only, no hedge    cost $2,734,654   net $+55,186   ROI +2.02%
    first side, max 3 fills      cost $1,534,037   net $+39,316   ROI +2.56%
    first side, max 2 fills      cost $1,188,936   net $+32,369   ROI +2.72%
    first fill only              cost   $708,244   net $+19,036   ROI +2.69%

The hedge is not what makes the money — it deploys 5.9x the capital for the
same return per dollar. (The widely-quoted "single-sided windows returned
+23.5%" is a selection artefact: the wallet only *stays* single-sided in
windows that went its way. Conditioning on that conditions on the outcome.)

Replayed with a flat $5 clip on the wallet's own entries, restricted to the
0.02-0.85 band, the rule returns **+3.50% ROI over 98,742 windows** and was
positive in all five calendar months — though decaying: +4.23% (May), +5.34%
(Jun), +2.62% (Jul), +1.55% (Aug).

Two caveats an operator must hold in mind. First, all of the above replays the
wallet's *own* entries; this module has to generate its own from
:mod:`polybot.signal`, and whether our feed is fast enough to capture the same
three points is the open question that Paper exists to answer. Second, the
observed edge is small and shrinking — an ROI of +2 to +3% per settled window
does not survive much extra cost or latency.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .fees import taker_fee
from .feeds.polymarket import MarketWindow
from .feeds.spot import AssetState
from .signal import Read, read_window

# The wallet's own floor: the earliest first fill observed in any of 101,652
# windows was 2s after the open. Before that there is no drift to read.
MIN_ENTRY_OFFSET_SECONDS = 2.0

# The measured edge peaks at +0.054 and averages +0.03. A model claiming more
# than this against the book is not finding a mispricing — it is reporting a
# broken spot feed, and acting on it means buying the losing side at the price
# where recovery is least likely. See `max_model_edge`.
SANE_MAX_EDGE = 0.25


@dataclass
class Intent:
    """One clip the replication wants to buy."""

    slug: str
    asset: str
    window: str
    side: str                 # "up" | "down"
    size_usd: float
    limit_price: float        # highest price we will pay, incl. slippage
    stage: str                # "open" | "add" | "hedge"
    fill_index: int           # 0-based position in this window's ladder
    confidence: float         # modelled P(this side wins)
    edge: float               # confidence - all-in cost
    drift_bps: float
    seconds_into_window: float
    ask_depth_usd: float
    # Guaranteed profit this order would lock in, when it makes the window
    # risk-free. Zero for every ordinary entry.
    lock_profit_usd: float = 0.0
    reason: str | None = None  # populated when the intent is not tradeable

    @property
    def tradeable(self) -> bool:
        return self.reason is None

    @property
    def is_lock(self) -> bool:
        return self.lock_profit_usd > 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "asset": self.asset,
            "window": self.window,
            "side": self.side,
            "size_usd": round(self.size_usd, 2),
            "limit_price": round(self.limit_price, 4),
            "entry_price": round(self.limit_price, 4),
            "stage": self.stage,
            "fill_index": self.fill_index,
            "confidence": round(self.confidence, 4),
            "edge": round(self.edge, 4),
            "drift_bps": round(self.drift_bps, 2),
            "seconds_into_window": round(self.seconds_into_window, 1),
            "ask_depth_usd": round(self.ask_depth_usd, 2),
            "lock_profit_usd": round(self.lock_profit_usd, 4),
            "is_lock": self.is_lock,
            "tradeable": self.tradeable,
            "reason": self.reason,
        }


@dataclass
class WindowState:
    """Per-window ladder bookkeeping. Lives only as long as the window."""

    slug: str
    opened_at: float = 0.0
    fills: int = 0
    cost_usd: float = 0.0
    last_fill_at: float = 0.0
    # Cost and share count per side, so the hedge leg can be priced against
    # what is already held rather than against the book alone.
    side_cost: dict[str, float] = field(default_factory=dict)
    side_shares: dict[str, float] = field(default_factory=dict)
    first_side: str | None = None
    adverse_streak: int = 0

    def record(self, side: str, size_usd: float, shares: float, now: float) -> None:
        self.fills += 1
        self.cost_usd += size_usd
        self.last_fill_at = now
        self.side_cost[side] = self.side_cost.get(side, 0.0) + size_usd
        self.side_shares[side] = self.side_shares.get(side, 0.0) + shares
        if self.first_side is None:
            self.first_side = side

    def fills_on(self, side: str) -> float:
        return self.side_shares.get(side, 0.0)

    def vwap(self, side: str) -> float | None:
        shares = self.side_shares.get(side, 0.0)
        if shares <= 0:
            return None
        return self.side_cost.get(side, 0.0) / shares

    def held_sides(self) -> set[str]:
        return {s for s, v in self.side_shares.items() if v > 0}

    @property
    def sides_held(self) -> int:
        return sum(1 for v in self.side_shares.values() if v > 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "fills": self.fills,
            "cost_usd": round(self.cost_usd, 2),
            "sides_held": self.sides_held,
            "first_side": self.first_side,
            "adverse_streak": self.adverse_streak,
            "side_cost": {k: round(v, 2) for k, v in self.side_cost.items()},
        }


class AntsaslykuStrategy:
    """The replication.

    Consumes the engine's feeds and returns :class:`Intent` objects. It holds
    no broker, no store handle and no risk manager, so it can be constructed,
    scanned and discarded without side effects — which is what makes it
    testable and what keeps the engine's mandatory bankroll controls the single
    authority on how much real money can move.
    """

    NAME = "antsaslyku"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        cfg = dict(config.get("strategy", {}) or {})
        self.cfg = cfg

        self.dry_run = bool(cfg.get("dry_run", False))
        self.risk_multiplier = float(cfg.get("risk_multiplier", 1.0))

        # -- observed universe ------------------------------------------------
        # Share of fills: btc 5m 31.3%, eth 5m 22.5%, sol 5m 21.2%, xrp 5m
        # 10.7%, then the 15m variants. First-fill-only ROI by asset:
        # xrp +5.78%, btc +3.32%, eth +3.29%, sol +2.01%.
        self.assets = [str(a).lower() for a in cfg.get("assets", ["btc", "eth", "sol", "xrp"])]
        self.windows = [str(w).lower() for w in cfg.get("windows", ["5m", "15m"])]

        # -- entry timing -----------------------------------------------------
        # First fill p10 7s / p50 53s / p90 291s into the window; the earliest
        # observed was 2s. Entries run the full length of the window, so the
        # default fraction is 1.0 — the last decile is where first-fill ROI is
        # actually highest (+16.2%).
        self.min_entry_offset_seconds = max(
            MIN_ENTRY_OFFSET_SECONDS, float(cfg.get("min_entry_offset_seconds", 2.0))
        )
        self.max_entry_window_fraction = float(cfg.get("max_entry_window_fraction", 1.0))
        self.min_seconds_remaining = float(cfg.get("min_seconds_remaining", 5.0))

        # -- entry pricing ----------------------------------------------------
        # The measured edge is positive from 0.02 to ~0.80 and gone by 0.85.
        self.min_entry_price = float(cfg.get("min_entry_price", 0.02))
        self.max_entry_price = float(cfg.get("max_entry_price", 0.85))
        # The wallet buys at roughly fair value plus three points, so it needs
        # no minimum-edge gate at all; 0.0 reproduces it.
        self.min_edge = float(cfg.get("min_edge", 0.0))
        # ...but it never has a forty-point edge either. A model that claims
        # one is describing a broken feed, and this is the gate that refuses to
        # trade on it. See SANE_MAX_EDGE.
        self.max_model_edge = min(
            SANE_MAX_EDGE, float(cfg.get("max_model_edge", SANE_MAX_EDGE))
        )
        self.min_confidence = float(cfg.get("min_confidence", 0.0))

        # -- sizing -----------------------------------------------------------
        # Median clip is $5.04 at every ladder index from 1 to 26. There is no
        # escalation in this wallet.
        self.base_clip_usd = float(cfg.get("base_clip_usd", 5.0))

        # ...but the clip is only $5 at ordinary prices. Measured median fill
        # size by the price paid, across all 569,015 fills:
        #
        #     0.00-0.02   n= 9,462   median $0.05
        #     0.02-0.05   n= 1,965   median $1.20
        #     0.05-0.10   n= 7,191   median $1.54
        #     0.10-0.20   n=20,074   median $3.05
        #     0.30-0.50            median $5.00
        #     0.50-0.70            median $5.06
        #     0.70-0.85            median $5.03
        #
        # The wallet stakes *dust* on longshots — five cents at a cent. Cloning
        # it with a flat $5 clip is not faithful, it is a 100x over-size in the
        # exact band where the position usually expires worthless. Observed
        # 2026-08-15: a flat clip bought 467 shares at 1c, three times, and lost
        # every one.
        self.clip_ladder = [
            (0.02, float(cfg.get("clip_under_2c_usd", 0.05))),
            (0.05, float(cfg.get("clip_under_5c_usd", 1.20))),
            (0.10, float(cfg.get("clip_under_10c_usd", 1.55))),
            (0.20, float(cfg.get("clip_under_20c_usd", 3.05))),
        ]
        self.price_scaled_clips = bool(cfg.get("price_scaled_clips", True))

        # The venue minimum is FIVE SHARES, not five dollars — `orderMinSize`
        # is denominated in shares. At 60c that is $3.00, which is why the
        # wallet shows $3.65 and $4.17 fills. Treating it as a dollar floor both
        # blocked every legitimate cheap entry and forced oversized ones.
        self.min_order_shares = float(cfg.get("min_order_shares", 5.0))

        # -- ladder shape -----------------------------------------------------
        # Mean 5.6 fills/window, median 3, p90 13, max 391. ROI by cap on the
        # first side: 1 fill +2.69%, 2 +2.72%, 3 +2.56%, 5 +2.22%, uncapped
        # +2.02%. The default reproduces the wallet rather than the optimum —
        # set this to 2 to keep only the component that carries the edge.
        self.max_fills_per_window = int(cfg.get("max_fills_per_window", 40))
        # 86.3% of the wallet's fills are on 5m and only 13.7% on 15m, and it
        # averages 5.94 fills per 5m window against 4.11 per 15m one. Three
        # times as many 5m windows exist per hour, so entering both at the same
        # rate already gives roughly 3:1 — the measured 4.36:1 comes from
        # entering a higher *fraction* of 5m windows (69% vs 47% on btc) and
        # laddering them harder. A per-window-type cap is what stops long 15m
        # windows from sitting on the exposure budget and starving the 5m
        # windows that carry most of the wallet's activity.
        ratios = cfg.get("max_fills_by_window") or {}
        self.max_fills_by_window = {
            "5m": int(ratios.get("5m", self.max_fills_per_window)),
            "15m": int(
                ratios.get("15m", max(1, round(self.max_fills_per_window * 4.11 / 5.94)))
            ),
        }
        # Same reasoning for concurrency: the measured peak is 8 windows at
        # once, and 15m windows must not be allowed to occupy all of them.
        self.max_open_windows_15m = int(cfg.get("max_open_windows_15m", 3))
        # Second same-side fill lands a median 21s after the first.
        self.min_seconds_between_fills = float(cfg.get("min_seconds_between_fills", 2.0))
        self.add_on_enabled = bool(cfg.get("add_on_enabled", True))
        # 56.5% of add-ons went in above the prior same-side fill, 33.8% below.
        self.add_into_strength = bool(cfg.get("add_into_strength", True))
        self.average_down_enabled = bool(cfg.get("average_down_enabled", True))

        # -- hedging ----------------------------------------------------------
        # 52.0% of the wallet's windows opened a second leg, a median 66s after
        # the first fill, for a median combined cost of $1.042 — above $1, so
        # it is a rescue and not a lock, and only 35.6% of its 52,816 hedges
        # came in under $1.00.
        #
        # Enabled by operator decision (2026-08-15) to reproduce the wallet
        # literally. Be clear about what that means: a combined cost above 1.0
        # buys, for more than $1, a pair that pays exactly $1 — the loss is
        # locked at the moment of the fill, not risked. Set hedge_enabled=false
        # (or hedge_max_combined_cost=1.0 to admit only genuine locks) to trade
        # the measured-profitable subset instead.
        self.hedge_enabled = bool(cfg.get("hedge_enabled", True))
        # One confirming scan, not two. Observed live on 2026-08-15, the XRP
        # 1:25-1:30 window went Down 34c -> Up 67c -> Down 37c -> Down 51c ->
        # Up 34c inside about a minute. At a 3s poll a two-tick confirmation
        # costs 6s per switch, which is most of the move on a 300s window.
        self.hedge_after_adverse_ticks = int(cfg.get("hedge_after_adverse_ticks", 1))
        self.hedge_max_combined_cost = float(cfg.get("hedge_max_combined_cost", 99.0))

        # -- locks -------------------------------------------------------------
        # 7.31% of the wallet's 52,816 two-sided windows end as genuine
        # risk-free locks: whichever side resolves, the payout exceeds the total
        # spent. Those are free money and the clone should take them
        # deliberately rather than stumble into them.
        #
        # Holding ``s`` shares of one side for a total ``c``, and able to buy the
        # other at an all-in ``q`` per share, buying ``n`` shares makes the
        # guaranteed payout min(s, n) against a cost of c + n*q. For n <= s that
        # is n(1-q) > c, so the guaranteed profit rises with n and is largest at
        # n = s; beyond s the payout stops growing while the cost keeps rising.
        # So the optimal lock is *exactly* as many shares as are already held,
        # and it exists iff s*(1-q) > c.
        self.lock_when_available = bool(cfg.get("lock_when_available", True))
        # Skip locks too small to be worth the fees and the exposure.
        self.min_lock_profit_usd = float(cfg.get("min_lock_profit_usd", 0.10))

        # -- exposure ---------------------------------------------------------
        # The wallet's own envelope: peak 8 concurrent windows, median $19.28
        # and p90 $96.43 of cost per window, peak $2,418. These are the outer
        # bound; the engine's mandatory bankroll controls are applied after
        # this and are what actually binds at a small balance.
        self.max_cost_per_window_usd = float(cfg.get("max_cost_per_window_usd", 120.0))
        self.max_open_windows = int(cfg.get("max_open_windows", 8))
        self.max_capital_usd = float(cfg.get("max_capital_usd", 650.0))

        # -- execution --------------------------------------------------------
        # Every observed fill is a taker BUY, and usdcSize/(shares*price)
        # averages 1.0245 — consistent with this venue's shares*rate*p*(1-p)
        # taker fee, and confirming that every ROI quoted above is fee-inclusive.
        self.slippage_ticks = int(cfg.get("slippage_ticks", 1))
        self.min_depth_usd = float(cfg.get("min_depth_usd", 25.0))

        fees = config.get("fees", {})
        self.taker_rate = float(fees.get("taker_rate", 0.07))
        self.fee_exponent = float(fees.get("exponent", 1.0))

        self.windows_state: dict[str, WindowState] = {}
        self.stats: dict[str, Any] = {
            "scans": 0,
            "intents": 0,
            "fills": 0,
            "rejections": {},
            "cost_usd": 0.0,
            "by_stage": {},
            "locks": 0,
            "locked_profit_usd": 0.0,
        }

    # -- helpers -----------------------------------------------------------

    def reject(self, reason: str) -> None:
        key = reason.split("(")[0].strip()[:48]
        self.stats["rejections"][key] = self.stats["rejections"].get(key, 0) + 1

    def state_for(self, slug: str) -> WindowState:
        st = self.windows_state.get(slug)
        if st is None:
            st = WindowState(slug=slug, opened_at=time.time())
            self.windows_state[slug] = st
        return st

    def open_cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.windows_state.values())

    def open_windows(self) -> int:
        return sum(1 for s in self.windows_state.values() if s.fills > 0)

    def drop_window(self, slug: str) -> WindowState | None:
        """Release a settled window's bookkeeping."""
        return self.windows_state.pop(slug, None)

    def prune(self, now: float | None = None, max_age: float = 7200.0) -> int:
        now = now if now is not None else time.time()
        stale = [
            slug for slug, s in self.windows_state.items()
            if s.opened_at and now - s.opened_at > max_age
        ]
        for slug in stale:
            self.windows_state.pop(slug, None)
        return len(stale)

    def all_in_cost(self, price: float) -> float:
        """Price plus the taker fee — what a winning share must beat."""
        return price + taker_fee(price, 1.0, self.taker_rate, self.fee_exponent)

    def clip_for(self, price: float) -> float:
        """What the wallet actually stakes at this price.

        Flat $5 above 20c; measured dust below it. See ``clip_ladder``.
        """
        clip = self.base_clip_usd
        if self.price_scaled_clips:
            for ceiling, size in self.clip_ladder:
                if price < ceiling:
                    clip = size
                    break
        return clip * self.risk_multiplier

    def lock_for(
        self, st: WindowState, side: str, limit_price: float
    ) -> tuple[float, float] | None:
        """Size an order that makes this window risk-free, if one exists.

        Returns ``(size_usd, guaranteed_profit_usd)``, or None when no lock is
        available at this price. See the derivation in ``__init__``: the optimal
        order is exactly as many shares as are already held on the other side.

        This is the test the earlier build got wrong. It compared the two sides'
        VWAPs and asked whether a matched *pair* cost under $1, which is only
        equivalent when the share counts match — and across the wallet's 52,816
        two-sided windows the legs are lopsided by a median 2.11x. Measured
        correctly, 7.31% of those windows are true locks against the 33.3% the
        VWAP test claimed.
        """
        other = next(iter(st.held_sides() - {side}), None)
        if other is None:
            return None
        s_other = st.side_shares.get(other, 0.0)
        s_this = st.side_shares.get(side, 0.0)
        cost = st.cost_usd  # everything already committed to this window
        if s_other <= 0:
            return None

        q = self.all_in_cost(limit_price)
        if q >= 1.0:
            return None

        # Buying n more of this side gives a guaranteed min(s_other, s_this + n)
        # against a cost of ``cost + n*q``. Below the balance point the profit
        # rises with n; past it the payout stops growing while the cost does
        # not. So the optimum is exactly the shortfall — which is why the third
        # fill of a window that is already two-sided can still create a lock.
        need = s_other - s_this
        if need <= 0:
            return None

        profit = s_other - cost - need * q
        if profit < self.min_lock_profit_usd:
            return None
        return need * q, profit

    def min_stake_for(self, price: float, market: MarketWindow | None = None) -> float:
        """Smallest order the venue will accept at this price, in dollars.

        ``orderMinSize`` is a share count, so the dollar floor moves with the
        price: 5 shares is $0.05 at a cent and $4.25 at 85c.
        """
        shares = self.min_order_shares
        if market is not None and getattr(market, "min_order_size", None):
            shares = float(market.min_order_size)
        return shares * max(price, 1e-6)

    # -- the model ---------------------------------------------------------

    def evaluate(
        self, market: MarketWindow, state: AssetState, now: float | None = None
    ) -> Intent | None:
        """Score one window and return the next clip the clone would buy.

        Returns ``None`` when no view can be formed at all, and an intent with
        ``reason`` set when a view exists but a gate blocks it, so the dashboard
        can render rejections rather than silently showing nothing.
        """
        now = now if now is not None else time.time()
        self.stats["scans"] += 1

        if market.asset.lower() not in self.assets:
            return None
        if market.window.lower() not in self.windows:
            return None

        read = read_window(market, state, now)
        if read is None:
            return None

        st = self.state_for(market.slug)

        # Score BOTH sides and take the one the book underprices most, measured
        # as raw edge — probability minus all-in cost.
        #
        # Raw edge, not expected ROI. ROI divides by cost, so a 2c ask scores
        # 50x on any disagreement and the strategy would systematically buy the
        # cheapest thing on the board; that is precisely how this repo's
        # predecessor lost money. Raw edge treats a three-point mispricing as
        # three points wherever it occurs, which is what the wallet's own
        # numbers describe.
        #
        # Both sides must be considered because the wallet's single most
        # profitable cohort is its cheap entries — 0.05-0.10 returned +51.65%
        # — and those are bets *against* the prevailing drift. A
        # favourite-only rule could never place them.
        best: tuple[float, str, float, float] | None = None
        for side in ("up", "down"):
            book = market.book_for(side)
            if book is None or book.asks.best is None:
                continue
            limit = min(
                0.999, book.asks.best + self.slippage_ticks * market.tick_size
            )
            probability = read.probability_for(side)
            edge = probability - self.all_in_cost(limit)
            if best is None or edge > best[0]:
                best = (edge, side, limit, probability)

        if best is None:
            return None
        edge, side, limit_price, confidence = best

        # Wanting a side we do not yet hold is a *switch*. Keyed to what is
        # actually held, not to whichever side happened to be first: once both
        # sides are held the wallet simply adds to whichever one it currently
        # likes, and a first-side-relative rule would mislabel half of that and
        # gate it behind a confirmation it does not need.
        held = st.held_sides()
        is_switch = bool(held) and side not in held
        if is_switch:
            st.adverse_streak += 1
        else:
            st.adverse_streak = 0

        book = market.book_for(side)
        stage = self._stage(st, is_switch)

        # Stake what the wallet stakes at this price, but never less than the
        # venue's five-share minimum — below that the order is unplaceable.
        size_usd = max(
            self.clip_for(limit_price),
            self.min_stake_for(limit_price, market),
        )

        # If this order can make the whole window risk-free, size it to do that
        # instead. A guaranteed profit outranks the wallet's flat clip: it is
        # the one case where the second leg is unambiguously worth buying, and
        # it is 7.31% of its two-sided windows.
        lock_profit = 0.0
        if self.lock_when_available and stage in ("hedge", "add"):
            lock = self.lock_for(st, side, limit_price)
            if lock is not None:
                size_usd, lock_profit = lock

        intent = Intent(
            slug=market.slug,
            asset=market.asset,
            window=market.window,
            side=side,
            size_usd=size_usd,
            limit_price=limit_price,
            stage=stage,
            fill_index=st.fills,
            confidence=confidence,
            edge=edge,
            drift_bps=read.drift_bps,
            seconds_into_window=read.seconds_elapsed,
            ask_depth_usd=book.asks.depth_usd(),
            lock_profit_usd=lock_profit,
        )
        intent.reason = self._reject_reason(intent, market, st, read, now)
        if intent.reason:
            self.reject(intent.reason)
        else:
            self.stats["intents"] += 1
        return intent

    def _stage(self, st: WindowState, is_switch: bool) -> str:
        """Which rung of the observed ladder this clip sits on."""
        if st.fills == 0:
            return "open"
        if is_switch:
            return "hedge"
        return "add"

    def _reject_reason(
        self,
        intent: Intent,
        market: MarketWindow,
        st: WindowState,
        read: Read,
        now: float,
    ) -> str | None:
        """First failing gate, or None when the clip may be bought."""
        if not market.accepting_orders or market.closed:
            return "market not accepting orders"

        elapsed, remaining = read.seconds_elapsed, read.seconds_remaining
        if elapsed < self.min_entry_offset_seconds:
            return f"only {elapsed:.0f}s into window (need {self.min_entry_offset_seconds:.0f}s)"
        if remaining < self.min_seconds_remaining:
            return f"only {remaining:.0f}s left (need {self.min_seconds_remaining:.0f}s)"
        duration = max(1.0, float(market.end - market.start))
        if elapsed / duration > self.max_entry_window_fraction:
            return f"too deep into the window (>{self.max_entry_window_fraction:.0%} elapsed)"

        # -- model-quality gates.
        #
        # A lock skips every gate in this block, and only this block. Its profit
        # is arithmetic on the book — if the guaranteed payout exceeds the total
        # cost, the window pays whichever side resolves, however wrong the
        # model happens to be. These gates all encode a view about when the
        # *model* can be trusted, so applying them to a position that does not
        # depend on the model would refuse free money. Everything below this
        # block — depth, timing, exposure, the venue's own limits — still binds,
        # because those are about whether the order can actually be filled and
        # afforded.
        if not intent.is_lock:
            # The measured edge dies above 0.80, and the wallet never pays more
            # than a handful of cents below 0.02.
            if intent.limit_price > self.max_entry_price:
                return f"entry {intent.limit_price:.2f} above price ceiling"
            if intent.limit_price < self.min_entry_price:
                return f"entry {intent.limit_price:.2f} below price floor"

            if intent.confidence < self.min_confidence:
                return f"confidence {intent.confidence:.0%} < {self.min_confidence:.0%}"
            if intent.edge < self.min_edge:
                return f"edge {intent.edge:.3f} < {self.min_edge:.3f}"
            # The load-bearing sanity gate. Our probability and the book's price
            # are two estimates of the same quantity; when they disagree by tens
            # of points the near-certain explanation is that our spot feed is
            # wrong, not that the venue is mispricing a five-minute window by
            # 50x. The wallet's largest measured edge in 101,541 windows was
            # +0.054.
            if intent.edge > self.max_model_edge:
                return (
                    f"edge {intent.edge:.2f} above {self.max_model_edge:.2f} — "
                    "implausible, treating the feed as unreliable"
                )

        if intent.ask_depth_usd < self.min_depth_usd:
            return f"depth ${intent.ask_depth_usd:.0f} < ${self.min_depth_usd:.0f}"

        # -- ladder gates -----------------------------------------------------
        if intent.stage != "open":
            cap = self.max_fills_by_window.get(
                intent.window.lower(), self.max_fills_per_window
            )
            if st.fills >= cap:
                return f"window fill cap ({cap})"
            if now - st.last_fill_at < self.min_seconds_between_fills:
                return "fill cadence"

        if intent.stage == "add":
            if not self.add_on_enabled:
                return "add-on disabled"
            avg = st.vwap(intent.side)
            if avg is not None:
                if intent.limit_price < avg and not self.average_down_enabled:
                    return "averaging down disabled"
                if intent.limit_price > avg and not self.add_into_strength:
                    return "adding into strength disabled"

        if intent.stage == "hedge":
            if not self.hedge_enabled:
                return "hedging disabled"
            if st.adverse_streak < self.hedge_after_adverse_ticks:
                return (
                    f"adverse streak {st.adverse_streak} < "
                    f"{self.hedge_after_adverse_ticks}"
                )
            # A lock needs no further justification: whichever side resolves,
            # the window pays more than it cost. Only non-lock hedges face the
            # combined-cost ceiling.
            if not intent.is_lock:
                # Price the pair against the side actually held, whichever that
                # is. Observed live: BTC 1:25-1:30 went Up 71c -> Down 39c ->
                # Down 39c -> Down 12c, and XRP flipped four times in a minute,
                # so the leg being switched away from is not necessarily first.
                other = next(iter(st.held_sides() - {intent.side}), None)
                held_vwap = st.vwap(other) if other else None
                if held_vwap is not None:
                    combined = held_vwap + intent.limit_price
                    if combined > self.hedge_max_combined_cost:
                        return (
                            f"combined cost {combined:.2f} above "
                            f"{self.hedge_max_combined_cost:.2f}"
                        )

        # -- exposure gates ---------------------------------------------------
        if st.cost_usd + intent.size_usd > self.max_cost_per_window_usd:
            return f"window cost cap (${self.max_cost_per_window_usd:.0f})"
        if st.fills == 0:
            if self.open_windows() >= self.max_open_windows:
                return f"max open windows ({self.max_open_windows})"
            # Keep slots free for the 5m windows the wallet mostly trades.
            if intent.window.lower() == "15m":
                open_15m = sum(
                    1 for slug, s in self.windows_state.items()
                    if s.fills > 0 and "-15m-" in slug
                )
                if open_15m >= self.max_open_windows_15m:
                    return f"max open 15m windows ({self.max_open_windows_15m})"
        if self.open_cost_usd() + intent.size_usd > self.max_capital_usd:
            return f"strategy capital cap (${self.max_capital_usd:.0f})"

        return None

    # -- fill accounting ---------------------------------------------------

    def record_fill(
        self, slug: str, side: str, size_usd: float, shares: float,
        stage: str = "open", now: float | None = None,
        lock_profit_usd: float = 0.0,
    ) -> WindowState:
        """Book a fill the engine actually got. Called after execution."""
        now = now if now is not None else time.time()
        st = self.state_for(slug)
        st.record(side, size_usd, shares, now)
        self.stats["fills"] += 1
        self.stats["cost_usd"] = round(self.stats["cost_usd"] + size_usd, 4)
        self.stats["by_stage"][stage] = self.stats["by_stage"].get(stage, 0) + 1
        if lock_profit_usd > 0:
            self.stats["locks"] += 1
            self.stats["locked_profit_usd"] = round(
                self.stats["locked_profit_usd"] + lock_profit_usd, 4
            )
        return st

    def settle(self, slug: str, up_won: bool) -> dict[str, Any]:
        """Resolve a window and release its state.

        The clone never sells, so settlement is the only exit: the winning side
        pays $1 a share and the other pays nothing.
        """
        st = self.windows_state.pop(slug, None)
        if st is None:
            return {"slug": slug, "known": False}
        winner = "up" if up_won else "down"
        payout = st.side_shares.get(winner, 0.0)
        return {
            "slug": slug,
            "known": True,
            "winner": winner,
            "fills": st.fills,
            "cost_usd": round(st.cost_usd, 4),
            "payout_usd": round(payout, 4),
            "pnl_usd": round(payout - st.cost_usd, 4),
            "sides_held": st.sides_held,
        }

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.NAME,
            "dry_run": self.dry_run,
            "risk_multiplier": self.risk_multiplier,
            "open_windows": self.open_windows(),
            "open_cost_usd": round(self.open_cost_usd(), 2),
            "max_capital_usd": self.max_capital_usd,
            "config": {
                "assets": self.assets,
                "windows": self.windows,
                "base_clip_usd": self.base_clip_usd,
                "max_fills_per_window": self.max_fills_per_window,
                "min_entry_price": self.min_entry_price,
                "max_entry_price": self.max_entry_price,
                "max_model_edge": self.max_model_edge,
                "hedge_enabled": self.hedge_enabled,
                "add_on_enabled": self.add_on_enabled,
                "max_open_windows": self.max_open_windows,
            },
            "stats": {
                "scans": self.stats["scans"],
                "intents": self.stats["intents"],
                "fills": self.stats["fills"],
                "cost_usd": round(self.stats["cost_usd"], 2),
                "by_stage": dict(self.stats["by_stage"]),
                "locks": self.stats["locks"],
                "locked_profit_usd": round(self.stats["locked_profit_usd"], 2),
                "rejections": dict(
                    sorted(self.stats["rejections"].items(), key=lambda kv: -kv[1])[:8]
                ),
            },
            "windows": [s.to_dict() for s in self.windows_state.values() if s.fills > 0],
        }
