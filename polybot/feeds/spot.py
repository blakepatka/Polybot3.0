"""Spot reference prices from Coinbase and Kraken.

These markets resolve against a Chainlink data stream, not against either of
these exchanges. We poll centralised venues anyway because they lead the
Chainlink stream by a second or two, and that lead is the entire edge the
short-window strategy trades on. Treat the numbers here as a *predictor* of the
resolution price, never as the resolution price itself.

Each asset keeps a rolling tick buffer, which serves double duty: momentum for
the signal, and realized volatility for the risk regime.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..settings import ASSET_SPEC

COINBASE_TICKER = "https://api.exchange.coinbase.com/products/{product}/ticker"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{product}/candles"
KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker"
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"

# How far back to seed the tick buffer on startup. Must exceed the longest
# trading window (15m) so the engine can anchor windows already in progress
# instead of idling until a fresh one opens.
BACKFILL_SECONDS = 1800

# Grid spacing for volatility estimation. Long enough to sit above bid-ask
# bounce and irregular poll spacing, short enough to keep a useful sample count
# inside a 15-minute lookback.
RESAMPLE_STEP = 20.0

# Ticks older than this are dropped. Must exceed the longest lookback any
# consumer asks for (the 15m volatility window) with room to spare.
BUFFER_SECONDS = 1200.0


# The two price series this buffer can hold, and the reason they must never be
# read as one. EXCHANGE is Coinbase/Kraken: the *predictor*, which leads by a
# second or two. RESOLUTION is the Chainlink stream the venue actually settles
# on. They track each other but are not equal — the gap between them is a real,
# drifting basis worth several dollars on BTC.
#
# Any calculation that subtracts one price from another — drift, anchor-vs-spot,
# anchor-vs-close — must take both ends from the SAME series, or it measures the
# basis instead of the market. A window anchored on one series and settled on
# the other books that basis as profit or loss that never existed.
EXCHANGE = "exchange"
RESOLUTION = "chainlink"


@dataclass(slots=True)
class Tick:
    ts: float
    price: float
    # Which series this price came from. Untagged ticks are exchange prices,
    # so that a caller predating the split keeps its old meaning.
    source: str = EXCHANGE


@dataclass
class AssetState:
    """Rolling price history and derived statistics for one asset.

    Holds more than one price series. Read them with the ``source`` argument
    on :meth:`price_at_or_before`, :meth:`latest_from` and :meth:`resample`,
    and keep both ends of any comparison on the same one — see the note on
    :data:`EXCHANGE` above.
    """

    asset: str
    ticks: deque[Tick] = field(default_factory=lambda: deque(maxlen=4096))
    last_price: float | None = None
    last_update: float = 0.0
    sources: set[str] = field(default_factory=set)
    backfilled: bool = False
    # Most recent (ts, price) per series. Kept alongside the buffer so the hot
    # path never scans backwards to find the newest tick of one source.
    latest_by_source: dict[str, tuple[float, float]] = field(default_factory=dict)

    def push(
        self, price: float, ts: float | None = None, source: str = EXCHANGE
    ) -> None:
        if price is None or price <= 0 or not math.isfinite(price):
            return
        ts = time.time() if ts is None else ts
        tick = Tick(ts, price, source)
        previous = self.latest_by_source.get(source)
        if previous is None or ts >= previous[0]:
            self.latest_by_source[source] = (ts, price)
        if self.ticks and ts < self.ticks[-1].ts:
            # Multiple venues do not always report in arrival order. Keep the
            # buffer chronological because anchors, resampling and change
            # calculations all stop walking once they pass their target time.
            ordered = sorted((*self.ticks, tick), key=lambda row: row.ts)
            self.ticks.clear()
            self.ticks.extend(ordered)
        else:
            self.ticks.append(tick)
        if ts >= self.last_update:
            self.last_price = price
            self.last_update = ts
        cutoff = time.time() - BUFFER_SECONDS
        while self.ticks and self.ticks[0].ts < cutoff:
            self.ticks.popleft()

    def seed(self, points: list[tuple[float, float]]) -> None:
        """Insert historical ``(ts, price)`` points and re-sort the buffer.

        Used once at startup. Kept separate from :meth:`push` because it must
        not advance ``last_price`` — a stale candle close is not the live price.
        """
        if not points:
            return
        for ts, price in points:
            if price and price > 0 and math.isfinite(price):
                self.ticks.append(Tick(ts, price, EXCHANGE))
        ordered = sorted(self.ticks, key=lambda t: t.ts)
        cutoff = time.time() - BUFFER_SECONDS
        self.ticks.clear()
        self.ticks.extend(t for t in ordered if t.ts >= cutoff)
        self.backfilled = True

    def price_at_or_before(
        self, ts: float, source: str | None = None
    ) -> float | None:
        """Most recent price at/just before ``ts``, from one series.

        Used to anchor a window's opening price. Returns None when that series
        does not reach back that far — the caller must not guess, since a wrong
        anchor flips the sign of every signal in that window, and must not
        silently substitute the other series, since that books the basis
        between them as a price move.
        """
        best: float | None = None
        for tick in self.ticks:
            if tick.ts > ts:
                break
            if source is None or tick.source == source:
                best = tick.price
        return best

    def latest_from(self, source: str) -> tuple[float, float] | None:
        """Newest ``(ts, price)`` from one series, or None if it has none."""
        return self.latest_by_source.get(source)

    def has_source(self, source: str) -> bool:
        return source in self.latest_by_source

    def pricing_source(self, anchor_ts: float, now: float, max_age: float) -> str | None:
        """Pick the one series to measure a window's drift on.

        Exchange spot is preferred because it *leads* the resolution stream,
        and that lead is the entire edge. Chainlink is the fallback rather
        than the default: it is what the venue settles on, so it is never
        wrong, merely later.

        The choice is deliberately all-or-nothing. A series qualifies only if
        it covers both ends — the window's open and a fresh reading now — so
        that drift is a difference within one series. Returning None (and
        trading nothing) is correct when neither does; splicing the two is how
        an inter-feed basis becomes a phantom signal.
        """
        for candidate in (EXCHANGE, RESOLUTION):
            newest = self.latest_by_source.get(candidate)
            if newest is None or now - newest[0] > max_age:
                continue
            if self.price_at_or_before(anchor_ts, candidate) is None:
                continue
            return candidate
        return None

    def change_bps(self, lookback_seconds: float) -> float | None:
        """Return over ``lookback_seconds``, in basis points."""
        if self.last_price is None or not self.ticks:
            return None
        cutoff = time.time() - lookback_seconds
        base: float | None = None
        for tick in self.ticks:
            if tick.ts >= cutoff:
                base = tick.price
                break
        if base is None or base <= 0:
            return None
        return (self.last_price / base - 1.0) * 10_000.0

    def resample(
        self,
        lookback_seconds: float,
        step: float = RESAMPLE_STEP,
        source: str | None = None,
    ) -> list[float]:
        """Prices on a uniform time grid, most recent last.

        Volatility must never be measured off raw ticks. Polls land at
        irregular, sometimes near-zero, intervals, and the bid-ask bounce means
        consecutive ticks differ without the price having actually moved.
        Both effects inflate a naive estimate by an order of magnitude, which
        would make every real signal look like noise. Sampling on a fixed grid
        removes both.

        Pass ``source`` for the same reason drift needs it: two series
        interleaved in one buffer alternate around their basis, and that
        sawtooth is indistinguishable from volatility to this estimator.
        """
        now = time.time()
        start = now - lookback_seconds
        usable = [
            t for t in self.ticks
            if t.ts >= start and (source is None or t.source == source)
        ]
        if len(usable) < 3:
            return []

        out: list[float] = []
        idx = 0
        last: float | None = None
        grid = start
        while grid <= now:
            while idx < len(usable) and usable[idx].ts <= grid:
                last = usable[idx].price
                idx += 1
            if last is not None:
                out.append(last)
            grid += step
        return out

    @property
    def primary_source(self) -> str:
        """The series to describe this asset with when no window is in view."""
        return EXCHANGE if self.has_source(EXCHANGE) else RESOLUTION

    def realized_vol_bps(self, lookback_seconds: float = 900.0) -> float | None:
        """Realized volatility over the lookback, in bps.

        Standard deviation of grid-sampled log returns scaled to the full
        lookback. This gates the trading regime: above the configured ceiling
        the lead/lag edge stops being reliable — so it is measured on a single
        series, or the inter-feed sawtooth reads as a volatility spike and
        halts trading on a market that never moved.
        """
        prices = self.resample(lookback_seconds, source=self.primary_source)
        if len(prices) < 6:
            return None
        rets = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
            if prices[i - 1] > 0 and prices[i] > 0
        ]
        if len(rets) < 4:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        # Scale a per-step deviation up to the whole lookback.
        steps = lookback_seconds / RESAMPLE_STEP
        return math.sqrt(var) * math.sqrt(steps) * 10_000.0

    def snapshot(self) -> dict[str, Any]:
        spec = ASSET_SPEC.get(self.asset, {})
        return {
            "asset": self.asset,
            "label": spec.get("label", self.asset.upper()),
            "price": self.last_price,
            "decimals": spec.get("decimals", 2),
            "age_seconds": (time.time() - self.last_update) if self.last_update else None,
            "change_bps_60s": self.change_bps(60.0),
            "change_bps_300s": self.change_bps(300.0),
            "vol_bps": self.realized_vol_bps(),
            "sources": sorted(self.sources),
            "ticks": len(self.ticks),
            "backfilled": self.backfilled,
        }


class SpotFeed:
    """Polls Coinbase and Kraken concurrently and merges into per-asset state."""

    def __init__(self, assets: list[str], interval: float = 1.0) -> None:
        self.assets = [a for a in assets if a in ASSET_SPEC]
        self.interval = interval
        self.state: dict[str, AssetState] = {a: AssetState(a) for a in self.assets}
        # `url` and `role` are static descriptors so the dashboard can show
        # where a number came from and what it is used for, not merely whether
        # the fetch succeeded.
        self.source_health: dict[str, dict[str, Any]] = {
            "coinbase": {
                "active": False, "latency": None, "error": None,
                "url": "api.exchange.coinbase.com",
                "role": "Primary spot reference + 1m backfill",
                "kind": "REST",
            },
            "kraken": {
                "active": False, "latency": None, "error": None,
                "url": "api.kraken.com",
                "role": "Secondary spot; sole source for BNB and HYPE",
                "kind": "REST",
            },
        }
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "polybot3.0"},
        )
        # Seed history before the loop starts so the first scan already has
        # anchors and a usable volatility estimate.
        await self.backfill()
        self._task = asyncio.create_task(self._run(), name="spot-feed")

    async def backfill(self) -> dict[str, int]:
        """Seed each asset's buffer with recent 1-minute history.

        Window starts are aligned to 300s/900s, which are exact minute
        boundaries, so a 1-minute candle opening at that instant is a faithful
        anchor rather than an interpolation.
        """
        assert self._client is not None
        results = await asyncio.gather(
            *(self._backfill_asset(a) for a in self.assets), return_exceptions=True
        )
        return {
            asset: (count if isinstance(count, int) else 0)
            for asset, count in zip(self.assets, results)
        }

    async def _backfill_asset(self, asset: str) -> int:
        spec = ASSET_SPEC[asset]
        points: list[tuple[float, float]] = []
        if spec.get("coinbase"):
            points = await self._coinbase_candles(spec["coinbase"])
        if not points and spec.get("kraken_req"):
            points = await self._kraken_ohlc(spec["kraken_req"])
        if points:
            self.state[asset].seed(points)
        return len(points)

    async def _coinbase_candles(self, product: str) -> list[tuple[float, float]]:
        assert self._client is not None
        try:
            resp = await self._client.get(
                COINBASE_CANDLES.format(product=product), params={"granularity": 60}
            )
            if resp.status_code != 200:
                return []
            cutoff = time.time() - BACKFILL_SECONDS
            # Coinbase rows are [time, low, high, open, close, volume].
            return [
                (float(row[0]), float(row[3]))
                for row in resp.json()
                if isinstance(row, list) and len(row) >= 5 and float(row[0]) >= cutoff
            ]
        except Exception:
            return []

    async def _kraken_ohlc(self, pair: str) -> list[tuple[float, float]]:
        assert self._client is not None
        try:
            resp = await self._client.get(
                KRAKEN_OHLC,
                params={"pair": pair, "interval": 1, "since": int(time.time() - BACKFILL_SECONDS)},
            )
            result = (resp.json() or {}).get("result") or {}
            rows = next((v for k, v in result.items() if k != "last"), [])
            # Kraken rows are [time, open, high, low, close, vwap, volume, count].
            return [
                (float(row[0]), float(row[1]))
                for row in rows
                if isinstance(row, list) and len(row) >= 5
            ]
        except Exception:
            return []

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                await asyncio.gather(
                    self._poll_coinbase(),
                    self._poll_kraken(),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A feed outage must not kill the loop; health flags carry it.
                pass
            elapsed = time.time() - started
            await asyncio.sleep(max(0.2, self.interval - elapsed))

    async def _poll_coinbase(self) -> None:
        assert self._client is not None
        products = [
            (a, ASSET_SPEC[a]["coinbase"]) for a in self.assets if ASSET_SPEC[a].get("coinbase")
        ]
        if not products:
            return
        started = time.time()

        async def one(asset: str, product: str) -> bool:
            try:
                resp = await self._client.get(COINBASE_TICKER.format(product=product))
                if resp.status_code != 200:
                    return False
                data = resp.json()
                price = float(data.get("price") or 0)
                if price > 0:
                    self.state[asset].push(price)
                    self.state[asset].sources.add("coinbase")
                    return True
            except Exception:
                return False
            return False

        results = await asyncio.gather(
            *(one(a, p) for a, p in products), return_exceptions=True
        )
        received = sum(result is True for result in results)
        ok = received > 0
        self.source_health["coinbase"].update(
            active=ok,
            latency=round(time.time() - started, 3),
            error=None if ok else "no valid prices received",
            polled=len(products),
            received=received,
        )

    async def _poll_kraken(self) -> None:
        assert self._client is not None
        wanted = [(a, ASSET_SPEC[a]) for a in self.assets if ASSET_SPEC[a].get("kraken_req")]
        if not wanted:
            return
        pairs = ",".join(spec["kraken_req"] for _, spec in wanted)
        started = time.time()
        try:
            resp = await self._client.get(KRAKEN_TICKER, params={"pair": pairs})
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            payload = resp.json()
            errors = payload.get("error") or []
            if errors:
                raise RuntimeError(str(errors[0]))
            result = payload.get("result") or {}
            received = 0
            for asset, spec in wanted:
                # Kraken answers under either the canonical or the requested
                # pair name depending on the asset, so try both.
                entry = result.get(spec["kraken"]) or result.get(spec["kraken_req"])
                if not entry:
                    continue
                last = entry.get("c") or []
                if last:
                    price = float(last[0])
                    if price > 0:
                        self.state[asset].push(price)
                        self.state[asset].sources.add("kraken")
                        received += 1
            ok = received > 0
            self.source_health["kraken"].update(
                active=ok,
                latency=round(time.time() - started, 3),
                error=None if ok else "no valid prices received",
                polled=len(wanted),
                received=received,
            )
        except Exception as exc:
            self.source_health["kraken"].update(
                active=False,
                latency=round(time.time() - started, 3),
                error=str(exc)[:120],
            )

    def get(self, asset: str) -> AssetState | None:
        return self.state.get(asset)

    def price(self, asset: str) -> float | None:
        st = self.state.get(asset)
        return st.last_price if st else None

    def snapshot(self) -> list[dict[str, Any]]:
        return [self.state[a].snapshot() for a in self.assets]
