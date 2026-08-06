"""The trading engine.

One asyncio loop drives everything:

1. Refresh spot prices and recompute the volatility regime.
2. Discover the windows currently in progress (cheap — slugs are computed).
3. Refresh order books for those windows.
4. Score every window, and enter where the model and the risk layer agree.
5. Settle windows that have closed.

Settlement deserves a note. These markets resolve against a Chainlink data
stream, and that resolution is published minutes after the window closes. We
settle immediately against our own spot reference instead, so the dashboard
stays responsive — the same anchor price that produced the signal decides the
outcome, which makes the paper record internally consistent. It is an
approximation of the venue's own resolution, not a reproduction of it, and the
two can disagree on a near-flat close.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .broker import LiveBroker, LiveTradingDisabled, PaperBroker
from .feeds import PolymarketFeed, SpotFeed
from .feeds.chainlink import ChainlinkStreams
from .feeds.polymarket import MarketWindow
from .maker import MakerStrategy, Quote
from .risk import RiskManager
from .settings import ASSET_SPEC, Runtime, live_trading_enabled
from .signal import SignalModel
from .store import Store

# Wait this long after a window closes before settling, so a spot tick landing
# just after the boundary is included rather than raced.
SETTLE_GRACE_SECONDS = 3.0
SIGNAL_RETENTION_SECONDS = 24 * 60 * 60


@dataclass
class EngineStats:
    signal_scans: int = 0
    entries: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    last_loop_ms: float = 0.0
    loops: int = 0
    live_fill_misses: int = 0

    def reject(self, reason: str) -> None:
        # Bucket by the leading clause so per-value noise ("only 12s left")
        # does not shatter the histogram into hundreds of singletons.
        key = reason.split("(")[0].strip()[:48]
        self.rejections[key] = self.rejections.get(key, 0) + 1


class Engine:
    def __init__(self, config: dict[str, Any], store: Store) -> None:
        self.config = config
        self.store = store
        self.runtime = Runtime.from_config(config)

        assets = [a for a in config["assets"] if a in ASSET_SPEC]
        self.assets = assets
        self.windows = config["windows"]

        self.spot = SpotFeed(assets, interval=float(config["engine"]["spot_interval_seconds"]))
        self.poly = PolymarketFeed(assets, self.windows)
        # Optional. When credentials are present this becomes the anchor and
        # settlement source, because it is the same stream the venue resolves
        # on — removing the exchange-vs-Chainlink basis entirely.
        self.chainlink = ChainlinkStreams(assets)
        self.model = SignalModel(config)
        self.risk = RiskManager(config)
        self.paper = PaperBroker(config)
        self.live = LiveBroker(config)

        self.strategy = config.get("strategy", "maker")
        self.max_entries_per_window = int(config["entry"]["max_entries_per_window"])
        self.maker = MakerStrategy(config)
        self.maker_stats = {
            "quotes_placed": 0,
            "fills": 0,
            "locked_windows": 0,
            "requotes": 0,
            "rebates_usd": 0.0,
        }
        self.stats = EngineStats()
        self.latest_signals: dict[str, dict[str, Any]] = {}
        # Explicit operator override for the current UTC day. This is kept
        # paper-only and in memory so it can never weaken Live or survive into
        # a later day/restart by accident.
        self.paper_daily_loss_bypass_until: float = 0.0
        # Populated by the final Live-order preflight.  Keeping the last
        # wallet snapshot visible in /api/state makes it possible to verify
        # that the configured bankroll is being reconciled with real funds.
        self._last_wallet_risk: dict[str, Any] = {}

        self._task: asyncio.Task | None = None
        self._signal_expiry_task: asyncio.Task | None = None
        self._signals_changed = asyncio.Event()
        self._stop = asyncio.Event()
        self._last_discovery = 0.0
        # Anchor price per window slug, captured once so every position in the
        # same window is judged against an identical opening price.
        self._anchors: dict[str, float] = {}
        # A one-tick signal change is noise, not a side switch. Candidates must
        # persist before an opposite-side order becomes eligible.
        self._reversal_candidates: dict[str, dict[str, Any]] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self, mode: str | None = None) -> dict[str, Any]:
        if self._task and not self._task.done():
            return {"ok": True, "already_running": True}

        if mode:
            if mode == "live" and not live_trading_enabled():
                return {
                    "ok": False,
                    "error": "Live trading is disabled. Set LIVE_TRADING_ENABLED=1 in .env and restart.",
                }
            self.runtime.update(mode=mode)

        self._stop.clear()
        await self.spot.start()
        await self.poly.start()

        # Never fatal: without a key we simply keep using exchange spot, and
        # the dashboard shows which source is authoritative.
        try:
            if await self.chainlink.start():
                self.store.log(
                    self.runtime.mode if mode is None else mode, "engine",
                    f"Chainlink Data Streams connected — {len(self.chainlink.feeds)} feeds. "
                    "Anchors and settlement now use the venue's own resolution source.",
                )
        except Exception as exc:
            self.runtime.update(last_error=f"chainlink: {exc}"[:200])

        self.runtime.update(running=True, paused=False, started_at=time.time(), last_error=None)
        if self._signal_expiry_task is None or self._signal_expiry_task.done():
            self._signal_expiry_task = asyncio.create_task(
                self._expire_signals(), name="signal-expiry"
            )
        self._task = asyncio.create_task(self._run(), name="engine")
        self.store.log(self.runtime.mode, "engine", f"Engine started in {self.runtime.mode} mode")
        return {"ok": True, "mode": self.runtime.mode}

    async def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._signal_expiry_task:
            self._signal_expiry_task.cancel()
            try:
                await self._signal_expiry_task
            except (asyncio.CancelledError, Exception):
                pass
            self._signal_expiry_task = None
        await self.spot.stop()
        await self.poly.stop()
        await self.chainlink.stop()
        self.runtime.update(running=False)
        self.store.log(self.runtime.mode, "engine", "Engine stopped")
        return {"ok": True}

    def pause(self) -> dict[str, Any]:
        self.runtime.update(paused=True)
        self.store.log(self.runtime.mode, "engine", "Trading paused — open windows keep settling")
        return {"ok": True, "paused": True}

    def resume(self) -> dict[str, Any]:
        self.runtime.update(paused=False)
        self.store.log(self.runtime.mode, "engine", "Trading resumed")
        return {"ok": True, "paused": False}

    async def panic(self) -> dict[str, Any]:
        """Sell every open position into resting bids, then pause.

        Positions with no bid to hit are left open and settle normally — there
        is nothing better to do with them, and pretending otherwise would book
        a fictional exit price.
        """
        self.runtime.update(paused=True)
        mode = self.runtime.mode
        broker = self.paper if mode == "paper" else self.live

        closed = 0
        stranded = 0
        for position in self.store.open_positions(mode):
            market = self.poly.get(position["slug"])
            if market is None:
                stranded += 1
                continue
            try:
                fill = broker.sell(market, position["side"], float(position["shares"]))
            except Exception as exc:
                self.runtime.update(last_error=str(exc)[:200])
                stranded += 1
                continue
            if fill is None:
                stranded += 1
                continue

            stake = float(position["stake_usd"])
            self.store.record_settlement(
                position_id=position["id"],
                mode=mode,
                slug=position["slug"],
                asset=position["asset"],
                window=position["window"],
                side=position["side"],
                confidence=position["confidence"],
                entry_price=position["entry_price"],
                shares=fill.shares,
                stake_usd=stake,
                payout_usd=round(fill.stake_usd, 4),
                pnl_usd=round(fill.stake_usd - stake, 4),
                won=1 if fill.stake_usd > stake else 0,
                anchor_price=position["anchor_price"],
                close_price=None,
                settled_at=time.time(),
                method="panic",
            )
            self.store.close_position(position["id"], "closed")
            closed += 1

        self.store.log(
            mode, "panic",
            f"Panic: sold {closed} position(s), {stranded} left to settle normally. Trading paused.",
        )
        return {"ok": True, "closed": closed, "stranded": stranded}

    # -- main loop ---------------------------------------------------------

    async def _run(self) -> None:
        interval = float(self.config["engine"]["poll_interval_seconds"])
        discovery_every = float(self.config["engine"]["discovery_interval_seconds"])

        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                now = time.time()
                self.prune_signals(now)
                self._prune_anchors(now)

                if now - self._last_discovery >= discovery_every or not self.poly.markets:
                    await self.poly.discover()
                    self._last_discovery = now

                live_markets = self.poly.live_markets(now)
                if live_markets:
                    await self.poly.refresh_books(live_markets)

                # When Data Streams is live its prices become the buffer's
                # source of truth, so anchors, volatility and settlement all
                # derive from the exact series the venue resolves on.
                if self.chainlink.health.get("active"):
                    for asset, sp in self.chainlink.latest.items():
                        state = self.spot.get(asset)
                        if state is not None:
                            state.push(sp.price, sp.received_at)
                            state.sources.add("chainlink")

                entry = self.config["entry"]
                entry_override = bool(
                    entry.get("confidence_only", False)
                    or entry.get("profit_tuned", False)
                )
                if not entry_override:
                    self.risk.update_regimes(self.spot.snapshot())
                    self.risk.update_hourly_loss(
                        self.store.pnl_since(
                            self.runtime.mode, now - self.risk.dd_lookback_seconds
                        ),
                        now,
                    )

                self._capture_anchors(live_markets, now)

                # Maker fills must be processed before settlement so a fill
                # landing in the final seconds is still counted in the window
                # it belongs to.
                if self.strategy in ("maker", "both") and not self.runtime.paused:
                    self._run_maker(live_markets, now)

                await self._settle_closed(now)
                self._settle_maker(now)

                if self.strategy in ("directional", "both") and not self.runtime.paused:
                    await self._scan(live_markets, now)

                self.runtime.update(last_error=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.runtime.update(last_error=f"{type(exc).__name__}: {exc}"[:240])

            self.stats.last_loop_ms = (time.perf_counter() - started) * 1000.0
            self.stats.loops += 1
            await asyncio.sleep(max(0.5, interval - (time.perf_counter() - started)))

    def _capture_anchors(self, markets: list[MarketWindow], now: float) -> None:
        """Latch each window's opening price the first time we can read it."""
        for market in markets:
            if market.slug in self._anchors:
                continue
            state = self.spot.get(market.asset)
            if state is None:
                continue
            anchor = state.price_at_or_before(market.start)
            if anchor:
                self._anchors[market.slug] = anchor

    def _remember_signal(self, signal: Any, now: float) -> None:
        """Keep the most recent score for a window for no longer than 24 hours."""
        row = signal.to_dict()
        row["observed_at"] = now
        self.latest_signals[signal.slug] = row
        self._signals_changed.set()

    def prune_signals(self, now: float | None = None) -> int:
        """Delete signals the instant their age reaches the retention limit."""
        now = now if now is not None else time.time()
        cutoff = now - SIGNAL_RETENTION_SECONDS
        stale = [
            slug for slug, signal in self.latest_signals.items()
            if float(signal.get("observed_at", 0.0)) <= cutoff
        ]
        for slug in stale:
            self.latest_signals.pop(slug, None)
        return len(stale)

    async def _expire_signals(self) -> None:
        """Sleep until the next exact expiry rather than polling or leaking tasks."""
        while True:
            self.prune_signals()
            if not self.latest_signals:
                self._signals_changed.clear()
                await self._signals_changed.wait()
                continue

            next_expiry = min(
                float(signal.get("observed_at", 0.0)) + SIGNAL_RETENTION_SECONDS
                for signal in self.latest_signals.values()
            )
            delay = max(0.0, next_expiry - time.time())
            self._signals_changed.clear()
            try:
                await asyncio.wait_for(self._signals_changed.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _prune_anchors(self, now: float) -> None:
        """Bound window bookkeeping while retaining far more than settlement needs."""
        cutoff = now - SIGNAL_RETENTION_SECONDS
        stale = []
        for slug in self._anchors:
            try:
                started_at = float(slug.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                continue
            if started_at <= cutoff:
                stale.append(slug)
        for slug in stale:
            self._anchors.pop(slug, None)
            self._reversal_candidates.pop(slug, None)

    # -- maker -------------------------------------------------------------

    def _run_maker(self, markets: list[MarketWindow], now: float) -> None:
        """One pass of the two-sided quoting loop.

        Order matters: check existing quotes for fills first, then flatten any
        residual exposure, then place fresh quotes. Flattening takes priority
        over new pairs because unpaired inventory is the only real risk the
        strategy carries.
        """
        mode = self.runtime.mode
        runtime = self.runtime.snapshot()
        day_exposure = self.store.exposure_since(mode, _start_of_day(now))
        selection = self.live_market_selection()

        max_drift = float(self.config["maker"]["max_quote_drift_cents"])

        for market in markets:
            if (
                not self.configured_market_allowed(market.asset, market.window)
                or not self.live_market_allowed(market.asset, market.window, selection)
            ):
                self.maker.quotes.pop((market.slug, "up"), None)
                self.maker.quotes.pop((market.slug, "down"), None)
                continue

            # -- cancel quotes that mid has drifted away from, before checking
            # fills. A stale quote is a free option written to the market.
            for side in self.maker.stale_quotes(market, max_drift):
                self.maker.quotes.pop((market.slug, side), None)
                self.maker_stats["requotes"] += 1

            # -- fills on resting quotes
            for side in ("up", "down"):
                key = (market.slug, side)
                quote = self.maker.quotes.get(key)
                if quote is None:
                    continue
                fill = self.maker.simulate_fill(quote, market)
                if fill is None:
                    continue

                self.maker.quotes.pop(key, None)
                # Resting fills earn the maker fee-share rebate, which offsets
                # a little of the cost. Credited against cost basis so it shows
                # up in P/L rather than as a separate flattering line item.
                rebate = self.maker.maker_rebate(fill["price"], fill["shares"], market)
                inv = self.maker.record_fill(
                    market, side, fill["shares"], fill["cost"] - rebate
                )
                self.maker_stats["fills"] += 1
                self.maker_stats["rebates_usd"] += rebate
                day_exposure += fill["cost"]

                self.store.log(
                    mode, "fill",
                    f"{market.asset.upper()} {market.window} {side.upper()} filled "
                    f"{fill['shares']:.1f} @ {fill['price'] * 100:.0f}¢ · ${fill['cost']:.2f}",
                    asset=market.asset, slug=market.slug,
                    detail=(f"pair cost {inv.pair_cost * 100:.0f}¢ · "
                            f"locked {inv.locked_shares:.1f} shares "
                            f"(${inv.locked_profit:.2f})") if inv.locked_shares else "unpaired leg",
                )

            if self.runtime.paused:
                continue

            # -- risk gates apply to the whole strategy, not each leg
            if self.risk.breaker_active or self.risk.asset_halted(market.asset):
                continue
            if day_exposure >= runtime["daily_cap_usd"]:
                self.stats.reject("Daily cap reached")
                continue

            inv = self.maker.inventory.get(market.slug)
            window_cost = inv.total_cost if inv else 0.0
            market_window_cap = float(
                self.config["entry"].get(
                    "hard_market_window_cap_usd",
                    runtime["max_per_window_usd"],
                )
            )
            market_window_remaining = max(0.0, market_window_cap - window_cost)
            if market_window_remaining < self.risk.min_trade_usd:
                self.stats.reject("Per-market/window cap reached")
                continue

            # -- flatten residual exposure first (active side switching)
            residual = self.maker.residual_quote(market, now)
            if residual is not None:
                residual.size_usd = min(
                    residual.size_usd,
                    runtime["max_per_trade_usd"],
                    market_window_remaining,
                )
                bankroll_size, bankroll_reason = self._apply_bankroll_cap(
                    mode, residual.size_usd, now
                )
                if bankroll_size is None:
                    self.stats.reject(bankroll_reason or "bankroll cap")
                    continue
                residual.size_usd = bankroll_size
                self.maker.quotes[(market.slug, residual.side)] = residual
                self.maker_stats["quotes_placed"] += 1
                continue

            # -- otherwise quote both sides for a fresh pair
            if any((market.slug, s) in self.maker.quotes for s in ("up", "down")):
                continue

            state = self.spot.get(market.asset)
            p_up = None
            if state is not None:
                signal = self.model.evaluate(market, state, now)
                if signal is not None:
                    p_up = signal.raw_probability
                    self._remember_signal(signal, now)
            self.stats.signal_scans += 1

            desired = self.maker.desired_quotes(market, p_up, now)
            for quote in desired:
                quote.size_usd = min(quote.size_usd, runtime["max_per_trade_usd"])
            requested_pair = sum(quote.size_usd for quote in desired)
            if requested_pair:
                if requested_pair > market_window_remaining + 1e-6:
                    self.stats.reject("Per-market/window room below pair size")
                    continue
                bankroll_size, bankroll_reason = self._apply_bankroll_cap(
                    mode, requested_pair, now
                )
                # Both legs must fit. Quoting only one side would convert a
                # bankroll resize into an unintended directional position.
                if bankroll_size is None or bankroll_size + 1e-6 < requested_pair:
                    self.stats.reject(bankroll_reason or "bankroll room below pair size")
                    continue
            for quote in desired:
                self.maker.quotes[(market.slug, quote.side)] = quote
                self.maker_stats["quotes_placed"] += 1

    def _settle_maker(self, now: float) -> None:
        """Resolve maker inventory for windows that have closed."""
        mode = self.runtime.mode
        for slug in [s for s, inv in self.maker.inventory.items()
                     if now >= inv.end + SETTLE_GRACE_SECONDS]:
            inv = self.maker.inventory[slug]
            state = self.spot.get(inv.asset)
            anchor = self._anchors.get(slug)
            close = state.price_at_or_before(inv.end) if state else None

            if close is None or not anchor:
                if now > inv.end + 300:
                    self.maker.drop_window(slug)
                    self.store.log(
                        mode, "warn",
                        f"{inv.asset.upper()} {inv.window} maker inventory dropped — "
                        "no spot price covering the window close.",
                        asset=inv.asset, slug=slug,
                    )
                continue

            up_won = close >= anchor
            result = inv.settle(up_won)
            side_label = "up" if up_won else "down"

            self.store.record_settlement(
                position_id=None,
                mode=mode,
                slug=slug,
                asset=inv.asset,
                window=inv.window,
                side=side_label,
                confidence=None,
                entry_price=round(inv.total_cost / max(inv.shares_up + inv.shares_down, 1e-9), 4),
                shares=round(inv.shares_up + inv.shares_down, 4),
                stake_usd=result["cost_usd"],
                payout_usd=result["payout_usd"],
                pnl_usd=result["pnl_usd"],
                won=1 if result["pnl_usd"] >= 0 else 0,
                anchor_price=anchor,
                close_price=close,
                settled_at=now,
                method="maker",
            )
            if inv.locked_shares > 0:
                self.maker_stats["locked_windows"] += 1

            self.store.log(
                mode, "settlement",
                f"{inv.asset.upper()} {inv.window} maker {result['pnl_usd']:+.2f} "
                f"({inv.locked_shares:.1f} locked pairs @ {inv.pair_cost * 100:.0f}¢)",
                asset=inv.asset, slug=slug,
                detail=(f"anchor {anchor:.6g} → close {close:.6g} · {side_label.upper()} won · "
                        f"residual {result['residual_shares']:.1f} "
                        f"{result['residual_side'] or 'none'}"),
            )
            self.maker.drop_window(slug)

    # -- entries -----------------------------------------------------------

    def configured_market_allowed(self, asset: str, window: str) -> bool:
        """Return false for a disabled or currently unqualified cohort."""
        return self.configured_market_rejection(asset, window) is None

    def configured_market_rejection(self, asset: str, window: str) -> str | None:
        """Explain the static or rolling-evidence gate blocking a cohort.

        This gate is deliberately separate from the paper-tested live
        whitelist. Entry override modes may bypass that whitelist, but they
        must never bypass an explicit market exclusion.
        """
        asset = str(asset).lower()
        window = str(window).lower()
        excluded = any(
            isinstance(row, dict)
            and str(row.get("asset", "")).lower() == asset
            and str(row.get("window", "")).lower() == window
            for row in self.config.get("excluded_markets", [])
        )
        if excluded:
            return "market disabled by operator"
        if self.config.get("entry", {}).get(
            "require_evidence_qualified_market", False
        ):
            evidence = self.store.cohort_evidence("paper", asset, window)
            if (
                not evidence["evidence_qualified"]
                and not self._paper_experimental_market_allowed(asset, window)
            ):
                return "market failed rolling paper evidence gate"
        return None

    def _paper_experimental_market_allowed(self, asset: str, window: str) -> bool:
        """Allow an exact unqualified cohort only as a configured Paper test."""
        if getattr(getattr(self, "runtime", None), "mode", None) != "paper":
            return False
        asset = str(asset).lower()
        window = str(window).lower()
        entry = self.config.get("entry", {})
        minimum_floor = max(
            0.50, float(entry.get("paper_experimental_min_confidence", 0.90))
        )
        asset_floor = float(
            entry.get("asset_min_confidence", {}).get(asset, 0.0)
        )
        return any(
            isinstance(row, dict)
            and str(row.get("asset", "")).lower() == asset
            and str(row.get("window", "")).lower() == window
            and max(asset_floor, float(row.get("min_confidence", 0.0)))
            >= minimum_floor
            for row in entry.get("paper_experimental_markets", [])
        )

    def live_market_selection(self) -> dict[str, Any]:
        """Rank live-eligible markets using settled paper results only."""
        cfg = self.config.get("live_market_selection", {})
        enabled = bool(cfg.get("enabled", True))
        min_settlements = max(1, int(cfg.get("min_paper_settlements", 100)))
        min_roi = float(cfg.get("min_paper_roi", 0.10))
        max_markets = max(1, int(cfg.get("max_markets", 5)))
        configured = {
            (asset, window)
            for asset in self.assets
            for window in self.windows
            if self.configured_market_allowed(asset, window)
        }
        ranked = sorted(
            (
                dict(row)
                for row in self.store.by_asset("paper")
                if (row.get("asset"), row.get("window")) in configured
            ),
            key=lambda row: (float(row.get("roi") or 0.0), float(row.get("pnl") or 0.0)),
            reverse=True,
        )
        qualifying = [
            row for row in ranked
            if int(row.get("settlements") or 0) >= min_settlements
            and float(row.get("roi") or 0.0) >= min_roi
        ]
        return {
            "enabled": enabled,
            "min_paper_settlements": min_settlements,
            "min_paper_roi": min_roi,
            "max_markets": max_markets,
            "eligible": qualifying[:max_markets],
            "ranked": ranked,
        }

    def live_market_allowed(
        self,
        asset: str,
        window: str,
        selection: dict[str, Any] | None = None,
    ) -> bool:
        """Paper is unrestricted; live is limited to the paper-tested leaders."""
        if self.runtime.mode != "live":
            return True
        selection = selection or self.live_market_selection()
        if not selection["enabled"]:
            return True
        return any(
            row["asset"] == asset and row["window"] == window
            for row in selection["eligible"]
        )

    def normal_entry_cap(self) -> int:
        """Leave one measured entry slot available for an opposite-side hedge."""
        entry = self.config["entry"]
        reserve = int(
            bool(entry.get("reversal_enabled", False))
            and int(entry.get("max_side_switches", 0)) > 0
        )
        return max(1, self.max_entries_per_window - reserve)

    def _apply_hard_window_cap(
        self,
        mode: str,
        window: str,
        window_end: float,
        requested_usd: float,
    ) -> tuple[float | None, str | None]:
        """Cap one shared timed interval across every configured asset.

        This is intentionally separate from ``RiskManager`` because entry
        override modes bypass that layer. Every enabled asset ending in the
        same timed window consumes the same allowance.
        """
        cap = float(self.config["entry"].get("hard_window_cap_usd", 0.0))
        if cap <= 0:
            return float(requested_usd), None

        spent = self.store.interval_exposure(mode, window, window_end)
        remaining = max(0.0, cap - spent)
        size = min(float(requested_usd), remaining)
        if size < self.risk.min_trade_usd:
            return None, (
                f"hard shared-window cap reached "
                f"(${spent:,.2f}/${cap:,.2f})"
            )
        return size, None

    def _apply_market_window_cap(
        self,
        mode: str,
        slug: str,
        requested_usd: float,
    ) -> tuple[float | None, str | None]:
        """Cap exposure for one exact market/window before shared limits.

        BTC 5m and ETH 5m ending together each consume their own market/window
        allowance, while ``_apply_hard_window_cap`` still caps their combined
        exposure for that timed interval.
        """
        entry = self.config["entry"]
        cap = float(
            entry.get(
                "hard_market_window_cap_usd",
                entry.get("hard_window_cap_usd", 0.0),
            )
        )
        if cap <= 0:
            return float(requested_usd), None

        spent = self.store.window_exposure(mode, slug)
        remaining = max(0.0, cap - spent)
        size = min(float(requested_usd), remaining)
        if size < self.risk.min_trade_usd:
            return None, (
                f"hard market-window cap reached "
                f"(${spent:,.2f}/${cap:,.2f})"
            )
        return size, None

    def _actual_open_exposure(self, mode: str) -> float:
        exposure = self.store.open_exposure(mode)
        maker = getattr(self, "maker", None)
        if maker is not None:
            exposure += sum(
                float(inventory.total_cost)
                for inventory in maker.inventory.values()
            )
        return exposure

    def _pending_quote_exposure(self) -> float:
        maker = getattr(self, "maker", None)
        if maker is None:
            return 0.0
        return sum(float(quote.size_usd) for quote in maker.quotes.values())

    def bankroll_status(self, now: float | None = None) -> dict[str, Any]:
        """Current use of the operator-declared bankroll for this mode."""
        now = time.time() if now is None else now
        mode = self.runtime.mode
        cfg = self.config.get("bankroll", {})
        day_start = max(
            _start_of_day(now), float(cfg.get("activated_at", 0.0))
        )
        day_pnl = float(self.store.stats(mode, day_start)["total_pnl"])
        starting_balance = float(cfg.get("starting_balance_usd", 0.0))
        max_open = float(cfg.get("max_open_exposure_usd", 0.0))
        open_exposure = self._actual_open_exposure(mode)
        pending_quotes = self._pending_quote_exposure()
        status: dict[str, Any] = {
            "starting_balance_usd": starting_balance,
            "cash_reserve_usd": round(
                max(
                    0.0,
                    float(
                        cfg.get(
                            "cash_reserve_usd",
                            starting_balance - max_open,
                        )
                    ),
                ),
                4,
            ),
            "max_open_exposure_usd": max_open,
            "open_exposure_usd": round(open_exposure, 4),
            "pending_quote_exposure_usd": round(pending_quotes, 4),
            "committed_exposure_usd": round(open_exposure + pending_quotes, 4),
            "max_daily_loss_usd": float(cfg.get("max_daily_loss_usd", 0.0)),
            "daily_profit_lock_usd": float(cfg.get("daily_profit_lock_usd", 0.0)),
            "daily_pnl_usd": round(day_pnl, 4),
            "paper_daily_loss_bypass_active": self._paper_daily_loss_bypass_active(
                mode, now
            ),
            "paper_daily_loss_bypass_until": self.paper_daily_loss_bypass_until,
            "max_daily_turnover_usd": float(
                cfg.get("max_daily_turnover_usd", 0.0)
            ),
            "daily_turnover_usd": round(
                self.store.exposure_since(mode, day_start), 4
            ),
        }
        status.update(getattr(self, "_last_wallet_risk", {}))
        return status

    def _apply_bankroll_cap(
        self,
        mode: str,
        requested_usd: float,
        now: float,
    ) -> tuple[float | None, str | None]:
        """Apply capital, turnover and loss limits that overrides cannot skip."""
        cfg = self.config.get("bankroll", {})
        if not cfg:
            return float(requested_usd), None

        day_start = max(
            _start_of_day(now), float(cfg.get("activated_at", 0.0))
        )
        bankroll = float(cfg.get("starting_balance_usd", 0.0))
        bankroll_label = f"${bankroll:,.0f} bankroll" if bankroll > 0 else "bankroll"
        max_daily_loss = float(cfg.get("max_daily_loss_usd", 0.0))
        daily_pnl = float(self.store.stats(mode, day_start)["total_pnl"])
        if (
            max_daily_loss > 0
            and daily_pnl <= -max_daily_loss
            and not self._paper_daily_loss_bypass_active(mode, now)
        ):
            return None, (
                f"{bankroll_label} daily loss stop reached "
                f"(${daily_pnl:,.2f}/-${max_daily_loss:,.2f})"
            )

        daily_profit_lock = float(cfg.get("daily_profit_lock_usd", 0.0))
        if daily_profit_lock > 0 and daily_pnl >= daily_profit_lock:
            return None, (
                f"Daily profit lock reached "
                f"(${daily_pnl:,.2f}/${daily_profit_lock:,.2f})"
            )

        max_open = float(cfg.get("max_open_exposure_usd", 0.0))
        open_exposure = (
            self._actual_open_exposure(mode) + self._pending_quote_exposure()
        )
        open_remaining = (
            max(0.0, max_open - open_exposure)
            if max_open > 0
            else float(requested_usd)
        )

        max_turnover = float(cfg.get("max_daily_turnover_usd", 0.0))
        day_turnover = self.store.exposure_since(mode, day_start)
        turnover_remaining = (
            max(0.0, max_turnover - day_turnover)
            if max_turnover > 0
            else float(requested_usd)
        )

        size = min(float(requested_usd), open_remaining, turnover_remaining)
        if size < self.risk.min_trade_usd:
            if max_open > 0 and open_remaining < self.risk.min_trade_usd:
                return None, (
                    f"{bankroll_label} open-exposure cap reached "
                    f"(${open_exposure:,.2f}/${max_open:,.2f})"
                )
            if max_turnover > 0 and turnover_remaining < self.risk.min_trade_usd:
                return None, (
                    f"{bankroll_label} daily turnover reached "
                    f"(${day_turnover:,.2f}/${max_turnover:,.2f})"
                )
            return None, "bankroll room below venue minimum"
        return size, None

    def _apply_live_wallet_cap(
        self,
        requested_usd: float,
    ) -> tuple[float | None, str | None, bool]:
        """Reconcile configured risk limits with the real Live wallet.

        The declared bankroll is an operator limit, not proof that those funds
        still exist.  Before a Live order, require a fresh collateral and
        position-value snapshot, preserve the configured cash reserve against
        actual account equity, and include *all* wallet positions (including
        positions not represented in this process's database) in the exposure
        calculation.

        The boolean return value marks failures that should pause the engine:
        an unreadable wallet or account equity below the required reserve is
        not a temporary per-window rejection.
        """
        cfg = self.config.get("bankroll", {})
        if not cfg:
            return float(requested_usd), None, False

        try:
            snapshot = self.live.balance()
        except Exception as exc:
            reason = f"Live wallet risk check failed: {exc}"[:240]
            self._last_wallet_risk = {
                "wallet_risk_ok": False,
                "wallet_risk_error": reason,
                "wallet_checked_at": time.time(),
            }
            return None, reason, True

        if not snapshot.get("ok"):
            reason = (
                f"Live wallet risk check failed: "
                f"{snapshot.get('error') or 'balance unavailable'}"
            )[:240]
            self._last_wallet_risk = {
                "wallet_risk_ok": False,
                "wallet_risk_error": reason,
                "wallet_checked_at": time.time(),
            }
            return None, reason, True

        try:
            cash = float(snapshot["balance_pusd"])
            positions_value_raw = snapshot.get("positions_value_pusd")
            equity_raw = snapshot.get("account_equity_pusd")
            if positions_value_raw is None or equity_raw is None:
                raise ValueError(
                    snapshot.get("positions_error")
                    or "position value unavailable"
                )
            positions_value = max(0.0, float(positions_value_raw))
            equity = max(0.0, float(equity_raw))
        except (KeyError, TypeError, ValueError) as exc:
            reason = f"Live wallet risk check incomplete: {exc}"[:240]
            self._last_wallet_risk = {
                "wallet_risk_ok": False,
                "wallet_risk_error": reason,
                "wallet_checked_at": time.time(),
            }
            return None, reason, True

        max_open = max(0.0, float(cfg.get("max_open_exposure_usd", 0.0)))
        reserve = max(
            0.0,
            float(
                cfg.get(
                    "cash_reserve_usd",
                    max(0.0, float(cfg.get("starting_balance_usd", 0.0)) - max_open),
                )
            ),
        )
        equity_risk_budget = max(0.0, equity - reserve)
        wallet_risk_budget = (
            min(max_open, equity_risk_budget)
            if max_open > 0
            else equity_risk_budget
        )

        tracked_exposure = (
            self._actual_open_exposure("live") + self._pending_quote_exposure()
        )
        # The public wallet value can contain positions opened elsewhere; the
        # local cost basis can be higher than mark value.  Taking the maximum
        # is conservative without double-counting known bot positions.
        exposure_basis = max(tracked_exposure, positions_value)
        room = min(
            max(0.0, cash),
            max(0.0, wallet_risk_budget - exposure_basis),
        )
        size = min(float(requested_usd), room)

        self._last_wallet_risk = {
            "wallet_risk_ok": equity >= reserve,
            "wallet_balance_pusd": round(cash, 6),
            "wallet_positions_value_pusd": round(positions_value, 6),
            "wallet_equity_pusd": round(equity, 6),
            "wallet_cash_reserve_usd": round(reserve, 4),
            "wallet_risk_budget_usd": round(wallet_risk_budget, 4),
            "wallet_exposure_basis_usd": round(exposure_basis, 4),
            "wallet_new_order_room_usd": round(room, 4),
            "wallet_checked_at": time.time(),
            "wallet_risk_error": None,
        }

        if equity + 1e-6 < reserve:
            reason = (
                f"Live wallet reserve breached: ${equity:,.2f} equity is below "
                f"the ${reserve:,.2f} cash reserve"
            )
            self._last_wallet_risk["wallet_risk_error"] = reason
            return None, reason, True

        if size < self.risk.min_trade_usd:
            return None, (
                f"Live wallet exposure cap reached "
                f"(${exposure_basis:,.2f}/${wallet_risk_budget:,.2f}; "
                f"${cash:,.2f} spendable cash)"
            ), False
        return size, None, False

    def _paper_daily_loss_bypass_active(self, mode: str, now: float) -> bool:
        return bool(
            mode == "paper"
            and now < getattr(self, "paper_daily_loss_bypass_until", 0.0)
        )

    def bypass_paper_daily_loss_today(self, now: float | None = None) -> dict[str, Any]:
        """Ignore only Paper's daily-loss stop until the next UTC boundary."""
        now = time.time() if now is None else now
        until = _start_of_day(now) + 86400.0
        self.paper_daily_loss_bypass_until = until
        self.store.log(
            "paper",
            "risk",
            "Paper daily-loss stop bypassed until the next UTC reset. Live remains protected.",
        )
        return {
            "ok": True,
            "mode": "paper",
            "daily_loss_bypass_active": True,
            "daily_loss_bypass_until": until,
        }

    async def _scan(self, markets: list[MarketWindow], now: float) -> None:
        mode = self.runtime.mode
        runtime = self.runtime.snapshot()
        entry = self.config["entry"]
        entry_override = bool(
            entry.get("confidence_only", False)
            or entry.get("profit_tuned", False)
        )
        day_start = _start_of_day(now)
        day_exposure = self.store.exposure_since(mode, day_start)
        realized = self.store.stats(mode)["total_pnl"]
        selection = self.live_market_selection()

        for market in markets:
            market_rejection = self.configured_market_rejection(
                market.asset, market.window
            )
            if market_rejection is not None:
                self.stats.reject(market_rejection)
                continue

            state = self.spot.get(market.asset)
            if state is None:
                continue

            self.stats.signal_scans += 1
            signal = self.model.evaluate(market, state, now)
            if signal is None:
                continue

            self._remember_signal(signal, now)

            hard_entry_price = float(entry.get("max_entry_price", 1.0))
            if signal.entry_price > hard_entry_price:
                self.stats.reject(
                    f"entry {signal.entry_price:.2f} above "
                    f"{hard_entry_price:.2f} hard price ceiling"
                )
                continue

            if not signal.tradeable:
                self.stats.reject(signal.reason or "unknown")
                continue

            if (
                not entry_override
                and not self.live_market_allowed(market.asset, market.window, selection)
            ):
                self.stats.reject("outside paper-tested live whitelist")
                continue

            # Entry cap per window. This is the single highest-impact rule in
            # the whole strategy, and it is measured rather than guessed:
            # replaying 597 real windows from a live profile's own fills, an
            # uncapped run returned -0.70% while capping at 3 returned +9.12%
            # on the same trades. Uncapped trading is averaging down into
            # losers — their 16+-entry windows won 28.8% and lost 12.9%.
            taken: int | None = None
            side_switches = 0
            existing = (
                self.store.open_position_for(mode, market.slug)
                if entry.get("reversal_enabled", False)
                else None
            )
            if existing is not None and existing["side"] != signal.side:
                taken = self.store.entries_for_window(mode, market.slug)
                side_switches = self.store.side_switches_for_window(mode, market.slug)
                if taken >= self.max_entries_per_window:
                    self.stats.reject(
                        f"window entry cap ({self.max_entries_per_window})"
                    )
                    continue
                # Every strategy profile routes an opposite-side signal through
                # the confirmed switch gate. An override must not turn a model
                # flip into an untracked, uncapped second direction.
                await self._maybe_reverse(
                    market, signal, existing, side_switches, now
                )
                continue

            getattr(self, "_reversal_candidates", {}).pop(market.slug, None)

            if not entry_override:
                if taken is None:
                    taken = self.store.entries_for_window(mode, market.slug)
                side_switches = self.store.side_switches_for_window(mode, market.slug)
                normal_cap = self.normal_entry_cap()
                if taken >= normal_cap:
                    self.stats.reject(
                        f"normal entry cap ({normal_cap}); reversal slot reserved"
                    )
                    continue

            stake = self.risk.size_for(signal.confidence, runtime["max_per_trade_usd"])
            market_capped_stake, market_cap_reason = self._apply_market_window_cap(
                mode, market.slug, stake
            )
            if market_capped_stake is None:
                self.stats.reject(market_cap_reason or "hard market-window cap")
                continue
            stake = market_capped_stake
            capped_stake, hard_cap_reason = self._apply_hard_window_cap(
                mode, market.window, market.end, stake
            )
            if capped_stake is None:
                self.stats.reject(hard_cap_reason or "hard market-window cap")
                continue
            stake = capped_stake
            bankroll_stake, bankroll_reason = self._apply_bankroll_cap(
                mode, stake, now
            )
            if bankroll_stake is None:
                self.stats.reject(bankroll_reason or "bankroll cap")
                continue
            stake = bankroll_stake
            if entry_override:
                # The clip remains an order-size setting. Every bot-level
                # rejection in RiskManager is deliberately bypassed.
                size_usd = stake
            else:
                decision = self.risk.check_entry(
                    asset=market.asset,
                    stake_usd=stake,
                    window_exposure_usd=self.store.interval_exposure(
                        mode, market.window, market.end
                    ),
                    day_exposure_usd=day_exposure,
                    realized_pnl=realized,
                    max_per_trade=runtime["max_per_trade_usd"],
                    max_per_window=runtime["max_per_window_usd"],
                    daily_cap=runtime["daily_cap_usd"],
                    ask_depth_usd=signal.ask_depth_usd,
                    now=now,
                )
                if not decision.allowed:
                    self.stats.reject(decision.reason or "risk")
                    self.runtime.update(
                        halt_reason=decision.reason if self.risk.breaker_active else None
                    )
                    continue
                size_usd = decision.size_usd

            fill = await self._execute_buy(
                market, signal.side, size_usd, signal.entry_price
            )
            if fill is None:
                self.stats.reject("unfillable at size")
                continue

            anchor = self._anchors.get(market.slug) or signal.anchor_price
            self.store.open_position(
                mode=mode,
                slug=market.slug,
                asset=market.asset,
                window=market.window,
                side=signal.side,
                confidence=signal.confidence,
                entry_price=fill.avg_price,
                shares=fill.shares,
                stake_usd=fill.stake_usd,
                anchor_price=anchor,
                opened_at=fill.ts,
                window_end=market.end,
                switches=(side_switches if not entry_override else 0),
                order_id=None,
                question=market.question,
            )
            if not entry_override:
                self.risk.record_entry(now)
            self.stats.entries += 1
            day_exposure += fill.stake_usd

            self.store.log(
                mode, "entry",
                f"{market.asset.upper()} {market.window} {signal.side.upper()} "
                f"{signal.confidence:.0%} @ {fill.avg_price:.2f} · "
                f"${fill.stake_usd:.2f} staked",
                asset=market.asset, slug=market.slug,
                detail=(
                    f"expected ROI {signal.expected_roi:.1%}, "
                    f"fee ${fill.fee_usd:.4f}, {signal.seconds_remaining:.0f}s left"
                ),
            )

    async def _maybe_reverse(
        self,
        market: MarketWindow,
        signal: Any,
        position: dict[str, Any],
        side_switches: int,
        now: float,
    ) -> None:
        """Buy one capped opposite-side leg after a persistent model flip.

        This does not pretend that crossing both asks locks a profit: after the
        spread and taker fees it usually locks a loss. The new direction instead
        receives a normal confidence-sized clip. Confirmation and the switch cap
        keep transient chop from repeatedly adding opposite exposure.
        """
        entry = self.config["entry"]
        if not entry.get("reversal_enabled", False):
            return
        if (
            self.runtime.mode == "live"
            and not entry.get("reversal_live_enabled", False)
        ):
            self.stats.reject("reversal awaiting Paper evidence for Live")
            return
        if signal.side == position["side"]:
            return
        hard_entry_price = float(entry.get("max_entry_price", 1.0))
        if signal.entry_price > hard_entry_price:
            self.stats.reject(
                f"entry {signal.entry_price:.2f} above "
                f"{hard_entry_price:.2f} hard price ceiling"
            )
            return
        if int(side_switches) >= int(entry.get("max_side_switches", 0)):
            self.stats.reject("side-switch cap reached")
            return
        if signal.confidence < float(entry.get("reversal_min_confidence", 1.0)):
            self.stats.reject("reversal confidence below threshold")
            return
        if signal.expected_roi < float(entry.get("reversal_min_expected_roi", 0.0)):
            self.stats.reject("reversal expected ROI below threshold")
            return
        if not self._reversal_confirmed(market.slug, signal.side, now):
            self.stats.reject("reversal awaiting confirmation")
            return

        runtime = self.runtime.snapshot()
        # A normal confidence-sized clip changes direction without pretending
        # that crossing both asks created a risk-free equal-share hedge.
        target = self.risk.size_for(
            signal.confidence, runtime["max_per_trade_usd"]
        )
        if target < self.risk.min_trade_usd:
            return

        market_capped_target, market_cap_reason = self._apply_market_window_cap(
            self.runtime.mode, market.slug, target
        )
        if market_capped_target is None:
            self.stats.reject(market_cap_reason or "hard market-window cap")
            return
        target = market_capped_target

        capped_target, hard_cap_reason = self._apply_hard_window_cap(
            self.runtime.mode, market.window, market.end, target
        )
        if capped_target is None:
            self.stats.reject(hard_cap_reason or "hard market-window cap")
            return
        target = capped_target

        bankroll_target, bankroll_reason = self._apply_bankroll_cap(
            self.runtime.mode, target, now
        )
        if bankroll_target is None:
            self.stats.reject(bankroll_reason or "bankroll cap")
            return
        target = bankroll_target

        decision = self.risk.check_entry(
            asset=market.asset,
            stake_usd=target,
            window_exposure_usd=self.store.interval_exposure(
                self.runtime.mode, market.window, market.end
            ),
            day_exposure_usd=self.store.exposure_since(self.runtime.mode, _start_of_day(now)),
            realized_pnl=self.store.stats(self.runtime.mode)["total_pnl"],
            max_per_trade=runtime["max_per_trade_usd"],
            max_per_window=runtime["max_per_window_usd"],
            daily_cap=runtime["daily_cap_usd"],
            ask_depth_usd=signal.ask_depth_usd,
            now=now,
        )
        if not decision.allowed:
            return

        fill = await self._execute_buy(
            market, signal.side, decision.size_usd, signal.entry_price
        )
        if fill is None:
            return

        next_switches = int(side_switches) + 1
        self.store.open_position(
            mode=self.runtime.mode,
            slug=market.slug,
            asset=market.asset,
            window=market.window,
            side=signal.side,
            confidence=signal.confidence,
            entry_price=fill.avg_price,
            shares=fill.shares,
            stake_usd=fill.stake_usd,
            anchor_price=self._anchors.get(market.slug) or signal.anchor_price,
            opened_at=fill.ts,
            window_end=market.end,
            switches=next_switches,
            order_id=None,
            question=market.question,
        )
        self.risk.record_entry(now)
        self.stats.entries += 1
        candidate = self._reversal_candidates.pop(market.slug, {})
        self.store.log(
            self.runtime.mode, "reversal",
            f"{market.asset.upper()} {market.window} side switch "
            f"{position['side'].upper()} -> {signal.side.upper()} "
            f"{signal.confidence:.0%} @ {fill.avg_price:.2f}",
            asset=market.asset, slug=market.slug,
            detail=(
                f"expected ROI {signal.expected_roi:.1%}, ${fill.stake_usd:.2f} staked, "
                f"confirmed by {candidate.get('observations', 0)} observations over "
                f"{max(0.0, now - float(candidate.get('first_seen', now))):.1f}s, "
                f"switch {next_switches}/{int(entry.get('max_side_switches', 0))}"
            ),
        )

    def _reversal_confirmed(self, slug: str, side: str, now: float) -> bool:
        """Return true only after consecutive, time-spanning opposite signals."""
        entry = self.config["entry"]
        min_seconds = max(
            0.0, float(entry.get("reversal_confirmation_seconds", 0.0))
        )
        min_observations = max(
            1, int(entry.get("reversal_min_observations", 1))
        )
        poll = max(
            0.5,
            float(self.config.get("engine", {}).get("poll_interval_seconds", 3.0)),
        )
        candidates = getattr(self, "_reversal_candidates", None)
        if candidates is None:
            candidates = self._reversal_candidates = {}
        candidate = candidates.get(slug)
        if (
            candidate is None
            or candidate["side"] != side
            or now - float(candidate["last_seen"]) > poll * 1.75
        ):
            candidate = {
                "side": side,
                "first_seen": now,
                "last_seen": now,
                "observations": 1,
            }
            candidates[slug] = candidate
        else:
            candidate["last_seen"] = now
            candidate["observations"] = int(candidate["observations"]) + 1

        return bool(
            int(candidate["observations"]) >= min_observations
            and now - float(candidate["first_seen"]) >= min_seconds
        )

    async def _execute_buy(
        self,
        market: MarketWindow,
        side: str,
        size_usd: float,
        max_price: float,
    ) -> Any:
        """Route an order to the active broker.

        The live path is intentionally allowed to raise: a live order that fails
        must be loud, not silently downgraded to a paper fill.
        """
        entry_config = getattr(self, "config", {}).get("entry", {})
        hard_entry_price = float(entry_config.get("max_entry_price", 1.0))
        if max_price > hard_entry_price:
            self.stats.reject(
                f"entry {max_price:.2f} above "
                f"{hard_entry_price:.2f} hard price ceiling"
            )
            return None

        # Re-read the one book that matters immediately before either simulated
        # or real execution. Paper is the live rehearsal, so it must not fill
        # against an older batch-refresh snapshot than Live would accept.
        await self.poly.refresh_book(market, side)

        if self.runtime.mode == "live":
            # A pause can arrive while the book refresh is in flight.  Check it
            # again at the last possible point before any real order is built.
            if getattr(self.runtime, "paused", False):
                self.stats.reject("engine paused before live order")
                return None

            now = time.time()
            final_size = float(size_usd)

            # Re-run both database-backed limits after the await above.  This
            # closes the gap between signal approval and order submission and
            # prevents any other completed fill from making this order exceed
            # the shared interval or total-open ceiling.
            if (
                getattr(self, "config", None)
                and hasattr(market, "slug")
            ):
                final_size, reason = self._apply_market_window_cap(
                    "live", market.slug, final_size
                )
                if final_size is None:
                    self.stats.reject(reason or "hard market-window cap reached")
                    return None

            if (
                getattr(self, "config", None)
                and hasattr(market, "window")
                and hasattr(market, "end")
            ):
                final_size, reason = self._apply_hard_window_cap(
                    "live", market.window, market.end, final_size
                )
                if final_size is None:
                    self.stats.reject(reason or "hard shared-window cap reached")
                    return None

            if getattr(self, "config", None):
                final_size, reason = self._apply_bankroll_cap(
                    "live", final_size, now
                )
                if final_size is None:
                    self.stats.reject(reason or "bankroll cap")
                    return None

                final_size, reason, must_pause = await asyncio.to_thread(
                    self._apply_live_wallet_cap, final_size
                )
                if final_size is None:
                    self.stats.reject(reason or "live wallet cap")
                    if must_pause:
                        self.runtime.update(
                            paused=True,
                            halt_reason=reason,
                            last_error=reason,
                        )
                        self.store.log(
                            "live",
                            "risk",
                            reason or "Live wallet risk check failed",
                            asset=getattr(market, "asset", None),
                            slug=getattr(market, "slug", None),
                        )
                    return None
                size_usd = final_size

        paper_plan = self.paper.plan_buy(market, side, size_usd, max_price)
        if paper_plan is None:
            self.stats.reject("fresh book cannot fill order")
            return None

        if self.runtime.mode == "paper":
            return self.paper.buy(market, side, size_usd, max_price)

        for attempt in range(2):
            if attempt and getattr(self, "config", None):
                # The retry is a second real submission, so it gets another
                # fresh wallet reconciliation instead of inheriting approval
                # from the first FOK attempt.
                if getattr(self.runtime, "paused", False):
                    self.stats.reject("engine paused before live retry")
                    return None
                retry_size, reason, must_pause = await asyncio.to_thread(
                    self._apply_live_wallet_cap, size_usd
                )
                if retry_size is None:
                    self.stats.reject(reason or "live wallet cap")
                    if must_pause:
                        self.runtime.update(
                            paused=True,
                            halt_reason=reason,
                            last_error=reason,
                        )
                    return None
                size_usd = retry_size
            try:
                fill = await asyncio.to_thread(
                    self.live.buy, market, side, size_usd, max_price
                )
                if fill is not None:
                    return fill
                return None
            except LiveTradingDisabled as exc:
                self.runtime.update(paused=True, last_error=str(exc))
                self.store.log(self.runtime.mode, "error", str(exc))
                return None
            except Exception as exc:
                # FOK guarantees the first attempt either filled completely or
                # did nothing, so exactly one fresh-book retry cannot duplicate
                # exposure. Every other live error remains loud and un-retried.
                if attempt == 0 and _is_no_fill_error(exc):
                    await self.poly.refresh_book(market, side)
                    paper_plan = self.paper.plan_buy(
                        market, side, size_usd, max_price
                    )
                    if paper_plan is not None:
                        continue
                self.stats.live_fill_misses += int(_is_no_fill_error(exc))
                self.runtime.update(last_error=f"live order failed: {exc}"[:240])
                self.store.log(
                    self.runtime.mode, "error", f"Live order failed: {exc}"[:240],
                    asset=market.asset, slug=market.slug,
                )
                return None


    # -- settlement --------------------------------------------------------

    async def _settle_closed(self, now: float) -> None:
        # A mode switch must not strand positions opened in the previous mode.
        # Settlement is bookkeeping, not new execution, so service both ledgers
        # while entries remain confined to the selected runtime mode.
        for mode in ("paper", "live"):
            self._settle_mode(mode, now)

    def _settle_mode(self, mode: str, now: float) -> None:
        for position in self.store.open_positions(mode):
            end = float(position["window_end"])
            if now < end + SETTLE_GRACE_SECONDS:
                continue

            state = self.spot.get(position["asset"])
            if state is None:
                # A previously enabled asset may later be removed from the feed
                # configuration. It can never acquire a close tick, so release
                # its exposure after the normal settlement recovery timeout.
                if now > end + 300:
                    self.store.close_position(position["id"], "unresolved")
                    self.store.log(
                        mode,
                        "warn",
                        f"{position['asset'].upper()} {position['window']} could not settle: "
                        "no settlement feed for this asset.",
                        asset=position["asset"],
                        slug=position["slug"],
                    )
                continue

            close_price = state.price_at_or_before(end)
            anchor = position["anchor_price"] or self._anchors.get(position["slug"])
            if close_price is None or not anchor:
                # Without both prices we cannot decide the outcome. Leave the
                # position open rather than guess; it settles once spot data
                # covering the boundary is available.
                if now > end + 300:
                    self.store.close_position(position["id"], "unresolved")
                    self.store.log(
                        mode, "warn",
                        f"{position['asset'].upper()} {position['window']} could not settle — "
                        "no spot price covering the window close.",
                        asset=position["asset"], slug=position["slug"],
                    )
                continue

            result = self.paper.settle(
                position["side"],
                float(position["shares"]),
                float(position["stake_usd"]),
                float(anchor),
                float(close_price),
            )
            self.store.record_settlement(
                position_id=position["id"],
                mode=mode,
                slug=position["slug"],
                asset=position["asset"],
                window=position["window"],
                side=position["side"],
                confidence=position["confidence"],
                entry_price=position["entry_price"],
                shares=position["shares"],
                stake_usd=position["stake_usd"],
                payout_usd=result["payout_usd"],
                pnl_usd=result["pnl_usd"],
                won=1 if result["won"] else 0,
                anchor_price=anchor,
                close_price=close_price,
                settled_at=now,
                method="spot",
            )
            self.store.close_position(position["id"], "settled")
            verdict = "WON" if result["won"] else "LOST"
            self.store.log(
                mode, "settlement",
                f"{position['asset'].upper()} {position['window']} {position['side'].upper()} "
                f"{verdict} {result['pnl_usd']:+.2f}",
                asset=position["asset"], slug=position["slug"],
                detail=f"anchor {anchor:.6g} → close {close_price:.6g}",
            )

    # -- reporting ---------------------------------------------------------

    def open_positions_view(self) -> list[dict[str, Any]]:
        """Open positions enriched with live spot progress for the UI."""
        out = []
        for position in self.store.open_positions(self.runtime.mode):
            state = self.spot.get(position["asset"])
            spot = state.last_price if state else None
            anchor = position["anchor_price"]
            status = "Awaiting spot"
            ahead: bool | None = None
            if spot is not None and anchor:
                up_ahead = spot >= float(anchor)
                ahead = up_ahead if position["side"] == "up" else not up_ahead
                status = "Currently ahead" if ahead else "Currently behind"

            market = self.poly.get(position["slug"])
            row = dict(position)
            row.update({
                "spot_price": spot,
                "status_label": status,
                "ahead": ahead,
                "seconds_remaining": round(float(position["window_end"]) - time.time(), 0),
                "decimals": ASSET_SPEC.get(position["asset"], {}).get("decimals", 2),
                "mark": (market.book_for(position["side"]).bids.best
                         if market and market.book_for(position["side"]) else None),
            })
            out.append(row)
        return out

    def performance_target_status(self, now: float | None = None) -> dict[str, Any]:
        """Measured progress toward operator targets; targets never imply a guarantee."""
        now = time.time() if now is None else now
        mode = self.runtime.mode
        cfg = self.config.get("performance_targets", {})
        hour = self.store.stats(mode, now - 3600.0)
        day = self.store.stats(mode, _start_of_day(now))
        return {
            "hourly_roi_target": float(cfg.get("hourly_roi", 0.0)),
            "daily_profit_target_min_usd": float(
                cfg.get("daily_profit_min_usd", 0.0)
            ),
            "daily_profit_target_max_usd": float(
                cfg.get("daily_profit_max_usd", 0.0)
            ),
            "hourly_roi": float(hour["roi"]),
            "hourly_pnl_usd": float(hour["total_pnl"]),
            "hourly_staked_usd": float(hour["staked"]),
            "daily_pnl_usd": float(day["total_pnl"]),
            "daily_staked_usd": float(day["staked"]),
            "target_market_note": cfg.get("market_note"),
        }

    def snapshot(self) -> dict[str, Any]:
        mode = self.runtime.mode
        stats = self.store.stats(mode)
        return {
            "runtime": self.runtime.snapshot(),
            "stats": stats,
            "engine": {
                "signal_scans": self.stats.signal_scans,
                "entries": self.stats.entries,
                "loops": self.stats.loops,
                "live_fill_misses": self.stats.live_fill_misses,
                "last_loop_ms": round(self.stats.last_loop_ms, 1),
                "rejections": dict(
                    sorted(self.stats.rejections.items(), key=lambda kv: -kv[1])[:8]
                ),
            },
            "risk": self.risk.snapshot(),
            "markets": self.poly.snapshot(),
            "live_availability": LiveBroker.availability(),
            "live_market_selection": self.live_market_selection(),
            "bankroll": self.bankroll_status(),
            "performance_targets": self.performance_target_status(),
            "strategy": self.strategy,
            "maker": {**self.maker.snapshot(), **self.maker_stats},
        }


def _start_of_day(now: float) -> float:
    """Midnight UTC preceding ``now`` — the daily-cap reset boundary."""
    return now - (now % 86400)


def _is_no_fill_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "no orders found to match",
            "no matching orders",
            "couldn't be fully filled",
            "could not be fully filled",
            "fok order",
        )
    )
