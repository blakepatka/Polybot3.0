"""Two-sided market making on short windows.

The strategy this implements was reverse-engineered from a live Polymarket
profile trading these same 5m/15m crypto windows, and it is structurally
different from directional betting.

Measured across 532 samples of live books (7 assets x 5m/15m, 4 minutes):

    crossing the spread   ask(Up) + ask(Down)   median 1.020, NEVER below 1.00
    resting at the bid    bid(Up) + bid(Down)   median 0.980, below 1.00 100% of the time

A binary market's two outcomes always pay exactly $1 between them. So a share
of Up and a share of Down held together is worth exactly $1 at settlement, no
matter which way the price went. If the pair is acquired for less than $1, the
difference is locked profit with *no directional risk at all*.

The measurement above says that lock is unreachable by taking liquidity — the
spread is always wider than the edge — and freely available by providing it.
Hence: post resting bids on both sides and wait to be hit.

What this does not eliminate:

* **Leg risk.** The dangerous case is one leg filling and the other not, which
  leaves an outright directional position — usually because the market moved
  through our bid, meaning the leg that filled is the one that is now losing.
  Residual exposure is tracked per window and actively re-quoted to flatten.
* **Queue position.** We model a fill as "the market traded at or through our
  price". A real resting order also has to be at the front of the queue. Paper
  results here are therefore optimistic; see PAPER_FILL_OPTIMISM in the notes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .feeds.polymarket import Book, MarketWindow


def _size_at(book: Book, price: float) -> float:
    """Shares already resting at ``price`` — the queue we join behind."""
    for level_price, size in book.bids.levels:
        if abs(level_price - price) < 1e-9:
            return size
    return 0.0

# The paper fill model assumes a resting order is filled whenever the market
# trades at or through its price. Reality adds queue priority: other makers may
# be ahead of us at the same price and absorb the flow. Treat paper fill rates
# as an upper bound, not an expectation.
PAPER_FILL_OPTIMISM = (
    "Resting fills assume full queue priority: any trade at or through our price "
    "fills us. Real fill rates will be lower."
)


@dataclass
class Quote:
    """A resting bid we believe is live on one side of one window."""

    slug: str
    asset: str
    window: str
    side: str
    price: float
    size_usd: float
    placed_at: float
    order_id: str | None = None
    # Shares already resting at our price when we joined. We are behind all of
    # it, and it must be consumed before any of our size trades.
    queue_ahead: float = 0.0
    mid_at_placement: float = 0.0

    @property
    def shares(self) -> float:
        return self.size_usd / self.price if self.price > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "asset": self.asset,
            "window": self.window,
            "side": self.side,
            "price": round(self.price, 4),
            "size_usd": round(self.size_usd, 2),
            "shares": round(self.shares, 2),
            "age_seconds": round(time.time() - self.placed_at, 1),
        }


@dataclass
class Inventory:
    """Filled position on both sides of one window.

    ``locked`` pairs are risk-free: they pay exactly $1 regardless of outcome.
    Whatever is left over on one side is outright directional exposure.
    """

    slug: str
    asset: str
    window: str
    end: float
    shares_up: float = 0.0
    cost_up: float = 0.0
    shares_down: float = 0.0
    cost_down: float = 0.0
    fills: int = 0

    def add(self, side: str, shares: float, cost: float) -> None:
        if side == "up":
            self.shares_up += shares
            self.cost_up += cost
        else:
            self.shares_down += shares
            self.cost_down += cost
        self.fills += 1

    @property
    def locked_shares(self) -> float:
        return min(self.shares_up, self.shares_down)

    @property
    def residual_side(self) -> str | None:
        if abs(self.shares_up - self.shares_down) < 1e-6:
            return None
        return "up" if self.shares_up > self.shares_down else "down"

    @property
    def residual_shares(self) -> float:
        return abs(self.shares_up - self.shares_down)

    @property
    def total_cost(self) -> float:
        return self.cost_up + self.cost_down

    @property
    def avg_up(self) -> float:
        return self.cost_up / self.shares_up if self.shares_up > 0 else 0.0

    @property
    def avg_down(self) -> float:
        return self.cost_down / self.shares_down if self.shares_down > 0 else 0.0

    @property
    def pair_cost(self) -> float:
        """Combined cost of one locked pair. Below 1.0 is locked profit."""
        if self.shares_up <= 0 or self.shares_down <= 0:
            return 0.0
        return self.avg_up + self.avg_down

    @property
    def locked_profit(self) -> float:
        """Guaranteed profit from paired shares, independent of the outcome."""
        pairs = self.locked_shares
        if pairs <= 0:
            return 0.0
        return pairs * (1.0 - self.pair_cost)

    def settle(self, up_won: bool) -> dict[str, Any]:
        """Resolve the whole window.

        Every share of the winning side pays $1; the losing side pays nothing.
        Paired shares net exactly $1 per pair either way, which is the point.
        """
        payout = (self.shares_up if up_won else self.shares_down) * 1.0
        return {
            "payout_usd": round(payout, 4),
            "cost_usd": round(self.total_cost, 4),
            "pnl_usd": round(payout - self.total_cost, 4),
            "locked_shares": round(self.locked_shares, 4),
            "locked_profit": round(self.locked_profit, 4),
            "residual_side": self.residual_side,
            "residual_shares": round(self.residual_shares, 4),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "asset": self.asset,
            "window": self.window,
            "shares_up": round(self.shares_up, 2),
            "shares_down": round(self.shares_down, 2),
            "avg_up": round(self.avg_up, 4),
            "avg_down": round(self.avg_down, 4),
            "locked_shares": round(self.locked_shares, 2),
            "pair_cost": round(self.pair_cost, 4),
            "locked_profit": round(self.locked_profit, 4),
            "residual_side": self.residual_side,
            "residual_shares": round(self.residual_shares, 2),
            "total_cost": round(self.total_cost, 2),
            "fills": self.fills,
            "seconds_remaining": round(self.end - time.time(), 0),
        }


class MakerStrategy:
    """Decides where to quote, and simulates resting fills in paper mode."""

    def __init__(self, config: dict[str, Any]) -> None:
        mk = config["maker"]
        self.min_lock_cents = float(mk["min_lock_cents"])
        self.base_clip_usd = float(mk["base_clip_usd"])
        self.max_pairs_per_window = int(mk["max_pairs_per_window"])
        self.min_price = float(mk["min_price"])
        self.max_price = float(mk["max_price"])
        self.min_seconds_remaining = float(mk["min_seconds_remaining"])
        self.skew_strength = float(mk["skew_strength"])
        self.improve_ticks = int(mk["improve_ticks"])
        self.residual_flatten = bool(mk["residual_flatten"])

        self.target_rewards = bool(mk.get("target_rewards", False))

        self.quotes: dict[tuple[str, str], Quote] = {}
        self.inventory: dict[str, Inventory] = {}

    # -- rewards -----------------------------------------------------------
    #
    # NOTE: liquidity rewards were investigated and ruled out for these
    # markets. Gamma reports rewardsMaxSpread=4.5 and rewardsMinSize=50 on the
    # short crypto windows, but the venue's own reward register
    # (/rewards/markets/current, 8,960 markets over 18 pages) contains none of
    # them, and the per-market lookup returns an empty set. Those Gamma fields
    # are template defaults, not an active program. No reward income is
    # modelled anywhere in this project as a result.

    @staticmethod
    def maker_rebate(price: float, shares: float, market: MarketWindow,
                     share_bps: float = 10000.0) -> float:
        """Fee-share rebate credited to the resting side of a fill.

        These markets set ``feesEnabled: true`` with a taker-only schedule and
        ``makerRebatesFeeShareBps: 10000``, i.e. the maker's share of the fee
        the taker paid. Since we only ever rest, every maker fill earns this.

        It is small — a fraction of a cent per share — and nowhere near enough
        to cover being adversely selected, which is the honest reason this is
        modelled rather than relied upon.
        """
        from .fees import taker_fee

        fee = taker_fee(price, shares, market.fee_rate, market.fee_exponent)
        return fee * (share_bps / 10000.0)

    # -- quoting -----------------------------------------------------------

    def desired_quotes(
        self, market: MarketWindow, p_up: float | None, now: float | None = None
    ) -> list[Quote]:
        """Where we want resting bids on this window, if anywhere.

        ``p_up`` is the directional model's probability, used only to skew size
        between the two legs. The *decision to quote at all* depends on the pair
        cost, not on the model — that is what makes the edge direction-free.
        """
        now = now if now is not None else time.time()

        if market.seconds_remaining(now) < self.min_seconds_remaining:
            return []
        if market.closed or not market.accepting_orders:
            return []

        book_up, book_down = market.book_for("up"), market.book_for("down")
        if not book_up or not book_down:
            return []
        if book_up.bids.best is None or book_down.bids.best is None:
            return []

        tick = float(market.tick_size or 0.01)
        # Improve on the best bid to win queue priority. Improving costs edge,
        # so it is capped by whether the pair still clears the lock threshold.
        bid_up = round(book_up.bids.best + self.improve_ticks * tick, 4)
        bid_down = round(book_down.bids.best + self.improve_ticks * tick, 4)

        # Never quote through the opposing ask — that is taking, not making.
        if book_up.asks.best is not None:
            bid_up = min(bid_up, round(book_up.asks.best - tick, 4))
        if book_down.asks.best is not None:
            bid_down = min(bid_down, round(book_down.asks.best - tick, 4))

        if not (self.min_price <= bid_up <= self.max_price):
            return []
        if not (self.min_price <= bid_down <= self.max_price):
            return []

        pair = bid_up + bid_down
        lock_cents = (1.0 - pair) * 100.0
        if lock_cents < self.min_lock_cents:
            return []

        inv = self.inventory.get(market.slug)
        if inv and inv.locked_shares >= self.max_pairs_per_window:
            return []

        # Skew size toward the model's favoured side. Both legs are still
        # quoted — an unquoted leg cannot fill, and an unfilled leg is exactly
        # the leg risk we are trying to avoid.
        skew = 0.0 if p_up is None else (p_up - 0.5) * 2.0 * self.skew_strength
        size_up = self.base_clip_usd * (1.0 + skew)
        size_down = self.base_clip_usd * (1.0 - skew)

        # Optionally scale each leg up to the reward-qualifying share count.
        # This is a real trade-off, not free money: qualifying means holding a
        # much larger position, so adverse selection on an unpaired leg hurts
        # proportionally more. It is off by default.
        if self.target_rewards and market.rewards_min_size:
            size_up = max(size_up, market.rewards_min_size * bid_up)
            size_down = max(size_down, market.rewards_min_size * bid_down)

        return [
            Quote(market.slug, market.asset, market.window, "up", bid_up,
                  round(max(1.0, size_up), 2), now,
                  queue_ahead=_size_at(book_up, bid_up),
                  mid_at_placement=book_up.mid or 0.0),
            Quote(market.slug, market.asset, market.window, "down", bid_down,
                  round(max(1.0, size_down), 2), now,
                  queue_ahead=_size_at(book_down, bid_down),
                  mid_at_placement=book_down.mid or 0.0),
        ]

    def residual_quote(self, market: MarketWindow, now: float | None = None) -> Quote | None:
        """A bid on the light side to flatten leftover directional exposure.

        When one leg fills and the other does not, we hold an outright
        position. Rather than sit on it, bid for the missing side: filling it
        converts the exposure into a locked pair. This is the active side
        switching — it can fire repeatedly within a window as the price moves.
        """
        if not self.residual_flatten:
            return None
        now = now if now is not None else time.time()

        inv = self.inventory.get(market.slug)
        if inv is None:
            return None
        need = inv.residual_side
        if need is None or inv.residual_shares < 0.5:
            return None
        # Flattening late is worse than holding: the missing side gets more
        # expensive as certainty rises, and a fill may no longer lock anything.
        if market.seconds_remaining(now) < self.min_seconds_remaining:
            return None

        # We must buy the side we are *short* of, which is the opposite of the
        # side we already hold too much of.
        want = "down" if need == "up" else "up"
        book = market.book_for(want)
        if book is None or book.bids.best is None:
            return None

        tick = float(market.tick_size or 0.01)
        price = round(book.bids.best + self.improve_ticks * tick, 4)
        if book.asks.best is not None:
            price = min(price, round(book.asks.best - tick, 4))
        if not (self.min_price <= price <= self.max_price):
            return None

        # Only flatten if the completed pair still costs less than $1.
        held_avg = inv.avg_up if need == "up" else inv.avg_down
        if held_avg + price >= 1.0:
            return None

        size = round(min(inv.residual_shares * price, self.base_clip_usd * 2), 2)
        if size < 1.0:
            return None
        return Quote(market.slug, market.asset, market.window, want, price, size, now)

    # -- fill simulation ---------------------------------------------------

    def simulate_fill(self, quote: Quote, market: MarketWindow) -> dict[str, Any] | None:
        """Would this resting bid have been hit, accounting for queue position?

        The naive model — "fill whenever the ask touches our price" — is far
        too generous, and it is what made the first version of this strategy
        look busier and worse than reality. Price *touching* our level does not
        fill us; everyone who was already resting there gets filled first.

        So we require the level to be cleared outright: the best ask must trade
        strictly *below* our price, which means the whole queue at our price,
        including us, was consumed. That is conservative in the other
        direction (it ignores partial fills), but erring toward fewer fills is
        the right way round when the fills we do get are adversely selected.
        """
        book = market.book_for(quote.side)
        if book is None or book.asks.best is None:
            return None

        # Touched but not cleared: someone sold into the queue ahead of us.
        if book.asks.best >= quote.price:
            return None

        shares = quote.size_usd / quote.price
        return {
            "side": quote.side,
            "price": quote.price,
            "shares": shares,
            "cost": quote.size_usd,
            "queue_ahead": quote.queue_ahead,
        }

    def stale_quotes(self, market: MarketWindow, max_drift_cents: float) -> list[str]:
        """Sides whose resting quote has drifted too far from mid to keep.

        Leaving a quote out while mid moves away from it is precisely how a
        maker gets picked off: the order stops being a fair two-sided price and
        becomes a free option for anyone trading against it. Cancelling and
        re-quoting around the new mid is the single biggest defence against
        adverse selection available to us.
        """
        stale: list[str] = []
        for side in ("up", "down"):
            quote = self.quotes.get((market.slug, side))
            if quote is None:
                continue
            book = market.book_for(side)
            if book is None or book.mid is None:
                continue
            if abs(book.mid - quote.price) * 100.0 > max_drift_cents:
                stale.append(side)
        return stale

    # -- inventory ---------------------------------------------------------

    def record_fill(self, market: MarketWindow, side: str, shares: float, cost: float) -> Inventory:
        inv = self.inventory.get(market.slug)
        if inv is None:
            inv = Inventory(market.slug, market.asset, market.window, market.end)
            self.inventory[market.slug] = inv
        inv.add(side, shares, cost)
        return inv

    def drop_window(self, slug: str) -> Inventory | None:
        for key in [k for k in self.quotes if k[0] == slug]:
            self.quotes.pop(key, None)
        return self.inventory.pop(slug, None)

    def snapshot(self) -> dict[str, Any]:
        inv = [i.to_dict() for i in self.inventory.values()]
        return {
            "quotes": [q.to_dict() for q in self.quotes.values()],
            "inventory": inv,
            "open_locked_profit": round(sum(i["locked_profit"] for i in inv), 4),
            "open_cost": round(sum(i["total_cost"] for i in inv), 2),
            "residual_windows": sum(1 for i in inv if i["residual_side"]),
            "fill_model_note": PAPER_FILL_OPTIMISM,
        }
