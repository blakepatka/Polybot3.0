"""The lead/lag signal model.

A window resolves Up if the asset's price at the closing instant is at or above
its price at the opening instant. So the question "will this resolve Up?" is
exactly "will the drift accumulated so far survive the time remaining?".

We model the remaining move as driftless geometric Brownian motion calibrated
to recent realized volatility. With ``P_now`` the current price, ``P_open`` the
window's anchor, and ``sigma_rem`` the standard deviation of the remaining log
return::

    P(Up) = P(P_now * e^X >= P_open),  X ~ N(0, sigma_rem^2)
          = Phi( ln(P_now / P_open) / sigma_rem )

The intuition falls out cleanly: a big lead with little time left is nearly
certain, the same lead with lots of time left is barely better than a coin
flip, and a high-volatility regime shrinks every edge toward 0.5.

Two honest caveats, both surfaced in the UI rather than hidden:

* **Basis risk.** These markets resolve against a Chainlink data stream. We
  anchor and settle against Coinbase/Kraken spot, which leads Chainlink but is
  not identical to it. Near a coin-flip boundary the two can disagree.
* **Calibration.** The configured ``calibration`` factor discounts the claimed
  *edge* — the gap between our probability and the market's price — because
  that gap is what depends on the volatility estimate being right. Note it is
  not applied to the probability itself: shrinking probabilities toward 0.5
  would understate genuine favourites while making cheap longshots look
  artificially attractive.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .fees import taker_fee
from .feeds.polymarket import MarketWindow
from .feeds.spot import RESAMPLE_STEP, AssetState

# Below this, sigma_rem is too small to divide by meaningfully — the outcome is
# effectively decided and the model would report absolute certainty.
_MIN_SIGMA = 1e-7

# Irreducible uncertainty, and the reason the model is never allowed to claim
# certainty. These markets resolve against a Chainlink data stream while we
# observe Coinbase/Kraken spot; the two can disagree at the boundary, and no
# amount of drift makes that basis risk vanish.
#
# Without this clamp the model reports p=1.0 as a window closes, which makes the
# losing side's 2c ask look like a 50x edge — the classic way to lose money
# buying longshots. Capping at 1.5% keeps that ROI near zero.
_MIN_PROBABILITY = 0.015

# Never turn a cached exchange quote into a new order after both predictor
# feeds have stopped updating. Normal polling is once per second; fifteen
# seconds leaves room for a transient timeout without trading blind.
_MAX_SPOT_AGE_SECONDS = 15.0


def _phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def per_second_sigma(state: AssetState, lookback_seconds: float = 900.0) -> float | None:
    """Estimate volatility as a standard deviation of log return per √second.

    Measured on a uniform time grid rather than raw ticks. Irregular poll
    spacing and bid-ask bounce inflate a tick-level estimate by roughly an
    order of magnitude, and an inflated sigma flattens every real signal to a
    coin flip.
    """
    prices = state.resample(lookback_seconds)
    if len(prices) < 8:
        return None

    rets = [
        math.log(prices[i] / prices[i - 1])
        for i in range(1, len(prices))
        if prices[i - 1] > 0 and prices[i] > 0
    ]
    if len(rets) < 6:
        return None

    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    # Each return spans one grid step; convert to a per-√second figure.
    sigma = math.sqrt(max(var, 0.0)) / math.sqrt(RESAMPLE_STEP)
    return sigma if sigma > _MIN_SIGMA else None


@dataclass
class Signal:
    """Model output for one side of one window."""

    asset: str
    slug: str
    window: str
    side: str  # "up" | "down"
    confidence: float  # calibrated P(this side wins), 0..1
    raw_probability: float  # pre-calibration P(up)
    entry_price: float  # best ask for this side
    expected_roi: float  # per dollar staked, after assumed costs
    edge: float  # confidence - entry_price
    anchor_price: float
    spot_price: float
    drift_bps: float
    sigma_remaining: float
    seconds_remaining: float
    ask_depth_usd: float
    reason: str | None = None  # populated when the signal is not tradeable

    @property
    def tradeable(self) -> bool:
        return self.reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "slug": self.slug,
            "window": self.window,
            "side": self.side,
            "confidence": round(self.confidence, 4),
            "raw_probability": round(self.raw_probability, 4),
            "entry_price": round(self.entry_price, 4),
            "expected_roi": round(self.expected_roi, 4),
            "edge": round(self.edge, 4),
            "anchor_price": self.anchor_price,
            "spot_price": self.spot_price,
            "drift_bps": round(self.drift_bps, 2),
            "seconds_remaining": round(self.seconds_remaining, 1),
            "ask_depth_usd": round(self.ask_depth_usd, 2),
            "tradeable": self.tradeable,
            "reason": self.reason,
        }


class SignalModel:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.calibration = float(config["sizing"]["calibration"])
        entry = config["entry"]
        self.confidence_only = bool(entry.get("confidence_only", False))
        self.profit_tuned = bool(entry.get("profit_tuned", False))
        self.confidence_priority = self.confidence_only or self.profit_tuned
        self.min_confidence = float(entry["min_confidence"])
        self.asset_min_confidence = {
            str(asset).lower(): float(value)
            for asset, value in entry.get("asset_min_confidence", {}).items()
            if isinstance(value, (int, float))
        }
        self.market_min_confidence = {
            (
                str(row.get("asset", "")).lower(),
                str(row.get("window", "")).lower(),
            ): float(row.get("min_confidence", 0.0))
            for row in entry.get("paper_experimental_markets", [])
            if isinstance(row, dict)
            and isinstance(row.get("min_confidence"), (int, float))
        }
        self.min_expected_roi = float(entry["min_expected_roi"])
        self.max_entry_price = float(entry["max_entry_price"])
        self.min_entry_price = float(entry["min_entry_price"])
        self.min_seconds_remaining = float(entry["min_seconds_remaining"])
        # Expressed as a fraction of the window's own length. An absolute cap
        # would behave completely differently on a 5m window than on a 15m one
        # — 240s is most of the former and a quarter of the latter.
        self.max_window_fraction = float(entry["max_window_fraction"])
        self.min_depth_usd = float(config["risk"]["depth_guard"]["min_orderbook_depth_usd"])
        fees = config["fees"]
        self.taker_rate = float(fees.get("taker_rate", 0.07))
        self.fee_exponent = float(fees.get("exponent", 1.0))
        self.slippage_ticks = int(fees["assumed_slippage_ticks"])

    def evaluate(
        self, market: MarketWindow, state: AssetState, now: float | None = None
    ) -> Signal | None:
        """Score a window. Returns None when the model cannot form a view."""
        now = now if now is not None else time.time()

        spot = state.last_price
        if spot is None or spot <= 0:
            return None
        if state.last_update <= 0 or now - state.last_update > _MAX_SPOT_AGE_SECONDS:
            return None

        # The anchor is the asset's price at the instant the window opened. If
        # the tick buffer does not reach back that far we must not guess: a
        # wrong anchor inverts the signal.
        anchor = state.price_at_or_before(market.start)
        if anchor is None or anchor <= 0:
            return None

        seconds_remaining = market.seconds_remaining(now)
        if seconds_remaining <= 0:
            return None

        sigma_ps = per_second_sigma(state)
        if sigma_ps is None:
            return None
        sigma_rem = sigma_ps * math.sqrt(seconds_remaining)
        if sigma_rem < _MIN_SIGMA:
            return None

        drift = math.log(spot / anchor)
        p_up = min(max(_phi(drift / sigma_rem), _MIN_PROBABILITY), 1.0 - _MIN_PROBABILITY)

        # Evaluate both sides and take whichever the market underprices, rather
        # than always buying the favourite. Buying the favourite unconditionally
        # would be structurally unprofitable: the market has already repriced
        # the drift, so the favourite's ask is nearly always at or above our own
        # probability once costs are paid.
        best: Signal | None = None
        for side, probability in (("up", p_up), ("down", 1.0 - p_up)):
            book = market.book_for(side)
            if book is None or book.asks.best is None:
                continue

            # Assume we cross the spread by the configured number of ticks.
            entry_price = min(0.999, book.asks.best + self.slippage_ticks * market.tick_size)
            if entry_price <= 0:
                continue

            # A winning share pays $1; cost is the entry price plus the taker
            # fee, which scales with min(p, 1-p) and so bites hardest exactly
            # where a directional model wants to trade — near a coin flip.
            cost = entry_price + taker_fee(
                entry_price, 1.0, self.taker_rate, self.fee_exponent
            )

            # Calibration discounts the *edge*, not the probability. The edge is
            # the uncertain quantity — the part that depends on our sigma
            # estimate being right — whereas shrinking the probability toward
            # 0.5 would both understate favourites and make longshots look
            # artificially attractive.
            raw_edge = probability - cost
            edge = raw_edge * self.calibration
            expected_roi = edge / cost

            candidate = Signal(
                asset=market.asset,
                slug=market.slug,
                window=market.window,
                side=side,
                confidence=probability,
                raw_probability=p_up,
                entry_price=entry_price,
                expected_roi=expected_roi,
                edge=edge,
                anchor_price=anchor,
                spot_price=spot,
                drift_bps=drift * 10_000.0,
                sigma_remaining=sigma_rem,
                seconds_remaining=seconds_remaining,
                ask_depth_usd=book.asks.depth_usd(),
            )
            # Override modes follow the side with the strongest modeled win
            # probability. Normal mode remains cost-aware.
            score = candidate.confidence if self.confidence_priority else candidate.expected_roi
            best_score = (
                best.confidence if self.confidence_priority else best.expected_roi
            ) if best is not None else None
            if best is None or score > best_score:
                best = candidate

        if best is None:
            return None

        best.reason = self._reject_reason(best, market, now)
        return best

    def _reject_reason(
        self, sig: Signal, market: MarketWindow, now: float
    ) -> str | None:
        """First failing gate, or None when the signal may be traded."""
        # This operator override preserves only the requested confidence gate
        # plus exchange-level market availability.
        confidence_floor = max(
            self.min_confidence,
            self.asset_min_confidence.get(sig.asset.lower(), 0.0),
            self.market_min_confidence.get(
                (sig.asset.lower(), sig.window.lower()), 0.0
            ),
        )
        if sig.confidence < confidence_floor:
            return f"confidence {sig.confidence:.0%} < {confidence_floor:.0%}"
        if not market.accepting_orders or market.closed:
            return "market not accepting orders"
        # The operator price ceiling is unconditional.  Override modes may
        # bypass model-quality heuristics, but they must never authorize a buy
        # above the configured maximum entry price.
        if sig.entry_price > self.max_entry_price:
            return f"entry {sig.entry_price:.2f} above price ceiling"
        if self.confidence_only:
            return None

        if self.profit_tuned:
            duration = max(1.0, float(market.end - market.start))
            if market.seconds_elapsed(now) / duration > self.max_window_fraction:
                return f"too deep into the window (>{self.max_window_fraction:.0%} elapsed)"
            return None

        if sig.seconds_remaining < self.min_seconds_remaining:
            return f"only {sig.seconds_remaining:.0f}s left (need {self.min_seconds_remaining:.0f}s)"
        duration = max(1.0, float(market.end - market.start))
        if market.seconds_elapsed(now) / duration > self.max_window_fraction:
            return f"too deep into the window (>{self.max_window_fraction:.0%} elapsed)"
        if sig.entry_price < self.min_entry_price:
            return f"entry {sig.entry_price:.2f} below price floor"
        if sig.expected_roi < self.min_expected_roi:
            return f"expected ROI {sig.expected_roi:.1%} < {self.min_expected_roi:.1%}"
        if sig.ask_depth_usd < self.min_depth_usd:
            return f"depth ${sig.ask_depth_usd:.0f} < ${self.min_depth_usd:.0f}"
        return None
