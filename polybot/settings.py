"""Configuration loading and mutable runtime settings.

Two layers:

* ``config.json`` on disk — the durable defaults, mirroring the schema used by
  the upstream HarrierOnChain/Polymarket repo (venue, enable_trading, risk
  blocks with circuit_breaker / depth_guard / trade_floor).
* :class:`Runtime` — the knobs the dashboard mutates live (clip size, pause
  state, execution mode). Exposure limits are persisted to ``data/runtime.json``
  and restored on start, so a slider you set stays set across restarts.
  Transient state (running, paused) is deliberately *not* persisted: an engine
  should never come back up mid-session believing it was already trading.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

# Both paths are overridable so a container deploy can point them at a mounted
# volume. A container's own filesystem is rebuilt on every deploy, which would
# otherwise discard the SQLite ledger and the saved credentials on each push.
DATA_DIR = Path(os.getenv("POLYBOT_DATA_DIR") or (ROOT / "data"))
ENV_PATH = Path(os.getenv("POLYBOT_ENV_FILE") or (ROOT / ".env"))
OPERATOR_LIMITS_PATH = DATA_DIR / "operator_limits.json"

# Assets Polymarket currently runs Up-or-Down windows on, mapped to the ticker
# each spot venue uses. `None` means that venue does not list the asset and is
# skipped rather than treated as a failure.
ASSET_SPEC: dict[str, dict[str, Any]] = {
    "btc": {"label": "BTC", "coinbase": "BTC-USD", "kraken": "XXBTZUSD", "kraken_req": "XBTUSD", "decimals": 2},
    "eth": {"label": "ETH", "coinbase": "ETH-USD", "kraken": "XETHZUSD", "kraken_req": "ETHUSD", "decimals": 2},
    "sol": {"label": "SOL", "coinbase": "SOL-USD", "kraken": "SOLUSD", "kraken_req": "SOLUSD", "decimals": 3},
    "xrp": {"label": "XRP", "coinbase": "XRP-USD", "kraken": "XRPUSD", "kraken_req": "XRPUSD", "decimals": 4},
    "bnb": {"label": "BNB", "coinbase": None, "kraken": "BNBUSD", "kraken_req": "BNBUSD", "decimals": 2},
    "doge": {"label": "DOGE", "coinbase": "DOGE-USD", "kraken": "XDGUSD", "kraken_req": "XDGUSD", "decimals": 5},
    "hype": {"label": "HYPE", "coinbase": None, "kraken": "HYPEUSD", "kraken_req": "HYPEUSD", "decimals": 3},
}

DEFAULT_CONFIG: dict[str, Any] = {
    "venue": "polymarket",
    "enable_trading": False,
    "engine": {
        "poll_interval_seconds": 3.0,
        "discovery_interval_seconds": 20.0,
        "spot_interval_seconds": 1.0,
        "autostart": True,
        "autostart_mode": "paper",
    },
    "assets": ["btc", "sol", "xrp", "eth"],
    "windows": ["5m", "15m"],
    "excluded_markets": [
        {"asset": "sol", "window": "5m", "reason": "Disabled by operator"},
    ],
    "strategy": "directional",
    "bankroll": {
        "activated_at": 0.0,
        "starting_balance_usd": 150.0,
        "max_open_exposure_usd": 60.0,
        "cash_reserve_usd": 90.0,
        "max_daily_loss_usd": 15.0,
        "max_daily_turnover_usd": 300.0,
    },
    "live_market_selection": {
        "enabled": False,
        "min_paper_settlements": 100,
        "min_paper_roi": 0.10,
        "max_markets": 5,
    },
    "maker": {
        "min_lock_cents": 2.0,
        "base_clip_usd": 5.0,
        "max_pairs_per_window": 40,
        "min_price": 0.03,
        "max_price": 0.97,
        "min_seconds_remaining": 45,
        "skew_strength": 0.35,
        "improve_ticks": 0,
        "residual_flatten": True,
        "target_rewards": False,
        "max_quote_drift_cents": 2.0,
    },
    "sizing": {
        "base_clip_usd": 5.0,
        "max_clip_usd": 5.0,
        "max_per_window_usd": 25.0,
        "daily_cap_usd": 300.0,
        "paper_daily_cap_usd": 300.0,
        "calibration": 0.55,
        "profit_lock_usd": 5000.0,
    },
    "entry": {
        "confidence_only": False,
        "profit_tuned": False,
        "hard_market_window_cap_usd": 25.0,
        "hard_window_cap_usd": 25.0,
        "min_confidence": 0.62,
        "min_expected_roi": 0.04,
        "max_entries_per_window": 2,
        "require_evidence_qualified_market": False,
        "paper_experimental_min_confidence": 0.90,
        "asset_min_confidence": {},
        "paper_experimental_markets": [],
        "max_entry_price": 0.80,
        "min_entry_price": 0.30,
        "min_seconds_remaining": 25,
        "max_window_fraction": 0.75,
        "reversal_enabled": False,
        "reversal_live_enabled": False,
        "reversal_min_confidence": 0.72,
        "reversal_min_expected_roi": 0.04,
        "reversal_confirmation_seconds": 6.0,
        "reversal_min_observations": 3,
        "max_side_switches": 1,
    },
    "risk": {
        "circuit_breaker": {
            "max_consecutive_large_trades": 5,
            "window_seconds": 60,
            "cooldown_seconds": 300,
        },
        "drawdown_breaker": {
            "lookback_seconds": 3600,
            "max_loss_usd": 100.0,
            "cooldown_seconds": 900,
        },
        "depth_guard": {"min_orderbook_depth_usd": 50.0},
        "trade_floor": {"min_trade_size_usd": 5.0},
        "volatility_regime": {
            "halt_above_bps": 60.0,
            "resume_below_bps": 50.0,
            "lookback_seconds": 900,
        },
    },
    "fees": {
        "taker_rate": 0.07,
        "exponent": 1.0,
        "maker_rate": 0.0,
        "assumed_slippage_ticks": 1,
    },
    "model_trust": {"min_test_windows": 300},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge ``override`` onto ``base``, recursing into nested dicts."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_operator_limits() -> dict[str, Any]:
    """Read dashboard-managed strategy limits, ignoring a corrupt file."""
    try:
        value = json.loads(OPERATOR_LIMITS_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_operator_limits(overrides: dict[str, Any]) -> None:
    """Merge and atomically persist limits changed from the dashboard."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = load_operator_limits()
    merged = _deep_merge(current, overrides)
    tmp = OPERATOR_LIMITS_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        tmp.replace(OPERATOR_LIMITS_PATH)
    except OSError:
        pass


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load config.json layered over the built-in defaults.

    A missing or malformed file falls back to defaults rather than refusing to
    start — the dashboard is more useful running with defaults than not at all.
    """
    use_operator_limits = path is None
    path = path or CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (json.JSONDecodeError, OSError):
        raw = {}
    merged = _deep_merge(DEFAULT_CONFIG, raw)
    if use_operator_limits:
        merged = _deep_merge(merged, load_operator_limits())
    return merged


@dataclass
class Runtime:
    """Live-mutable engine state, guarded by a lock for cross-thread reads."""

    running: bool = False
    paused: bool = False
    mode: str = "paper"  # "paper" | "live"

    max_per_trade_usd: float = 5.0
    max_per_window_usd: float = 25.0
    daily_cap_usd: float = 100.0

    # Set when the drawdown breaker or the volatility regime halts entries.
    halt_reason: str | None = None
    halt_until: float = 0.0

    started_at: float | None = None
    last_error: str | None = None

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Runtime":
        """Build runtime state, preferring persisted slider values."""
        sizing = cfg["sizing"]
        # A fresh process always comes up in Paper, whatever ``autostart_mode``
        # says. Live is a decision an operator takes at the dashboard against a
        # process they are watching — never a state a restart can restore on
        # its own. A hosted deploy restarts on every push and on every crash,
        # so any config-driven path to booting Live is a way to start placing
        # real orders on a box nobody is looking at.
        mode = "paper"

        runtime = cls(
            mode=mode,
            max_per_trade_usd=float(sizing["max_clip_usd"]),
            max_per_window_usd=float(sizing["max_per_window_usd"]),
            daily_cap_usd=daily_cap_for(cfg, mode),
        )

        saved = load_runtime_limits()
        # Dashboard risk settings are shared by Paper and Live. Legacy builds
        # saved per-mode values, so accept them only when they agree (or when
        # just one ledger exists); otherwise the dashboard-backed config is the
        # unambiguous source of truth.
        paper_limits = saved.get("paper")
        live_limits = saved.get("live")
        if isinstance(paper_limits, dict) and isinstance(live_limits, dict):
            limits = paper_limits if paper_limits == live_limits else {}
        elif isinstance(paper_limits, dict):
            limits = paper_limits
        elif isinstance(live_limits, dict):
            limits = live_limits
        else:
            limits = {}
        venue_minimum = float(cfg["risk"]["trade_floor"]["min_trade_size_usd"])
        for field_name in ("max_per_trade_usd", "max_per_window_usd", "daily_cap_usd"):
            value = limits.get(field_name)
            if isinstance(value, (int, float)) and value > 0:
                minimum = venue_minimum if field_name != "daily_cap_usd" else 1.0
                setattr(runtime, field_name, max(minimum, float(value)))
        return runtime

    def persist_limits(self) -> None:
        """Write the one shared limit set to both legacy mode slots."""
        with self._lock:
            limits = {
                "max_per_trade_usd": self.max_per_trade_usd,
                "max_per_window_usd": self.max_per_window_usd,
                "daily_cap_usd": self.daily_cap_usd,
            }
            for mode in ("paper", "live"):
                save_runtime_limits(mode, limits)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "paused": self.paused,
                "mode": self.mode,
                "max_per_trade_usd": self.max_per_trade_usd,
                "max_per_window_usd": self.max_per_window_usd,
                "daily_cap_usd": self.daily_cap_usd,
                "halt_reason": self.halt_reason,
                "halt_until": self.halt_until,
                "started_at": self.started_at,
                "last_error": self.last_error,
            }

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self, key):
                    setattr(self, key, value)


RUNTIME_STATE_PATH = DATA_DIR / "runtime.json"


def load_runtime_limits() -> dict[str, Any]:
    """Read persisted slider values. A corrupt file is ignored, not fatal."""
    try:
        return json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_runtime_limits(mode: str, limits: dict[str, float]) -> None:
    """Persist slider values for one execution mode.

    Written atomically via a temp file and replace, so a crash mid-write cannot
    leave a truncated file that silently resets every limit on next start.
    """
    ensure_data_dir()
    current = load_runtime_limits()
    current[mode] = {k: round(float(v), 2) for k, v in limits.items()}
    tmp = RUNTIME_STATE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
        tmp.replace(RUNTIME_STATE_PATH)
    except OSError:
        pass


def daily_cap_for(cfg: dict[str, Any], mode: str) -> float:
    """Shared Paper/Live daily turnover ceiling."""
    return float(cfg["sizing"]["daily_cap_usd"])


def live_trading_enabled() -> bool:
    """Master switch for real-money orders.

    The project-local ``.env`` is authoritative when it explicitly contains
    the switch. This avoids an inherited parent-process value silently
    overriding the operator's saved setting, and means changing the file takes
    effect without a restart.
    """
    value: str | None = None
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, candidate = stripped.partition("=")
            if key.strip() == "LIVE_TRADING_ENABLED":
                value = candidate.strip().strip('"').strip("'")
                break
    except OSError:
        pass

    if value is None:
        value = os.getenv("LIVE_TRADING_ENABLED", "0")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
