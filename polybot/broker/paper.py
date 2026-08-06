"""Paper execution.

Fills are simulated by walking the *real* order book rather than assuming the
mid or the top level. This is the difference between a paper record that
resembles live trading and one that flatters it: on these short windows the top
level is frequently a few dollars deep, so a $5 clip can easily clear two or
three levels, and any simulation that ignores that reports an entry price the
market would never have given.

Two further rules keep paper honest:

* A clip that the visible book cannot fill in full is **rejected**, not
  partially filled at a fictional price.
* Taker rebates are never credited. The reference wallet earns account-tier
  rebates; counting them here would let an unearned tier turn a losing strategy
  into a profitable-looking one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..fees import taker_fee
from ..feeds.polymarket import MarketWindow


@dataclass
class Fill:
    slug: str
    asset: str
    window: str
    side: str
    avg_price: float
    shares: float
    stake_usd: float
    fee_usd: float
    ts: float
    levels_cleared: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "asset": self.asset,
            "window": self.window,
            "side": self.side,
            "avg_price": round(self.avg_price, 4),
            "shares": round(self.shares, 4),
            "stake_usd": round(self.stake_usd, 2),
            "fee_usd": round(self.fee_usd, 4),
            "ts": self.ts,
            "levels_cleared": self.levels_cleared,
        }


@dataclass(frozen=True)
class BuyPlan:
    """A full-fill taker plan shared by the simulated and real brokers."""

    avg_price: float
    limit_price: float
    shares: float
    gross_usd: float
    fee_usd: float
    total_usd: float
    levels_cleared: int


class PaperBroker:
    """Simulated taker execution against live book depth."""

    mode = "paper"

    def __init__(self, config: dict[str, Any]) -> None:
        fees = config["fees"]
        self.taker_rate = float(fees.get("taker_rate", 0.07))
        self.fee_exponent = float(fees.get("exponent", 1.0))
        self.min_order_size = float(config["risk"]["trade_floor"]["min_trade_size_usd"])

    def plan_buy(
        self,
        market: MarketWindow,
        side: str,
        budget_usd: float,
        max_price: float | None = None,
    ) -> BuyPlan | None:
        """Build a fee-inclusive plan that can fill completely within budget."""
        book = market.book_for(side)
        if book is None or not book.asks.levels:
            return None
        if budget_usd < self.min_order_size:
            return None

        levels = [
            (price, size)
            for price, size in book.asks.levels
            if price > 0 and (max_price is None or price <= max_price + 1e-9)
        ]
        if not levels:
            return None

        def walk(gross_usd: float) -> tuple[float, float, float, int] | None:
            remaining = gross_usd
            spent = 0.0
            shares = 0.0
            worst_price = 0.0
            used = 0
            for price, size in levels:
                if remaining <= 1e-9:
                    break
                take = min(remaining, price * size)
                if take <= 0:
                    continue
                spent += take
                shares += take / price
                remaining -= take
                worst_price = price
                used += 1
            if remaining > 1e-6 or shares <= 0:
                return None
            return spent / shares, shares, worst_price, used

        # The slider is a total-cost ceiling. Find the largest two-decimal pUSD
        # maker amount whose notional plus protocol fee stays inside it.
        low, high = 0.0, budget_usd
        best: tuple[float, float, float, int] | None = None
        for _ in range(48):
            gross = (low + high) / 2.0
            walked = walk(gross)
            if walked is None:
                high = gross
                continue
            avg_price, shares, _, _ = walked
            fee = taker_fee(
                avg_price, shares, self.taker_rate, self.fee_exponent
            )
            if gross + fee <= budget_usd:
                low = gross
                best = walked
            else:
                high = gross

        gross_usd = int((low + 1e-9) * 100) / 100.0
        walked = walk(gross_usd)
        if walked is None:
            return None
        avg_price, shares, limit_price, used = walked
        if avg_price <= 0 or limit_price >= 1.0 or shares <= 0:
            return None
        fee_usd = taker_fee(
            avg_price, shares, self.taker_rate, self.fee_exponent
        )
        total_usd = gross_usd + fee_usd

        # Reject a thin book instead of silently shrinking a $5 decision into
        # a materially smaller order. One cent of amount rounding plus a small
        # floating-point tolerance is acceptable.
        if total_usd < budget_usd - 0.02 or total_usd > budget_usd + 1e-6:
            return None

        return BuyPlan(
            avg_price=avg_price,
            limit_price=limit_price,
            shares=shares,
            gross_usd=gross_usd,
            fee_usd=fee_usd,
            total_usd=total_usd,
            levels_cleared=used,
        )

    def buy(
        self,
        market: MarketWindow,
        side: str,
        stake_usd: float,
        max_price: float | None = None,
    ) -> Fill | None:
        """Buy within a total-cost budget, or return None if unfillable."""
        plan = self.plan_buy(market, side, stake_usd, max_price)
        if plan is None:
            return None

        return Fill(
            slug=market.slug,
            asset=market.asset,
            window=market.window,
            side=side,
            avg_price=plan.avg_price,
            shares=plan.shares,
            stake_usd=plan.total_usd,
            fee_usd=plan.fee_usd,
            ts=time.time(),
            levels_cleared=plan.levels_cleared,
        )

    def sell(self, market: MarketWindow, side: str, shares: float) -> Fill | None:
        """Liquidate ``shares`` into resting bids. Used by panic-close.

        Unlike a buy, a partial exit is genuinely useful — dumping what the book
        will absorb is better than holding everything — so this walks whatever
        depth exists and reports the true proceeds.
        """
        book = market.book_for(side)
        if book is None or not book.bids.levels or shares <= 0:
            return None

        remaining = shares
        proceeds = 0.0
        levels = 0
        for price, size in book.bids.levels:
            if remaining <= 1e-9:
                break
            take = min(remaining, size)
            proceeds += take * price
            remaining -= take
            levels += 1

        sold = shares - remaining
        if sold <= 0:
            return None

        return Fill(
            slug=market.slug,
            asset=market.asset,
            window=market.window,
            side=side,
            avg_price=proceeds / sold,
            shares=sold,
            stake_usd=proceeds,
            fee_usd=taker_fee(proceeds / sold, sold, self.taker_rate, self.fee_exponent),
            ts=time.time(),
            levels_cleared=levels,
        )

    @staticmethod
    def settle(
        side: str, shares: float, stake_usd: float, anchor: float, close: float
    ) -> dict[str, Any]:
        """Resolve a held position against the window's anchor and close price.

        Polymarket's rule: Up wins when the closing price is greater than *or
        equal to* the opening price. An exactly-flat window therefore pays Up,
        which matters more than it sounds — flat closes are common on 5m
        windows for the slower-moving assets.
        """
        won = (close >= anchor) if side.lower() == "up" else (close < anchor)
        payout = shares * 1.0 if won else 0.0
        return {
            "won": bool(won),
            "payout_usd": round(payout, 4),
            "pnl_usd": round(payout - stake_usd, 4),
            "close_price": close,
            "anchor_price": anchor,
        }
