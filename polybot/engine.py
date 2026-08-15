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
from .feeds.polymarket import WINDOW_SECONDS, MarketWindow
from .feeds.spot import EXCHANGE, RESOLUTION
from .risk import RiskManager
from .settings import ASSET_SPEC, Runtime, live_trading_enabled
from .store import Store
from .strategy import AntsaslykuStrategy

# Wait this long after a window closes before settling, so a spot tick landing
# just after the boundary is included rather than raced.
SETTLE_GRACE_SECONDS = 3.0
SIGNAL_RETENTION_SECONDS = 24 * 60 * 60
# The venue publishes an outcome long after the window closes — measured on
# 2026-08-15, a window 186s old was still unresolved while everything beyond
# ~43min had resolved. Waiting for it before booking would pin every position
# against the open-exposure cap for the better part of an hour and stop the bot
# trading, so a window settles provisionally on the spot proxy and the
# reconciler below replaces that verdict once the venue answers.
#
# How far back the reconciler looks. Comfortably beyond observed publish
# latency, so a slow resolution is still caught.
SETTLEMENT_RECONCILE_LOOKBACK_SECONDS = 6 * 60 * 60
# How often it sweeps. The venue answers in minutes, not seconds; polling
# faster only spends API calls on windows that cannot have resolved yet.
SETTLEMENT_RECONCILE_INTERVAL_SECONDS = 300.0
# When to give up on a window no price series covers and the venue never
# resolved. Deliberately long: abandoning writes no settlement row, so the
# stake vanishes from the ledger entirely rather than being booked as a loss.
# Well past observed venue latency, so this should effectively never fire.
UNRESOLVED_ABANDON_SECONDS = 6 * 60 * 60
# How long an operator's manual daily-loss bypass holds. The stop itself still
# measures P/L across the UTC day, so when this elapses it re-engages against
# the same losing day unless P/L has recovered above the limit in the meantime.
DAILY_LOSS_BYPASS_SECONDS = 60 * 60


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
        self.risk = RiskManager(config)
        self.paper = PaperBroker(config)
        self.live = LiveBroker(config)

        # There is exactly one strategy in this project: the @antsaslyku
        # replication. Paper and Live run the same module against the same
        # feeds, so Paper is a true rehearsal rather than a different bot.
        self.antsaslyku = AntsaslykuStrategy(config)
        self.strategy = self.antsaslyku.NAME
        self.stats = EngineStats()
        self.latest_signals: dict[str, dict[str, Any]] = {}
        # Explicit operator override of the daily-loss stop, applying to Paper
        # and Live alike. Held in memory only, so it can never survive a
        # restart and leave an unattended box trading past its loss limit.
        self.daily_loss_bypass_until: float = 0.0
        self.daily_loss_bypass_scope: str = "hour"
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
        # Which price series each anchor was read from. Settlement takes the
        # window's close from the same one, so that the difference between
        # them is a price move rather than the basis between two feeds.
        self._anchor_sources: dict[str, str] = {}
        # Outcomes the venue has published, by slug. Fetched once per window
        # and cached because a resolution never changes once it exists.
        self._resolutions: dict[str, str] = {}
        # Last time the venue reconciler ran. Zero so the first loop sweeps
        # immediately, catching anything a restart settled provisionally.
        self._last_reconcile: float = 0.0

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
                anchor_source=position["anchor_source"],
                strategy=position["strategy"],
            )
            self.store.close_position(position["id"], "closed")
            self._release_window(position["slug"])
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

                # Data Streams prices land in their own series inside the
                # buffer, tagged RESOLUTION. They are emphatically NOT merged
                # with exchange spot: the two feeds sit a real basis apart —
                # dollars, on BTC — so a buffer that interleaves them makes
                # every read a coin flip between two different prices, and any
                # subtraction across them reports the basis as a price move.
                if self.chainlink.health.get("active"):
                    for asset, sp in self.chainlink.latest.items():
                        state = self.spot.get(asset)
                        if state is not None:
                            state.push(sp.price, sp.received_at, RESOLUTION)
                            state.sources.add(RESOLUTION)

                self.risk.update_regimes(self.spot.snapshot())
                self.risk.update_hourly_loss(
                    self.store.pnl_since(
                        self.runtime.mode, now - self.risk.dd_lookback_seconds
                    ),
                    now,
                )

                self._capture_anchors(live_markets, now)

                await self._settle_closed(now)

                # Second settlement phase, on its own clock. Provisional spot
                # verdicts booked above are replaced by the venue's once it
                # publishes — see reconcile_settlements.
                if now - self._last_reconcile >= SETTLEMENT_RECONCILE_INTERVAL_SECONDS:
                    self._last_reconcile = now
                    await self.reconcile_settlements(now)

                if not self.runtime.paused:
                    await self._run_strategy(live_markets, now)

                self.runtime.update(last_error=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.runtime.update(last_error=f"{type(exc).__name__}: {exc}"[:240])

            self.stats.last_loop_ms = (time.perf_counter() - started) * 1000.0
            self.stats.loops += 1
            await asyncio.sleep(max(0.5, interval - (time.perf_counter() - started)))

    def _anchor_source_of(self, slug: str) -> str | None:
        """Series a window was anchored on, if this process captured it."""
        return getattr(self, "_anchor_sources", {}).get(slug)

    def _settlement_source(self, state: Any, start: float) -> str | None:
        """Which price series decides this window, open and close alike.

        The resolution stream is preferred because it is what the venue
        actually settles on. Exchange spot is the fallback for windows that
        opened before the stream connected — its own open is still readable,
        and a window judged consistently on the predictor feed is far better
        than one judged on the basis between the two.
        """
        for candidate in (RESOLUTION, EXCHANGE):
            if state.price_at_or_before(start, candidate) is not None:
                return candidate
        return None

    def _capture_anchors(self, markets: list[MarketWindow], now: float) -> None:
        """Latch each window's opening price the first time we can read it."""
        for market in markets:
            if market.slug in self._anchors:
                continue
            state = self.spot.get(market.asset)
            if state is None:
                continue
            source = self._settlement_source(state, market.start)
            if source is None:
                continue
            anchor = state.price_at_or_before(market.start, source)
            if anchor:
                self._anchors[market.slug] = anchor
                # Settlement must read the close from this same series.
                self._anchor_sources[market.slug] = source

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
            self._anchor_sources.pop(slug, None)
            self._resolutions.pop(slug, None)

    # -- market eligibility -------------------------------------------------

    def configured_market_allowed(self, asset: str, window: str) -> bool:
        """Return false for a cohort the operator has switched off."""
        return self.configured_market_rejection(asset, window) is None

    def configured_market_rejection(self, asset: str, window: str) -> str | None:
        """Explain the operator exclusion blocking a cohort, if any.

        This is the one gate nothing may bypass. It is deliberately separate
        from the paper-tested live whitelist below, which is advisory.
        """
        asset = str(asset).lower()
        window = str(window).lower()
        excluded = any(
            isinstance(row, dict)
            and str(row.get("asset", "")).lower() == asset
            and str(row.get("window", "")).lower() == window
            for row in self.config.get("excluded_markets", [])
        )
        return "market disabled by operator" if excluded else None

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

    def _apply_hard_window_cap(
        self,
        mode: str,
        window: str,
        window_end: float,
        requested_usd: float,
    ) -> tuple[float | None, str | None]:
        """Cap one shared timed interval across every configured asset.

        Every enabled asset ending in the same timed window consumes the same
        allowance, so the configured cap is not silently multiplied by the
        number of assets.
        """
        cap = float(self.config["limits"].get("hard_window_cap_usd", 0.0))
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
        limits = self.config["limits"]
        cap = float(
            limits.get(
                "hard_market_window_cap_usd",
                limits.get("hard_window_cap_usd", 0.0),
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

    def _actual_open_exposure(self, mode: str, strategy: str | None = None) -> float:
        """Capital committed to open positions, from the ledger.

        Every fill this project makes becomes a positions row, so the ledger is
        the whole picture — there is no resting-quote inventory to add.
        """
        return self.store.open_exposure(mode, strategy)

    def _bankroll_scope(self, mode: str, strategy: str | None) -> str | None:
        """Whose budget a prospective order is charged against.

        One strategy, one bankroll, in both modes. The parameter is retained so
        the limit helpers keep a single signature, and so a future second
        strategy can be given its own Paper allowance without reworking them.
        """
        return None if mode == "live" else strategy

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
        pending_quotes = 0.0
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
            "daily_loss_bypass_active": self._daily_loss_bypass_active(now),
            "daily_loss_bypass_until": self.daily_loss_bypass_until,
            "daily_loss_bypass_scope": getattr(
                self, "daily_loss_bypass_scope", "hour"
            ),
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
        strategy: str | None = None,
    ) -> tuple[float | None, str | None]:
        """Apply capital, turnover and loss limits that overrides cannot skip."""
        cfg = self.config.get("bankroll", {})
        if not cfg:
            return float(requested_usd), None

        # Live: one shared bankroll. Paper: one budget per strategy, so a
        # comparison is not decided by loop order. See _bankroll_scope.
        scope = self._bankroll_scope(mode, strategy)

        day_start = max(
            _start_of_day(now), float(cfg.get("activated_at", 0.0))
        )
        bankroll = float(cfg.get("starting_balance_usd", 0.0))
        # Deliberately not prefixed with the scope: each strategy's rejections
        # are already recorded against its own stats object, and operator-facing
        # reason strings are matched elsewhere.
        bankroll_label = f"${bankroll:,.0f} bankroll" if bankroll > 0 else "bankroll"
        max_daily_loss = float(cfg.get("max_daily_loss_usd", 0.0))
        daily_pnl = float(self.store.stats(mode, day_start, scope)["total_pnl"])
        if (
            max_daily_loss > 0
            and daily_pnl <= -max_daily_loss
            and not self._daily_loss_bypass_active(now)
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
        open_exposure = self._actual_open_exposure(mode, scope)
        open_remaining = (
            max(0.0, max_open - open_exposure)
            if max_open > 0
            else float(requested_usd)
        )

        max_turnover = float(cfg.get("max_daily_turnover_usd", 0.0))
        day_turnover = self.store.exposure_since(mode, day_start, scope)
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

        tracked_exposure = self._actual_open_exposure("live")
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

    def _daily_loss_bypass_active(self, now: float) -> bool:
        return bool(now < getattr(self, "daily_loss_bypass_until", 0.0))

    def bypass_daily_loss_stop(
        self,
        now: float | None = None,
        scope: str = "hour",
    ) -> dict[str, Any]:
        """Ignore the daily-loss stop, in Paper or Live alike.

        This is an operator override of a real-money backstop: while it holds,
        a mode that has already hit ``max_daily_loss_usd`` keeps opening new
        positions. ``scope="hour"`` re-arms the stop an hour later without
        anyone present; ``scope="day"`` holds until the next UTC reset, which
        is the whole of the losing day and cannot expire on its own before it.
        """
        if scope not in {"hour", "day"}:
            raise ValueError("scope must be 'hour' or 'day'")
        now = time.time() if now is None else now
        until = (
            _start_of_day(now) + 86400.0
            if scope == "day"
            else now + DAILY_LOSS_BYPASS_SECONDS
        )
        self.daily_loss_bypass_until = until
        self.daily_loss_bypass_scope = scope
        mode = self.runtime.mode
        window = (
            "the rest of the UTC day"
            if scope == "day"
            else f"{DAILY_LOSS_BYPASS_SECONDS / 60:.0f} minutes"
        )
        self.store.log(
            mode,
            "risk",
            f"{mode.upper()} daily-loss stop bypassed for {window}.",
        )
        return {
            "ok": True,
            "mode": mode,
            "daily_loss_bypass_active": True,
            "daily_loss_bypass_until": until,
            "daily_loss_bypass_scope": scope,
        }

    # -- strategy selection -------------------------------------------------

    def configured_strategies(self) -> list[str]:
        """Strategies this process can run.

        There is exactly one. The list survives because the dashboard, the
        store's ``strategy`` discriminator and the settlement path all key off
        strategy names, and keeping the shape means a second strategy can be
        added later without a schema change or a UI rewrite.
        """
        return [self.antsaslyku.NAME]

    @property
    def selected_strategy(self) -> str | None:
        return self.antsaslyku.NAME

    def select_strategy(self, name: str) -> dict[str, Any]:
        if name != self.antsaslyku.NAME:
            return {
                "ok": False,
                "error": f"'{name}' is not configured to run. "
                         f"Available: {self.configured_strategies()}",
            }
        return {
            "ok": True,
            "selected_strategy": name,
            "available": self.configured_strategies(),
        }

    def strategy_active(self, name: str, mode: str | None = None) -> bool:
        """Whether ``name`` may open new positions right now."""
        return name == self.antsaslyku.NAME

    def strategy_status(self) -> dict[str, Any]:
        return {
            "mode": self.runtime.mode,
            "configured": self.configured_strategies(),
            "selected": self.selected_strategy,
            "active": self.configured_strategies(),
            # One strategy, so the dashboard segment is a label rather than a
            # switch, in Paper and Live alike.
            "exclusive": False,
            "primary": self.strategy,
        }

    # -- entries -----------------------------------------------------------

    async def _run_strategy(self, markets: list[MarketWindow], now: float) -> None:
        """One pass of the @antsaslyku replication.

        Deliberately thin. The strategy decides *what* to buy; this method only
        applies the operator's mandatory bankroll controls and routes the
        order. Every hard limit an override cannot skip is re-applied inside
        :meth:`_execute_buy` on the live path, so the strategy cannot trade past
        the declared bankroll no matter what its own config block says.
        """
        strat = self.antsaslyku
        mode = self.runtime.mode
        strat.prune(now)

        for market in markets:
            # Operator market exclusions outrank the strategy.
            if self.configured_market_rejection(market.asset, market.window) is not None:
                continue
            if not self.live_market_allowed(market.asset, market.window):
                continue

            state = self.spot.get(market.asset)
            if state is None:
                continue

            intent = strat.evaluate(market, state, now)
            if intent is None:
                continue
            self._remember_signal(intent, now)
            if not intent.tradeable:
                self.stats.reject(intent.reason or "unknown")
                continue

            if strat.dry_run:
                # Score and log without touching a broker. This is how a
                # parameter change is meant to be forward-tested before it is
                # allowed to reach real money.
                self.store.log(
                    mode, "strategy",
                    f"[dry-run] {intent.stage} {intent.side} {market.slug} "
                    f"${intent.size_usd:.2f} @ {intent.limit_price:.2f} "
                    f"conf {intent.confidence:.0%}",
                    asset=market.asset, slug=market.slug,
                )
                strat.record_fill(
                    market.slug, intent.side, intent.size_usd,
                    intent.size_usd / max(intent.limit_price, 1e-6),
                    intent.stage, now,
                )
                continue

            size_usd, reason = self._apply_market_window_cap(
                mode, market.slug, intent.size_usd
            )
            if size_usd is None:
                self.stats.reject(reason or "hard market-window cap")
                continue
            size_usd, reason = self._apply_hard_window_cap(
                mode, market.window, market.end, size_usd
            )
            if size_usd is None:
                self.stats.reject(reason or "hard shared-window cap")
                continue
            size_usd, reason = self._apply_bankroll_cap(
                mode, size_usd, now, strat.NAME
            )
            if size_usd is None:
                self.stats.reject(reason or "bankroll cap")
                continue

            decision = self.risk.check_entry(
                asset=market.asset,
                stake_usd=size_usd,
                window_exposure_usd=self.store.interval_exposure(
                    mode, market.window, market.end
                ),
                day_exposure_usd=self.store.exposure_since(mode, _start_of_day(now)),
                realized_pnl=self.store.stats(mode)["total_pnl"],
                max_per_trade=self.runtime.max_per_trade_usd,
                max_per_window=self.runtime.max_per_window_usd,
                daily_cap=self.runtime.daily_cap_usd,
                ask_depth_usd=intent.ask_depth_usd,
                now=now,
            )
            if not decision.allowed:
                self.stats.reject(decision.reason or "risk")
                self.runtime.update(
                    halt_reason=decision.reason if self.risk.breaker_active else None
                )
                continue

            fill = await self._execute_buy(
                market, intent.side, decision.size_usd, intent.limit_price
            )
            if fill is None:
                self.stats.reject("unfillable at size")
                continue

            strat.record_fill(
                market.slug, intent.side, fill.stake_usd, fill.shares,
                intent.stage, now, intent.lock_profit_usd,
            )
            self.risk.record_entry(now)
            self.stats.entries += 1

            self.store.open_position(
                anchor_source=self._anchor_source_of(market.slug),
                mode=mode,
                slug=market.slug,
                asset=market.asset,
                window=market.window,
                side=intent.side,
                confidence=intent.confidence,
                entry_price=fill.avg_price,
                shares=fill.shares,
                stake_usd=fill.stake_usd,
                anchor_price=self._anchors.get(market.slug),
                opened_at=fill.ts,
                window_end=market.end,
                switches=0,
                order_id=getattr(fill, "order_id", None),
                question=market.question,
                strategy=strat.NAME,
            )
            self.store.log(
                mode, "lock" if intent.is_lock else "entry",
                f"{market.asset.upper()} {market.window} {intent.side.upper()} "
                + (
                    f"LOCK +${intent.lock_profit_usd:.2f} guaranteed @ "
                    f"{fill.avg_price:.2f} · ${fill.stake_usd:.2f} staked"
                    if intent.is_lock else
                    f"{intent.stage} {intent.confidence:.0%} @ {fill.avg_price:.2f} · "
                    f"${fill.stake_usd:.2f} staked"
                ),
                asset=market.asset, slug=market.slug,
                detail=(
                    (
                        "risk-free: the window now pays more than it cost "
                        "whichever side resolves"
                    ) if intent.is_lock else (
                        f"edge {intent.edge:+.3f}, fill #{intent.fill_index + 1}, "
                        f"fee ${fill.fee_usd:.4f}, "
                        f"{intent.seconds_into_window:.0f}s into the window"
                    )
                ),
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
        hard_entry_price = float(
            getattr(self, "config", {}).get("strategy", {}).get("max_entry_price", 1.0)
        )
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

    async def reconcile_settlements(self, now: float) -> dict[str, Any]:
        """Replace provisional spot verdicts with the outcome the venue published.

        Settlement runs in two phases because the two things it does have
        opposite deadlines. Releasing capital must happen the instant a window
        closes, or exposure piles up against the bankroll cap and the bot stops
        trading. Being *right* cannot happen then, because the venue does not
        publish for minutes to tens of minutes after the close.

        So a window books immediately on the spot proxy, and this pass corrects
        it afterwards. That matters because the proxy is not merely noisy: the
        exchanges we poll are not the Chainlink stream the venue resolves on,
        and across 27,679 replayed windows the inferred verdict was wrong 13.7%
        of the time — enough to turn a measured +$18.6k into -$8.8k.

        Corrections are P/L-neutral for capital that has already moved; they
        only fix the ledger, which is what every strategy decision is judged on.
        """
        poly = getattr(self, "poly", None)
        if poly is None or not hasattr(poly, "resolution"):
            return {"checked": 0, "corrected": 0}
        resolutions = getattr(self, "_resolutions", None)
        if resolutions is None:
            resolutions = self._resolutions = {}

        rows = self.store.provisional_settlements(
            now - SETTLEMENT_RECONCILE_LOOKBACK_SECONDS
        )

        # Windows that have closed but could not be settled from any price
        # series — after a restart, or when a feed drops mid-window. They are
        # the ones that most need the venue, since it is the only route left
        # that requires no price, so their resolutions are fetched here too.
        stranded: set[str] = set()
        for mode in ("paper", "live"):
            for position in self.store.open_positions(mode):
                if now >= float(position["window_end"]) + SETTLE_GRACE_SECONDS:
                    stranded.add(position["slug"])

        if not rows and not stranded:
            return {"checked": 0, "corrected": 0}

        wanted = {r["slug"] for r in rows} | stranded
        unknown = sorted({s for s in wanted if s not in resolutions})
        if unknown:
            results = await asyncio.gather(
                *(poly.resolution(slug) for slug in unknown),
                return_exceptions=True,
            )
            for slug, outcome in zip(unknown, results):
                if outcome in ("up", "down"):
                    resolutions[slug] = outcome

        corrected = 0
        delta = 0.0
        for row in rows:
            outcome = resolutions.get(row["slug"])
            if outcome is None:
                continue
            won = 1 if str(row["side"]).strip().lower() == outcome else 0
            payout = round(float(row["shares"]), 4) if won else 0.0
            pnl = round(payout - float(row["stake_usd"]), 4)
            if won == row["won"] and abs(pnl - float(row["pnl_usd"])) < 0.005:
                # The proxy happened to agree. Still stamp it, so the row
                # records that the venue confirmed it rather than that nobody
                # ever checked.
                self.store.apply_venue_outcome(
                    row["id"], won, payout, float(row["pnl_usd"])
                )
                continue
            self.store.apply_venue_outcome(row["id"], won, payout, pnl)
            delta += pnl - float(row["pnl_usd"])
            corrected += 1

        if corrected:
            self.store.log(
                self.runtime.mode, "settlement",
                f"Venue reconcile: {corrected} of {len(rows)} settlements corrected "
                f"({delta:+.2f} P/L)",
                detail="spot inferred the wrong side; the venue's outcome now stands",
            )
        return {"checked": len(rows), "corrected": corrected, "delta": round(delta, 4)}

    def _release_window(self, slug: str) -> None:
        """Drop the strategy's ladder bookkeeping for a finished window.

        The strategy tracks open cost and open-window count in memory to
        enforce ``max_capital_usd`` and ``max_open_windows``. Nothing else
        clears it, so without this every settled window would keep consuming
        both budgets and the strategy would ratchet itself shut after a few
        hours — the ledger would show no open exposure while the strategy
        refused to trade. Tying the release to settlement keeps the two views
        in agreement.

        Deliberately tolerant of a missing strategy: this is bookkeeping, and
        settling a real position must never fail because of it.
        """
        strategy = getattr(self, "antsaslyku", None)
        if strategy is not None:
            strategy.drop_window(slug)

    def _settlement_pair(
        self, state: Any, position: dict[str, Any], end: float
    ) -> tuple[float | None, float | None, str | None]:
        """An anchor and close for this window, both from one price series.

        Coherence is the requirement, not any particular feed. Reading the
        anchor from one series and the close from another measures the basis
        between them, which is how a losing window came to be booked as a
        +$228.51 win. But *pinning* to the series that produced the anchor is
        too strict on its own: a restart rebuilds the buffer from scratch, so a
        stream that connected after the window closed cannot supply its close,
        and the position strands forever.

        So the recorded source is preferred, and any other series that covers
        both the open and the close is accepted as a whole — anchor re-derived
        along with it, never spliced onto the old one.
        """
        start = end - WINDOW_SECONDS.get(position["window"], 0)
        recorded = (
            position["anchor_source"] or self._anchor_source_of(position["slug"])
        )
        anchor = position["anchor_price"] or self._anchors.get(position["slug"])

        if recorded and anchor:
            close = state.price_at_or_before(end, recorded)
            if close is not None:
                return float(anchor), float(close), recorded

        for candidate in (recorded, RESOLUTION, EXCHANGE):
            if candidate is None:
                continue
            open_price = state.price_at_or_before(start, candidate)
            close = state.price_at_or_before(end, candidate)
            if open_price is not None and close is not None:
                return float(open_price), float(close), candidate
        return None, None, None

    def _settle_from_venue(
        self, mode: str, position: dict[str, Any], outcome: str, now: float
    ) -> None:
        """Book a position against the outcome the venue published.

        No price comparison is involved, so there is no feed, no basis and no
        anchor to get wrong: the side either matches the winning outcome or it
        does not, and a winning share pays exactly $1. The anchor and close we
        observed are still recorded, so a later audit can see what the spot
        model would have concluded and how often it disagreed.
        """
        won = str(position["side"]).strip().lower() == outcome
        shares = float(position["shares"])
        stake = float(position["stake_usd"])
        payout = round(shares, 4) if won else 0.0
        pnl = round(payout - stake, 4)

        state = self.spot.get(position["asset"])
        source = (
            position["anchor_source"] or self._anchor_source_of(position["slug"])
        )
        close_price = (
            state.price_at_or_before(float(position["window_end"]), source)
            if state is not None and source
            else None
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
            payout_usd=payout,
            pnl_usd=pnl,
            won=1 if won else 0,
            anchor_price=position["anchor_price"],
            close_price=close_price,
            settled_at=now,
            method="venue",
            anchor_source="venue",
            strategy=position["strategy"],
        )
        self.store.close_position(position["id"], "settled")
        self._release_window(position["slug"])
        self.store.log(
            mode, "settlement",
            f"{position['asset'].upper()} {position['window']} "
            f"{str(position['side']).upper()} {'WON' if won else 'LOST'} {pnl:+.2f}",
            asset=position["asset"], slug=position["slug"],
            detail=f"venue resolved {outcome.upper()}",
        )

    def _settle_mode(self, mode: str, now: float) -> None:
        for position in self.store.open_positions(mode):
            end = float(position["window_end"])
            if now < end + SETTLE_GRACE_SECONDS:
                continue

            # If the venue has already answered — an older window being settled
            # after a restart, or one the reconciler has cached — use it. It
            # needs no price at all: a winning share pays $1, a losing one 0.
            # Otherwise settle provisionally on spot below and let the
            # reconciler correct it; holding out for the venue here would pin
            # capital for the better part of an hour.
            outcome = getattr(self, "_resolutions", {}).get(position["slug"])
            if outcome is not None:
                self._settle_from_venue(mode, position, outcome, now)
                continue

            state = self.spot.get(position["asset"])
            if state is None:
                # A previously enabled asset may later be removed from the feed
                # configuration, so it can never acquire a close tick. The
                # venue can still resolve it — that check ran above and will
                # run again next loop — so hold rather than abandon a real
                # stake over a config change.
                if now > end + UNRESOLVED_ABANDON_SECONDS:
                    self.store.close_position(position["id"], "unresolved")
                    self._release_window(position["slug"])
                    self.store.log(
                        mode,
                        "warn",
                        f"{position['asset'].upper()} {position['window']} abandoned: "
                        "no settlement feed for this asset and no venue outcome. "
                        f"${float(position['stake_usd']):.2f} is missing from this "
                        "ledger's P/L.",
                        asset=position["asset"],
                        slug=position["slug"],
                    )
                continue

            anchor, close_price, source = self._settlement_pair(state, position, end)
            if close_price is None or not anchor:
                # No series covers both ends of this window. Hold — the venue
                # will answer, and _settle_from_venue needs no price at all.
                # Abandoning here would close the position with no settlement
                # row, which does not merely lose the P/L: it deletes a real
                # stake from the ledger, so the strategy looks like it never
                # made the trade.
                if now > end + UNRESOLVED_ABANDON_SECONDS:
                    self.store.close_position(position["id"], "unresolved")
                    self._release_window(position["slug"])
                    self.store.log(
                        mode, "warn",
                        f"{position['asset'].upper()} {position['window']} abandoned after "
                        f"{UNRESOLVED_ABANDON_SECONDS / 3600:.0f}h — no price series covers "
                        "the window and the venue never published an outcome. "
                        f"${float(position['stake_usd']):.2f} is missing from this "
                        "ledger's P/L.",
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
                anchor_source=source,
                # Inherited from the position rather than from whatever is
                # selected now: a window opened by one strategy must settle
                # into that strategy's curve even if the operator has since
                # switched.
                strategy=position["strategy"],
            )
            self.store.close_position(position["id"], "settled")
            self._release_window(position["slug"])
            verdict = "WON" if result["won"] else "LOST"
            self.store.log(
                mode, "settlement",
                f"{position['asset'].upper()} {position['window']} {position['side'].upper()} "
                f"{verdict} {result['pnl_usd']:+.2f}",
                asset=position["asset"], slug=position["slug"],
                detail=f"anchor {anchor:.6g} → close {close_price:.6g}",
            )

    # -- reporting ---------------------------------------------------------

    def open_positions_view(
        self, strategy: str | None = None
    ) -> list[dict[str, Any]]:
        """Open positions enriched with live spot progress for the UI."""
        out = []
        for position in self.store.open_positions(self.runtime.mode, strategy):
            state = self.spot.get(position["asset"])
            # Read the live price from the series this window was anchored on,
            # so "ahead"/"behind" answers the same question settlement will.
            # Against the other feed it compares across the basis and can show
            # a winning position as losing right up to the moment it settles.
            source = (
                position["anchor_source"]
                or self._anchor_source_of(position["slug"])
            )
            latest = state.latest_from(source) if state and source else None
            spot = latest[1] if latest else (state.last_price if state else None)
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
            "strategies": {self.antsaslyku.NAME: self.antsaslyku.snapshot()},
            "strategy_selection": self.strategy_status(),
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
