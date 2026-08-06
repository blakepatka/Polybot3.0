"""Risk layer: regime gating, circuit breakers, and position sizing.

Three independent brakes, each able to stop entries on its own:

* **Volatility regime**, per asset. Above the ceiling the lead/lag edge stops
  being reliable, so that asset opens no new windows. Hysteresis (halt above
  60bps, resume below 50bps) prevents an asset flapping in and out of the
  regime on a single noisy tick.
* **Hourly loss breaker**, global. If net settled P/L over the rolling hour
  loses the configured amount, everything halts for a cooldown.
* **Exposure caps** — per trade, per window, per day, plus a profit lock.

Every brake reports a human-readable reason, because a bot that silently stops
trading is indistinguishable from a bot that is broken.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RegimeState:
    """Per-asset volatility regime with hysteresis."""

    asset: str
    halted: bool = False
    vol_bps: float | None = None
    since: float = field(default_factory=time.time)

    def update(self, vol_bps: float | None, halt_above: float, resume_below: float) -> None:
        self.vol_bps = vol_bps
        if vol_bps is None:
            # Unknown volatility is not a reason to halt; the signal model will
            # decline to produce a view anyway without enough ticks.
            return
        if not self.halted and vol_bps > halt_above:
            self.halted = True
            self.since = time.time()
        elif self.halted and vol_bps < resume_below:
            self.halted = False
            self.since = time.time()


@dataclass
class RiskDecision:
    allowed: bool
    reason: str | None = None
    size_usd: float = 0.0


class RiskManager:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        risk = config["risk"]
        vol = risk["volatility_regime"]
        self.halt_above_bps = float(vol["halt_above_bps"])
        self.resume_below_bps = float(vol["resume_below_bps"])
        self.vol_lookback = float(vol["lookback_seconds"])

        dd = risk["drawdown_breaker"]
        self.dd_lookback_seconds = float(dd.get("lookback_seconds", 3600.0))
        self.dd_max_usd = float(dd.get("max_loss_usd", dd.get("max_drawdown_usd", 100.0)))
        self.dd_cooldown = float(dd["cooldown_seconds"])

        cb = risk["circuit_breaker"]
        self.cb_max_trades = int(cb["max_consecutive_large_trades"])
        self.cb_window = float(cb["window_seconds"])
        self.cb_cooldown = float(cb["cooldown_seconds"])

        self.min_trade_usd = float(risk["trade_floor"]["min_trade_size_usd"])
        self.min_depth_usd = float(risk["depth_guard"]["min_orderbook_depth_usd"])

        sizing = config["sizing"]
        self.base_clip = float(sizing["base_clip_usd"])
        self.profit_lock_usd = float(sizing["profit_lock_usd"])

        self.regimes: dict[str, RegimeState] = {}
        self._recent_entries: list[float] = []

        # Set when the drawdown breaker trips; entries stay blocked until it
        # expires even if P/L recovers, so the bot cannot immediately re-enter
        # whatever just cost it money.
        self.breaker_until: float = 0.0
        self.breaker_reason: str | None = None
        self.breaker_drawdown: float = 0.0
        # The rolling settlement window does not change just because a
        # cooldown elapsed. Remember the loss that caused the last trip so the
        # same history cannot restart the timer forever.
        self._drawdown_breach_latched = False

    # -- regime ------------------------------------------------------------

    def update_regimes(self, spot_snapshot: list[dict[str, Any]]) -> None:
        for entry in spot_snapshot:
            asset = entry["asset"]
            regime = self.regimes.setdefault(asset, RegimeState(asset))
            regime.update(entry.get("vol_bps"), self.halt_above_bps, self.resume_below_bps)

    def asset_halted(self, asset: str) -> bool:
        regime = self.regimes.get(asset)
        return bool(regime and regime.halted)

    # -- drawdown breaker --------------------------------------------------

    def update_hourly_loss(self, hourly_pnl: list[float], now: float | None = None) -> None:
        """Trip only when rolling-hour net settled P/L is -limit or worse."""
        now = now if now is not None else time.time()
        if now < self.breaker_until:
            return
        if self.breaker_until and now >= self.breaker_until:
            self.breaker_until = 0.0
            self.breaker_reason = None

        hourly_total = sum(hourly_pnl)

        self.breaker_drawdown = hourly_total
        threshold_breached = hourly_total <= -self.dd_max_usd
        if not threshold_breached:
            # A recovered rolling window arms the breaker for the next
            # independent drawdown.
            self._drawdown_breach_latched = False
            return

        # One continuous threshold breach gets one cooldown. The rolling total
        # may move further negative while old settlements remain in the
        # lookback, but that must not restart the halt when its timer expires.
        # The breaker is re-armed only after the total recovers above the limit.
        if not self._drawdown_breach_latched:
            self.breaker_until = now + self.dd_cooldown
            self._drawdown_breach_latched = True
            self.breaker_reason = (
                f"Hourly loss breaker tripped at ${hourly_total:,.2f} net settled P/L "
                f"over the last {self.dd_lookback_seconds / 60:.0f} minutes."
            )

    @property
    def breaker_active(self) -> bool:
        return time.time() < self.breaker_until

    def breaker_seconds_remaining(self) -> float:
        return max(0.0, self.breaker_until - time.time())

    # -- entry gate --------------------------------------------------------

    def check_entry(
        self,
        *,
        asset: str,
        stake_usd: float,
        window_exposure_usd: float,
        day_exposure_usd: float,
        realized_pnl: float,
        max_per_trade: float,
        max_per_window: float,
        daily_cap: float,
        ask_depth_usd: float,
        now: float | None = None,
    ) -> RiskDecision:
        """Approve, resize, or reject a proposed entry."""
        now = now if now is not None else time.time()

        if self.breaker_active:
            secs = self.breaker_seconds_remaining()
            return RiskDecision(False, f"{self.breaker_reason} Resuming in {secs:.0f}s.")

        if self.asset_halted(asset):
            regime = self.regimes[asset]
            vol = regime.vol_bps or 0.0
            return RiskDecision(
                False, f"{asset.upper()} volatility {vol:.1f}bps above the {self.halt_above_bps:.0f}bps ceiling"
            )

        if realized_pnl >= self.profit_lock_usd:
            return RiskDecision(False, f"Profit lock reached (${realized_pnl:,.2f})")

        if ask_depth_usd < self.min_depth_usd:
            return RiskDecision(False, f"Book depth ${ask_depth_usd:,.0f} below guard")

        # Rate limiter: too many entries inside the circuit-breaker window.
        self._recent_entries = [t for t in self._recent_entries if now - t <= self.cb_window]
        if len(self._recent_entries) >= self.cb_max_trades:
            return RiskDecision(
                False,
                f"Circuit breaker: {len(self._recent_entries)} entries in {self.cb_window:.0f}s",
            )

        # Clamp against every exposure ceiling, then check the floor once.
        size = min(
            stake_usd,
            max_per_trade,
            max(0.0, max_per_window - window_exposure_usd),
            max(0.0, daily_cap - day_exposure_usd),
        )

        if size < self.min_trade_usd:
            if daily_cap - day_exposure_usd < self.min_trade_usd:
                return RiskDecision(False, f"Daily cap reached (${day_exposure_usd:,.2f}/${daily_cap:,.2f})")
            if max_per_window - window_exposure_usd < self.min_trade_usd:
                return RiskDecision(False, "Per-window cap reached")
            return RiskDecision(False, f"Size ${size:.2f} below ${self.min_trade_usd:.2f} floor")

        return RiskDecision(True, None, size)

    def record_entry(self, now: float | None = None) -> None:
        self._recent_entries.append(now if now is not None else time.time())

    # -- sizing ------------------------------------------------------------

    def size_for(self, confidence: float, max_per_trade: float) -> float:
        """Scale the clip with conviction, between the base clip and the slider.

        The slider is interpolated *toward*, not merely clamped against. An
        earlier version scaled from ``base_clip`` to ``2 x base_clip`` and only
        then clamped, which meant raising the slider above twice the base clip
        changed nothing at all — the control looked live but was inert.

        Now a marginal signal still gets the base clip, and full conviction
        gets the slider value, so moving it has a visible effect at every
        setting. It remains a hard ceiling: the result never exceeds it.
        """
        ceiling = max(max_per_trade, self.min_trade_usd)
        floor = min(self.base_clip, ceiling)
        # 0 at a coin flip, 1 by ~85% confidence.
        span = max(0.0, min(1.0, (confidence - 0.5) / 0.35))
        scaled = floor + (ceiling - floor) * span
        return round(min(scaled, ceiling), 2)

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "breaker_active": self.breaker_active,
            "breaker_reason": self.breaker_reason,
            "breaker_seconds_remaining": round(self.breaker_seconds_remaining(), 0),
            "breaker_drawdown": round(self.breaker_drawdown, 2),
            "halt_above_bps": self.halt_above_bps,
            "resume_below_bps": self.resume_below_bps,
            "regimes": {
                asset: {
                    "halted": r.halted,
                    "vol_bps": round(r.vol_bps, 1) if r.vol_bps is not None else None,
                }
                for asset, r in self.regimes.items()
            },
        }
