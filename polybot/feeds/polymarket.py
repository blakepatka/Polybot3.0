"""Polymarket market data: window discovery and CLOB order books.

Discovery is deterministic rather than search-based. Every Up-or-Down market
has the slug ``{asset}-updown-{window}-{unix_start}`` where ``unix_start`` is
the window's opening instant aligned to its own duration. That means the
current window's slug can be *computed*, and we fetch it directly.

This matters: Polymarket pre-creates roughly a day of windows in advance, so
listing endpoints sorted by start date return tomorrow's markets, not the one
trading right now. Computing the slug sidesteps that entirely.

Prices come from the CLOB book, not from Gamma's ``outcomePrices`` — the latter
lags the book noticeably on these fast-moving short windows.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

WINDOW_SECONDS: dict[str, int] = {"5m": 300, "15m": 900}


def window_start(window: str, now: float | None = None) -> int:
    """Opening instant of the window currently in progress."""
    dur = WINDOW_SECONDS[window]
    now = now if now is not None else time.time()
    return int(now // dur) * dur


def build_slug(asset: str, window: str, start: int) -> str:
    return f"{asset}-updown-{window}-{start}"


@dataclass
class BookSide:
    """One side of the book, sorted best-price-first."""

    levels: list[tuple[float, float]] = field(default_factory=list)

    @property
    def best(self) -> float | None:
        return self.levels[0][0] if self.levels else None

    def depth_usd(self, levels: int = 5) -> float:
        return sum(price * size for price, size in self.levels[:levels])

    def walk(self, usd: float) -> tuple[float, float] | None:
        """Consume ``usd`` of notional across levels.

        Returns ``(avg_price, shares)``, or None if the book cannot fill the
        whole amount. Partial fills are rejected rather than silently shrunk so
        that paper results never assume liquidity that was not there.
        """
        remaining = usd
        shares = 0.0
        spent = 0.0
        for price, size in self.levels:
            if price <= 0:
                continue
            level_usd = price * size
            take = min(remaining, level_usd)
            if take <= 0:
                break
            shares += take / price
            spent += take
            remaining -= take
            if remaining <= 1e-9:
                break
        if remaining > 1e-6 or shares <= 0:
            return None
        return spent / shares, shares


@dataclass
class Book:
    asks: BookSide = field(default_factory=BookSide)
    bids: BookSide = field(default_factory=BookSide)
    ts: float = 0.0

    @property
    def mid(self) -> float | None:
        if self.asks.best is not None and self.bids.best is not None:
            return (self.asks.best + self.bids.best) / 2.0
        return self.asks.best if self.asks.best is not None else self.bids.best

    @property
    def spread(self) -> float | None:
        if self.asks.best is None or self.bids.best is None:
            return None
        return self.asks.best - self.bids.best


@dataclass
class MarketWindow:
    """One tradeable Up-or-Down window."""

    asset: str
    window: str
    start: int
    end: int
    slug: str
    condition_id: str
    question: str
    token_up: str
    token_down: str
    tick_size: float = 0.01
    min_order_size: float = 5.0
    # Liquidity-reward qualification, read from the market. A resting order
    # only earns if it is within `rewards_max_spread` cents of mid AND at least
    # `rewards_min_size` shares — 50 shares is far above a $5 clip, so small
    # quotes earn nothing at all.
    rewards_max_spread: float = 0.0
    rewards_min_size: float = 0.0
    fee_rate: float = 0.07
    fee_exponent: float = 1.0
    accepting_orders: bool = True
    closed: bool = False
    liquidity: float = 0.0
    books: dict[str, Book] = field(default_factory=dict)
    fetched_at: float = 0.0

    @property
    def key(self) -> str:
        return self.slug

    def seconds_remaining(self, now: float | None = None) -> float:
        return self.end - (now if now is not None else time.time())

    def seconds_elapsed(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.start

    def token_for(self, side: str) -> str:
        return self.token_up if side.lower() == "up" else self.token_down

    def book_for(self, side: str) -> Book | None:
        return self.books.get(side.lower())

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "slug": self.slug,
            "asset": self.asset,
            "window": self.window,
            "start": self.start,
            "end": self.end,
            "question": self.question,
            "condition_id": self.condition_id,
            "seconds_remaining": round(self.seconds_remaining(), 1),
            "accepting_orders": self.accepting_orders,
            "closed": self.closed,
            "liquidity": self.liquidity,
            "tick_size": self.tick_size,
        }
        for side in ("up", "down"):
            book = self.books.get(side)
            if book:
                out[side] = {
                    "bid": book.bids.best,
                    "ask": book.asks.best,
                    "mid": book.mid,
                    "ask_depth_usd": round(book.asks.depth_usd(), 2),
                    "bid_depth_usd": round(book.bids.depth_usd(), 2),
                }
        return out


class PolymarketFeed:
    """Discovers live windows and keeps their order books fresh."""

    def __init__(self, assets: list[str], windows: list[str]) -> None:
        self.assets = assets
        self.windows = [w for w in windows if w in WINDOW_SECONDS]
        self.markets: dict[str, MarketWindow] = {}
        self.health: dict[str, dict[str, Any]] = {
            "polymarket_gamma": {
                "active": False, "latency": None, "error": None,
                "url": "gamma-api.polymarket.com/events",
                "role": "Window discovery by computed slug",
                "kind": "REST",
            },
            "polymarket_clob": {
                "active": False, "latency": None, "error": None,
                "url": "clob.polymarket.com/book",
                "role": "Order book depth — prices and paper fills",
                "kind": "REST",
            },
        }
        self._client: httpx.AsyncClient | None = None
        self._discovery_misses = 0

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(12.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                headers={"User-Agent": "polybot3.0"},
            )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def resolution(self, slug: str) -> str | None:
        """The winning side the venue published for ``slug``.

        ``"up"``, ``"down"``, or None when the window has not resolved yet, was
        voided, or cannot be read. This is the authority on who won: settling
        a spot anchor against a spot close only ever *infers* the outcome, and
        the exchanges we read are not the Chainlink stream the venue resolves
        on. Near a coin-flip boundary — which is most of a five-minute window —
        that basis decides the verdict, so the inference is wrong often enough
        to invert a strategy's measured P/L.

        Returns None rather than guessing. The caller keeps the position open
        and retries, and falls back to the spot comparison only once the window
        is old enough that waiting is worse than approximating.
        """
        assert self._client is not None
        try:
            resp = await self._client.get(f"{GAMMA}/events", params={"slug": slug})
            if resp.status_code != 200:
                return None
            events = resp.json()
            if not events:
                return None
            markets = events[0].get("markets") or []
            if not markets:
                return None
            market = markets[0]
            if not market.get("closed"):
                return None

            outcomes = market.get("outcomes")
            prices = market.get("outcomePrices")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)
            if not outcomes or not prices or len(outcomes) != len(prices):
                return None

            # A resolved binary market pays one side in full. Anything else —
            # an unresolved pair still marked closed, or a 50/50 void — is not
            # an outcome we may settle against.
            for label, price in zip(outcomes, prices):
                if float(price) >= 0.99:
                    side = str(label).strip().lower()
                    return side if side in ("up", "down") else None
            return None
        except Exception:
            return None

    async def discover(self, include_next: bool = True) -> list[MarketWindow]:
        """Fetch the in-progress window for every asset/window pair.

        ``include_next`` also pulls the upcoming window, which lets the engine
        have a book ready the instant a new window opens instead of losing the
        first poll cycle to discovery.
        """
        assert self._client is not None
        now = time.time()
        targets: list[tuple[str, str, int]] = []
        for asset in self.assets:
            for window in self.windows:
                dur = WINDOW_SECONDS[window]
                start = window_start(window, now)
                targets.append((asset, window, start))
                if include_next:
                    targets.append((asset, window, start + dur))

        started = time.time()
        results = await asyncio.gather(
            *(self._fetch_window(a, w, s) for a, w, s in targets),
            return_exceptions=True,
        )
        found = [r for r in results if isinstance(r, MarketWindow)]

        ok = bool(found)
        self.health["polymarket_gamma"].update(
            active=ok,
            latency=round(time.time() - started, 3),
            error=None if ok else "no windows resolved",
            polled=len(targets),
            resolved=len(found),
        )
        self._discovery_misses = len(targets) - len(found)

        for market in found:
            existing = self.markets.get(market.key)
            if existing:
                # Preserve books across discovery so the engine never sees a
                # market blink to "no price" mid-window.
                market.books = existing.books
            self.markets[market.key] = market

        self._evict(now)
        return found

    def _evict(self, now: float) -> None:
        """Drop windows that ended more than a few minutes ago."""
        stale = [k for k, m in self.markets.items() if m.end < now - 600]
        for key in stale:
            self.markets.pop(key, None)

    async def _fetch_window(self, asset: str, window: str, start: int) -> MarketWindow | None:
        assert self._client is not None
        slug = build_slug(asset, window, start)
        cached = self.markets.get(slug)
        # Static metadata never changes once created; refresh it sparingly.
        if cached and time.time() - cached.fetched_at < 120 and not cached.closed:
            return cached
        try:
            resp = await self._client.get(f"{GAMMA}/events", params={"slug": slug})
            if resp.status_code != 200:
                return None
            events = resp.json()
            if not events:
                return None
            event = events[0]
            markets = event.get("markets") or []
            if not markets:
                return None
            market = markets[0]

            tokens = market.get("clobTokenIds")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            outcomes = market.get("outcomes")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if not tokens or len(tokens) < 2 or not outcomes:
                return None

            # Do not assume index 0 is "Up" — read the outcome labels.
            token_map = {str(o).strip().lower(): t for o, t in zip(outcomes, tokens)}
            token_up = token_map.get("up")
            token_down = token_map.get("down")
            if not token_up or not token_down:
                return None

            from ..fees import schedule_from_market

            _fee_rate, _fee_exp = schedule_from_market(market.get("feeSchedule"))

            return MarketWindow(
                asset=asset,
                window=window,
                start=start,
                end=start + WINDOW_SECONDS[window],
                slug=slug,
                condition_id=market.get("conditionId", ""),
                question=market.get("question") or event.get("title") or slug,
                token_up=token_up,
                token_down=token_down,
                tick_size=float(market.get("orderPriceMinTickSize") or 0.01),
                min_order_size=float(market.get("orderMinSize") or 5.0),
                rewards_max_spread=float(market.get("rewardsMaxSpread") or 0.0),
                rewards_min_size=float(market.get("rewardsMinSize") or 0.0),
                fee_rate=_fee_rate,
                fee_exponent=_fee_exp,
                accepting_orders=bool(market.get("acceptingOrders", True)),
                closed=bool(market.get("closed", False)) or bool(event.get("closed", False)),
                liquidity=float(event.get("liquidity") or 0.0),
                fetched_at=time.time(),
            )
        except Exception:
            return None

    async def refresh_books(self, markets: list[MarketWindow]) -> None:
        """Pull both sides of the book for each supplied market."""
        assert self._client is not None
        if not markets:
            return
        started = time.time()
        jobs = []
        for market in markets:
            jobs.append(self._fetch_book(market, "up", market.token_up))
            jobs.append(self._fetch_book(market, "down", market.token_down))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        self.health["polymarket_clob"].update(
            active=ok > 0,
            latency=round(time.time() - started, 3),
            error=None if ok > 0 else "no books returned",
            polled=len(jobs),
            resolved=ok,
        )

    async def refresh_book(self, market: MarketWindow, side: str) -> bool:
        """Refresh one outcome immediately before a real-money order."""
        token = market.token_for(side)
        return await self._fetch_book(market, side, token)

    async def _fetch_book(self, market: MarketWindow, side: str, token: str) -> bool:
        assert self._client is not None
        try:
            resp = await self._client.get(f"{CLOB}/book", params={"token_id": token})
            if resp.status_code != 200:
                return False
            data = resp.json()
            bids = sorted(
                ((float(x["price"]), float(x["size"])) for x in data.get("bids") or []),
                key=lambda p: -p[0],
            )
            asks = sorted(
                ((float(x["price"]), float(x["size"])) for x in data.get("asks") or []),
                key=lambda p: p[0],
            )
            market.books[side] = Book(
                asks=BookSide(asks), bids=BookSide(bids), ts=time.time()
            )
            return True
        except Exception:
            return False

    def live_markets(self, now: float | None = None) -> list[MarketWindow]:
        """Windows currently in progress and accepting orders."""
        now = now if now is not None else time.time()
        return [
            m
            for m in self.markets.values()
            if m.start <= now < m.end and not m.closed and m.accepting_orders
        ]

    def get(self, slug: str) -> MarketWindow | None:
        return self.markets.get(slug)

    def snapshot(self) -> dict[str, Any]:
        return {
            "tracked": len(self.markets),
            "live": len(self.live_markets()),
            "discovery_misses": self._discovery_misses,
            "health": self.health,
        }
