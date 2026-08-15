"""The price model. No strategy lives here — only P(window resolves Up).

A window resolves Up if the asset's price at the closing instant is at or above
its price at the opening instant. So "will this resolve Up?" is exactly "will
the drift accumulated so far survive the time remaining?".

We model the remaining move as driftless geometric Brownian motion calibrated
to recent realized volatility. With ``P_now`` the current price, ``P_open`` the
window's anchor, and ``sigma_rem`` the standard deviation of the remaining log
return::

    P(Up) = P(P_now * e^X >= P_open),  X ~ N(0, sigma_rem^2)
          = Phi( ln(P_now / P_open) / sigma_rem )

The intuition falls out cleanly: a big lead with little time left is nearly
certain, the same lead with lots of time left is barely better than a coin
flip, and a high-volatility regime shrinks every edge toward 0.5.

One honest caveat, surfaced in the UI rather than hidden: **basis risk**. These
markets resolve against a Chainlink data stream. Unless the Data Streams
credential is configured we anchor and settle against Coinbase/Kraken spot,
which leads Chainlink but is not identical to it. Near a coin-flip boundary the
two can disagree — which is why :data:`MIN_PROBABILITY` exists.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .feeds.polymarket import MarketWindow
from .feeds.spot import RESAMPLE_STEP, AssetState

# Below this, sigma_rem is too small to divide by meaningfully — the outcome is
# effectively decided and the model would report absolute certainty.
MIN_SIGMA = 1e-7

# Irreducible uncertainty, and the reason the model is never allowed to claim
# certainty. These markets resolve against a Chainlink data stream while we
# observe Coinbase/Kraken spot; the two can disagree at the boundary, and no
# amount of drift makes that basis risk vanish.
#
# Without this clamp the model reports p=1.0 as a window closes, which makes the
# losing side's 2c ask look like a 50x edge — the classic way to lose money
# buying longshots. Capping at 1.5% keeps that ROI near zero.
MIN_PROBABILITY = 0.015

# Never turn a cached exchange quote into a new order after both predictor
# feeds have stopped updating. Normal polling is once per second; fifteen
# seconds leaves room for a transient timeout without trading blind.
MAX_SPOT_AGE_SECONDS = 15.0


def phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def per_second_sigma(
    state: AssetState,
    lookback_seconds: float = 900.0,
    source: str | None = None,
) -> float | None:
    """Estimate volatility as a standard deviation of log return per √second.

    Measured on a uniform time grid rather than raw ticks, and on a single
    price series. Irregular poll spacing and bid-ask bounce inflate a
    tick-level estimate by roughly an order of magnitude, and an inflated
    sigma flattens every real signal to a coin flip.
    """
    prices = state.resample(lookback_seconds, source=source)
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
    return sigma if sigma > MIN_SIGMA else None


@dataclass
class Read:
    """One model read of one window."""

    p_up: float
    drift: float
    sigma_remaining: float
    anchor_price: float
    spot_price: float
    source: str
    seconds_remaining: float
    seconds_elapsed: float

    @property
    def drift_bps(self) -> float:
        return self.drift * 10_000.0

    def probability_for(self, side: str) -> float:
        return self.p_up if side.lower() == "up" else 1.0 - self.p_up

    def to_dict(self) -> dict[str, Any]:
        return {
            "p_up": round(self.p_up, 4),
            "drift_bps": round(self.drift_bps, 2),
            "sigma_remaining": self.sigma_remaining,
            "anchor_price": self.anchor_price,
            "spot_price": self.spot_price,
            "source": self.source,
            "seconds_remaining": round(self.seconds_remaining, 1),
            "seconds_elapsed": round(self.seconds_elapsed, 1),
        }


def read_window(
    market: MarketWindow, state: AssetState, now: float | None = None
) -> Read | None:
    """P(window resolves Up), or None when no view can be formed.

    Both ends of the drift must come from one price series. The buffer can hold
    exchange spot and the Chainlink resolution stream at once, and they sit a
    real, drifting basis apart — on BTC that is dollars. Dividing a price from
    one series by a price from the other reports that basis as a move, and over
    a short remaining horizon sigma_rem is small enough that a few basis points
    of it saturates the model to a near-certainty on a side the book prices at a
    couple of cents.
    """
    now = now if now is not None else time.time()

    source = state.pricing_source(market.start, now, MAX_SPOT_AGE_SECONDS)
    if source is None:
        return None

    newest = state.latest_from(source)
    if newest is None:
        return None
    spot_ts, spot = newest
    if spot is None or spot <= 0:
        return None
    if spot_ts <= 0 or now - spot_ts > MAX_SPOT_AGE_SECONDS:
        return None

    # The anchor is the asset's price at the instant the window opened. If the
    # tick buffer does not reach back that far we must not guess: a wrong
    # anchor inverts the signal.
    anchor = state.price_at_or_before(market.start, source)
    if anchor is None or anchor <= 0:
        return None

    seconds_remaining = market.seconds_remaining(now)
    if seconds_remaining <= 0:
        return None

    sigma_ps = per_second_sigma(state, source=source)
    if sigma_ps is None:
        return None
    sigma_rem = sigma_ps * math.sqrt(seconds_remaining)
    if sigma_rem < MIN_SIGMA:
        return None

    drift = math.log(spot / anchor)
    p_up = min(max(phi(drift / sigma_rem), MIN_PROBABILITY), 1.0 - MIN_PROBABILITY)

    return Read(
        p_up=p_up,
        drift=drift,
        sigma_remaining=sigma_rem,
        anchor_price=anchor,
        spot_price=spot,
        source=source,
        seconds_remaining=seconds_remaining,
        seconds_elapsed=market.seconds_elapsed(now),
    )
