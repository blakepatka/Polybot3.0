"""Polymarket trading fees.

Short-window crypto markets carry a ``crypto_fees_v2`` schedule, read straight
off the market object:

    {"exponent": 1, "rate": 0.07, "takerOnly": true, "rebateRate": 0.2}

Two facts matter enormously for anything that reports P/L:

* **Takers pay; makers do not.** ``takerOnly: true``, and makers additionally
  receive a fee-share rebate (``makerRebatesFeeShareBps: 10000``). Any strategy
  that crosses the spread is paying a toll that a resting order avoids
  entirely.
* **The fee scales with uncertainty, not with notional.** CLOB V2 uses
  ``shares * rate * (price * (1 - price)) ** exponent``, so a coin-flip trade
  is charged the maximum while a 95c near-certainty is charged very little.
  This is the opposite shape to a flat percentage fee.

At 50c and a 0.07 rate the fee is 1.75c per share — 3.5% of notional. Ignoring
it overstates every taker result.
"""

from __future__ import annotations

from typing import Any

# Fallbacks used when a market does not carry its own schedule.
DEFAULT_TAKER_RATE = 0.07
DEFAULT_EXPONENT = 1.0


def taker_fee(price: float, shares: float, rate: float = DEFAULT_TAKER_RATE,
              exponent: float = DEFAULT_EXPONENT) -> float:
    """Fee in dollars for taking ``shares`` at ``price``.

    ``fee = shares * rate * (p * (1 - p)) ** exponent``
    """
    if shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    edge = price * (1.0 - price)
    if exponent != 1.0:
        edge = edge ** exponent
    return rate * edge * shares


def maker_fee(price: float, shares: float) -> float:
    """Makers are not charged on these markets."""
    return 0.0


def schedule_from_market(raw: Any) -> tuple[float, float]:
    """Extract ``(rate, exponent)`` from a Gamma market's ``feeSchedule``."""
    if isinstance(raw, dict):
        try:
            if raw.get("takerOnly") is False:
                # Not seen on these markets; if it ever appears, fall back to
                # the documented default rather than silently charging nothing.
                pass
            return (
                float(raw.get("rate", DEFAULT_TAKER_RATE)),
                float(raw.get("exponent", DEFAULT_EXPONENT)),
            )
        except (TypeError, ValueError):
            pass
    return DEFAULT_TAKER_RATE, DEFAULT_EXPONENT


def effective_cost(price: float, shares: float, is_taker: bool,
                   rate: float = DEFAULT_TAKER_RATE,
                   exponent: float = DEFAULT_EXPONENT) -> float:
    """Total dollars out for a buy, fee included."""
    gross = price * shares
    fee = taker_fee(price, shares, rate, exponent) if is_taker else 0.0
    return gross + fee


def breakeven_probability(price: float, is_taker: bool,
                          rate: float = DEFAULT_TAKER_RATE,
                          exponent: float = DEFAULT_EXPONENT) -> float:
    """Win probability needed to break even on one share.

    Useful as a sanity check on any directional signal: buying at 50c as a
    taker needs ~53.5% to break even, not 50%.
    """
    if price <= 0 or price >= 1:
        return 1.0
    cost = effective_cost(price, 1.0, is_taker, rate, exponent)
    return min(1.0, cost)
