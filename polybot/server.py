"""FastAPI application: REST control plane, SSE telemetry, static dashboard.

The server binds to loopback by default and, there, authenticates nobody. Set
``POLYBOT_PASSWORD`` to put every route behind HTTP Basic; the bind guard in
``run.py`` requires it before it will serve a routable interface, because
otherwise the exposed surface is engine control over a bot holding wallet
credentials.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import vault
from .engine import Engine
from .settings import (
    ROOT,
    load_config,
    save_operator_limits,
    save_runtime_limits,
)
from .store import Store

WEB_DIR = ROOT / "web"

RANGE_SECONDS: dict[str, float | None] = {
    "1h": 3600.0,
    "5h": 5 * 3600.0,
    "12h": 12 * 3600.0,
    "24h": 86400.0,
    "7d": 7 * 86400.0,
    "all": None,
}


def create_app() -> FastAPI:
    vault.load_into_env()
    config = load_config()
    store = Store()
    store.rebaseline_taker_fees(
        float(config["fees"].get("taker_rate", 0.07)),
        float(config["fees"].get("exponent", 1.0)),
    )
    engine = Engine(config, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Autostart makes the dashboard useful the moment it loads: paper
        # trading begins on its own, which is what the operator wants in every
        # case except an explicit live session.
        #
        # Boot is always Paper, and deliberately does not consult
        # ``autostart_mode`` or the live gate. Switching to Live is an
        # operator action taken against a running process; a restart must
        # never make it for them.
        autostart = config["engine"].get("autostart", True)
        if os.getenv("POLYBOT_AUTOSTART", "").strip() == "0":
            autostart = False
        if autostart:
            await engine.start("paper")
        try:
            yield
        finally:
            await engine.stop()
            store.close()

    app = FastAPI(title="Polybot 3.0", version="3.0.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.store = store
    app.state.config = config

    # ---- auth -----------------------------------------------------------

    password = os.getenv("POLYBOT_PASSWORD", "").strip()

    # Unauthenticated endpoints. The platform healthcheck has to answer before
    # a deploy is routed anywhere, so it cannot sit behind the password; it
    # reports liveness only and exposes no position, credential or P/L data.
    AUTH_EXEMPT = {"/healthz"}

    def _password_ok(header: str | None) -> bool:
        """Check an HTTP Basic header. The username is ignored."""
        if not header or not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        _, sep, supplied = decoded.partition(":")
        if not sep:
            return False
        return secrets.compare_digest(supplied, password)

    @app.middleware("http")
    async def require_password(request: Request, call_next):
        """Gate every route behind a shared password when one is configured.

        Nothing else in this process authenticates, and the dashboard can start
        the engine, move every risk limit and read the credential vault. On
        loopback that is acceptable; on a routable interface it is a remote
        control for a funded bot, so ``run.py`` refuses such a bind unless this
        password is set.

        Basic auth specifically, because the browser then attaches the
        credential to fetch and EventSource itself — the SSE telemetry stream
        cannot carry a custom header, and a cookie/login page would need one.
        Set no password and the gate disappears, preserving local behaviour.
        """
        if not password or request.url.path in AUTH_EXEMPT:
            return await call_next(request)
        if _password_ok(request.headers.get("authorization")):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required."},
            headers={"WWW-Authenticate": 'Basic realm="Polybot", charset="UTF-8"'},
        )

    # ---- state ---------------------------------------------------------

    def resolve_strategy(requested: str | None) -> str | None:
        """Normalise a strategy filter to a name the store will recognise.

        Anything unknown — including the explicit "all" — becomes ``None``,
        which every store read treats as "no filter". A stale selection left in
        a browser tab therefore degrades to the combined view rather than
        silently rendering an empty dashboard.
        """
        if not requested or requested == "all":
            return None
        known = {row["strategy"] for row in store.strategies_seen(engine.runtime.mode)}
        known.update(engine.configured_strategies())
        return requested if requested in known else None

    def build_state(
        pnl_range: str = "all", strategy: str | None = None
    ) -> dict[str, Any]:
        mode = engine.runtime.mode
        snap = engine.snapshot()
        window = RANGE_SECONDS.get(pnl_range, None)
        since = (time.time() - window) if window else None
        strategy = resolve_strategy(strategy)

        # Every settled-performance panel is derived from the same (mode,
        # strategy, since) triple, so the headline number, the curve and the
        # breakdown below it can never describe different sets of trades.
        return {
            "ts": time.time(),
            "mode": mode,
            "strategy_filter": strategy or "all",
            "strategies_seen": store.strategies_seen(mode),
            "strategy_selection": snap["strategy_selection"],
            "runtime": snap["runtime"],
            "stats": store.stats(mode, None, strategy),
            "engine": snap["engine"],
            "risk": snap["risk"],
            "markets": snap["markets"],
            "live_availability": snap["live_availability"],
            "live_market_selection": snap["live_market_selection"],
            "bankroll": snap["bankroll"],
            "performance_targets": snap["performance_targets"],
            "strategy": snap["strategy"],
            "strategies": snap.get("strategies", {}),
            "spot": engine.spot.snapshot(),
            "sources": {
                **engine.spot.source_health,
                **engine.poly.health,
                "chainlink_streams": engine.chainlink.health,
            },
            "chainlink": engine.chainlink.snapshot(),
            "positions": engine.open_positions_view(strategy),
            "equity": store.equity_curve(mode, since, strategy=strategy),
            "pnl_range": pnl_range,
            # Scoped to the selected range so the headline figure and the curve
            # below it always describe the same set of settlements.
            "range_stats": store.stats(mode, since, strategy),
            "config": {
                "assets": engine.assets,
                "windows": engine.windows,
                "excluded_markets": config.get("excluded_markets", []),
                "bankroll": config.get("bankroll", {}),
                "performance_targets": config.get("performance_targets", {}),
                "live_market_selection": config["live_market_selection"],
                "sizing": config["sizing"],
                "limits": config["limits"],
                "strategy_config": config["strategy"],
                "risk": config["risk"],
                "model_trust": config["model_trust"],
            },
            "vault": vault.status().__dict__,
        }

    @app.get("/api/state")
    async def get_state(
        range: str = "all", strategy: str | None = None
    ) -> dict[str, Any]:
        return build_state(range if range in RANGE_SECONDS else "all", strategy)

    @app.get("/api/stream")
    async def stream(
        request: Request, range: str = "all", strategy: str | None = None
    ) -> StreamingResponse:
        """Server-sent events. Chosen over WebSockets because the data flows one
        way and SSE reconnects on its own when the engine restarts."""
        pnl_range = range if range in RANGE_SECONDS else "all"

        async def gen():
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = json.dumps(build_state(pnl_range, strategy), default=str)
                    yield f"data: {payload}\n\n"
                except Exception as exc:
                    yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    # ---- engine control -------------------------------------------------

    @app.post("/api/engine/start")
    async def start_engine(body: dict = Body(default={})) -> dict[str, Any]:
        result = await engine.start(body.get("mode"))
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "could not start"))
        return result

    @app.post("/api/engine/stop")
    async def stop_engine() -> dict[str, Any]:
        return await engine.stop()

    @app.post("/api/engine/pause")
    async def pause_engine() -> dict[str, Any]:
        return engine.pause()

    @app.post("/api/engine/resume")
    async def resume_engine() -> dict[str, Any]:
        return engine.resume()

    @app.post("/api/engine/panic")
    async def panic_engine() -> dict[str, Any]:
        return await engine.panic()

    @app.post("/api/engine/mode")
    async def set_mode(body: dict = Body(...)) -> dict[str, Any]:
        mode = str(body.get("mode", "")).lower()
        if mode not in {"paper", "live"}:
            raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")

        if mode == "live":
            availability = engine.live.availability()
            if not availability["ready"]:
                raise HTTPException(
                    status_code=400,
                    detail="Live mode unavailable: " + "; ".join(availability["blockers"]) + ".",
                )

        was_running = engine.runtime.running
        was_paused = engine.runtime.paused
        if was_running:
            await engine.stop()
        # Execution mode changes the broker, not the strategy or its risk
        # budget. Retain the exact active dashboard limits across the switch.
        engine.runtime.update(mode=mode)
        if was_running:
            await engine.start(mode)
            if was_paused:
                engine.pause()
        return {
            "ok": True,
            "mode": mode,
            "paused": engine.runtime.paused,
            "daily_cap_usd": engine.runtime.daily_cap_usd,
        }

    @app.post("/api/limits")
    async def set_limits(body: dict = Body(...)) -> dict[str, Any]:
        venue_minimum = float(
            config["risk"]["trade_floor"]["min_trade_size_usd"]
        )
        specs = (
            ("max_per_trade_usd", venue_minimum, 1000.0),
            ("max_per_market_window_usd", venue_minimum, 5000.0),
            ("max_per_window_usd", venue_minimum, 5000.0),
            ("daily_cap_usd", venue_minimum, 100000.0),
            ("starting_balance_usd", venue_minimum, 1000000.0),
            ("max_open_exposure_usd", venue_minimum, 1000000.0),
            ("max_daily_loss_usd", 1.0, 1000000.0),
            # Strategy entry bounds. The floor is 0.0 rather than the old 0.62
            # because the cloned wallet's median entry is a 0.54 coin flip and
            # its edge comes from price, not from a confidence threshold.
            ("min_confidence", 0.0, 0.99),
            ("max_entry_price", 0.01, 0.99),
            ("max_window_fraction", 0.05, 1.0),
        )
        updates: dict[str, float] = {}
        for field, lo, hi in specs:
            if field in body:
                try:
                    value = float(body[field])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{field} must be a number")
                if not (lo <= value <= hi):
                    raise HTTPException(status_code=400, detail=f"{field} must be between {lo} and {hi}")
                updates[field] = round(value, 2)
        if not updates:
            raise HTTPException(status_code=400, detail="no recognised limit fields")

        bankroll = config["bankroll"]
        limits = config["limits"]
        strategy_cfg = config["strategy"]
        runtime = engine.runtime.snapshot()
        candidate = {
            "max_per_trade_usd": runtime["max_per_trade_usd"],
            "max_per_market_window_usd": float(
                limits.get(
                    "hard_market_window_cap_usd",
                    limits["hard_window_cap_usd"],
                )
            ),
            "max_per_window_usd": float(limits["hard_window_cap_usd"]),
            "daily_cap_usd": float(bankroll["max_daily_turnover_usd"]),
            "starting_balance_usd": float(bankroll["starting_balance_usd"]),
            "max_open_exposure_usd": float(bankroll["max_open_exposure_usd"]),
            "max_daily_loss_usd": float(bankroll["max_daily_loss_usd"]),
            "min_confidence": float(strategy_cfg["min_confidence"]),
            "max_entry_price": float(strategy_cfg["max_entry_price"]),
            "max_window_fraction": float(strategy_cfg["max_entry_window_fraction"]),
        }
        candidate.update(updates)

        if candidate["max_per_trade_usd"] > candidate["max_per_market_window_usd"]:
            raise HTTPException(
                status_code=400,
                detail="Maximum per trade cannot exceed the per-market/window cap.",
            )
        if candidate["max_per_market_window_usd"] > candidate["max_per_window_usd"]:
            raise HTTPException(
                status_code=400,
                detail="Per-market/window cap cannot exceed the shared interval cap.",
            )
        if candidate["max_per_trade_usd"] > candidate["max_per_window_usd"]:
            raise HTTPException(
                status_code=400,
                detail="Maximum per trade cannot exceed the shared interval cap.",
            )
        if candidate["max_per_window_usd"] > candidate["max_open_exposure_usd"]:
            raise HTTPException(
                status_code=400,
                detail="Shared interval cap cannot exceed total open exposure.",
            )
        if candidate["max_open_exposure_usd"] > candidate["starting_balance_usd"]:
            raise HTTPException(
                status_code=400,
                detail="Total open exposure cannot exceed the declared bankroll.",
            )
        if candidate["max_daily_loss_usd"] > candidate["starting_balance_usd"]:
            raise HTTPException(
                status_code=400,
                detail="Daily loss stop cannot exceed the declared bankroll.",
            )
        if candidate["daily_cap_usd"] < candidate["max_per_trade_usd"]:
            raise HTTPException(
                status_code=400,
                detail="Daily turnover must allow at least one trade.",
            )

        runtime_updates = {
            field: round(candidate[field], 2)
            for field in (
                "max_per_trade_usd", "max_per_window_usd", "daily_cap_usd"
            )
        }
        engine.runtime.update(**runtime_updates)
        # Paper and Live intentionally share the exact same limits.
        for mode in ("paper", "live"):
            save_runtime_limits(mode, runtime_updates)

        config["sizing"].update(
            max_clip_usd=runtime_updates["max_per_trade_usd"],
            max_per_window_usd=runtime_updates["max_per_window_usd"],
            daily_cap_usd=runtime_updates["daily_cap_usd"],
            paper_daily_cap_usd=runtime_updates["daily_cap_usd"],
        )
        limits.update(
            hard_market_window_cap_usd=round(
                candidate["max_per_market_window_usd"], 2
            ),
            hard_window_cap_usd=runtime_updates["max_per_window_usd"],
        )
        strategy_cfg.update(
            min_confidence=round(candidate["min_confidence"], 4),
            max_entry_price=round(candidate["max_entry_price"], 4),
            max_entry_window_fraction=round(candidate["max_window_fraction"], 4),
        )
        bankroll.update(
            starting_balance_usd=round(candidate["starting_balance_usd"], 2),
            max_open_exposure_usd=round(candidate["max_open_exposure_usd"], 2),
            cash_reserve_usd=round(
                candidate["starting_balance_usd"]
                - candidate["max_open_exposure_usd"],
                2,
            ),
            max_daily_loss_usd=round(candidate["max_daily_loss_usd"], 2),
            max_daily_turnover_usd=runtime_updates["daily_cap_usd"],
        )

        # The strategy caches these at construction; update the live instance
        # so the next scan uses the dashboard values without a restart.
        engine.antsaslyku.min_confidence = float(strategy_cfg["min_confidence"])
        engine.antsaslyku.max_entry_price = float(strategy_cfg["max_entry_price"])
        engine.antsaslyku.max_entry_window_fraction = float(
            strategy_cfg["max_entry_window_fraction"]
        )

        save_operator_limits({
            "sizing": {
                "max_clip_usd": config["sizing"]["max_clip_usd"],
                "max_per_window_usd": config["sizing"]["max_per_window_usd"],
                "daily_cap_usd": config["sizing"]["daily_cap_usd"],
                "paper_daily_cap_usd": config["sizing"]["paper_daily_cap_usd"],
            },
            "limits": {
                "hard_market_window_cap_usd": limits[
                    "hard_market_window_cap_usd"
                ],
                "hard_window_cap_usd": limits["hard_window_cap_usd"],
            },
            "strategy": {
                "min_confidence": strategy_cfg["min_confidence"],
                "max_entry_price": strategy_cfg["max_entry_price"],
                "max_entry_window_fraction": strategy_cfg[
                    "max_entry_window_fraction"
                ],
            },
            "bankroll": {
                "starting_balance_usd": bankroll["starting_balance_usd"],
                "max_open_exposure_usd": bankroll["max_open_exposure_usd"],
                "cash_reserve_usd": bankroll["cash_reserve_usd"],
                "max_daily_loss_usd": bankroll["max_daily_loss_usd"],
                "max_daily_turnover_usd": bankroll["max_daily_turnover_usd"],
            },
        })
        return {"ok": True, "persisted": True, **candidate}

    @app.post("/api/risk/daily-loss-bypass")
    async def bypass_daily_loss(body: dict = Body(default={})) -> dict[str, Any]:
        """Bypass the daily loss stop in Paper or Live, for an hour or a day."""
        scope = str(body.get("scope") or "hour").lower()
        if scope not in {"hour", "day"}:
            raise HTTPException(
                status_code=400, detail="scope must be 'hour' or 'day'"
            )
        return engine.bypass_daily_loss_stop(scope=scope)

    @app.post("/api/strategies/{name}")
    async def set_strategy_module(
        name: str, body: dict = Body(default={})
    ) -> dict[str, Any]:
        """Tune the running strategy at runtime.

        This is how the ladder and the hedge are switched between the literal
        clone and the measured-profitable subset without a restart — see the
        `_hedge_note` and `_ladder_note` blocks in config.json for what each
        setting is worth. ``dry_run: true`` keeps the strategy scoring and
        logging while never reaching a broker, which is how a parameter change
        should be forward-tested before it is allowed near real money.
        """
        module = getattr(engine, name.replace("-", "_"), None)
        if module is None or not hasattr(module, "NAME") or module.NAME != name:
            raise HTTPException(status_code=404, detail=f"no strategy module '{name}'")

        if "dry_run" in body:
            module.dry_run = bool(body["dry_run"])
        if "risk_multiplier" in body:
            try:
                multiplier = float(body["risk_multiplier"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="risk_multiplier must be a number")
            if not 0.0 < multiplier <= 5.0:
                raise HTTPException(
                    status_code=400, detail="risk_multiplier must be in (0, 5]"
                )
            module.risk_multiplier = multiplier
        for key in ("hedge_enabled", "add_on_enabled"):
            if key in body:
                setattr(module, key, bool(body[key]))
        for key in (
            "max_capital_usd", "max_fills_per_window", "max_open_windows",
            "max_cost_per_window_usd", "hedge_max_combined_cost",
            "min_entry_price", "max_entry_price", "base_clip_usd",
        ):
            if key in body:
                setattr(module, key, type(getattr(module, key))(body[key]))

        # In-memory only. A restart returns the strategy to its config.json
        # defaults, so nothing set here can silently outlive the session.
        store.log(
            engine.runtime.mode,
            "strategy",
            f"{name}: dry_run={module.dry_run} "
            f"risk_multiplier={module.risk_multiplier} "
            f"max_fills={module.max_fills_per_window} "
            f"hedge={module.hedge_enabled}",
        )
        return {"ok": True, "persisted": False, **module.snapshot()}

    @app.post("/api/reset")
    async def reset(body: dict = Body(default={})) -> dict[str, Any]:
        mode = str(body.get("mode") or engine.runtime.mode).lower()
        if mode not in {"paper", "live"}:
            raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
        # Guard rail: live history is a record of real money and is not
        # something a stray click should be able to erase.
        if mode == "live" and body.get("confirm") != "DELETE LIVE HISTORY":
            raise HTTPException(
                status_code=400,
                detail="Refusing to wipe live history without confirm='DELETE LIVE HISTORY'.",
            )
        # Optional: restart one paper cohort without discarding the other
        # strategy's comparison history.
        strategy = resolve_strategy(body.get("strategy"))
        store.reset(mode, strategy)
        if strategy is None:
            engine.stats.signal_scans = 0
            engine.stats.entries = 0
            engine.stats.rejections.clear()
            # The strategy holds its own in-memory ladder bookkeeping, which
            # backs max_capital_usd and max_open_windows. Wiping the ledger
            # without clearing it leaves the dashboard reporting capital at
            # work against an empty ledger, and leaves those budgets consumed
            # by windows that no longer exist.
            engine.antsaslyku.windows_state.clear()
            engine.antsaslyku.stats.update(
                scans=0, intents=0, fills=0, cost_usd=0.0
            )
            engine.antsaslyku.stats["rejections"] = {}
            engine.antsaslyku.stats["by_stage"] = {}
        return {"ok": True, "mode": mode, "strategy": strategy or "all"}

    # ---- data -----------------------------------------------------------

    @app.get("/api/strategies")
    async def list_strategies() -> dict[str, Any]:
        """What the selector should offer, and what is trading right now."""
        return {
            **engine.strategy_status(),
            "seen": store.strategies_seen(engine.runtime.mode),
        }

    @app.post("/api/strategy")
    async def select_strategy(body: dict = Body(default={})) -> dict[str, Any]:
        """Choose the single strategy Live is allowed to trade.

        Paper ignores the selection for execution purposes — it runs every
        configured strategy so the curves are comparable — but the dashboard
        uses the same name as its display filter.
        """
        name = str(body.get("strategy") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="strategy is required")
        result = engine.select_strategy(name)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error"))
        return {**result, **engine.strategy_status()}

    @app.get("/api/positions")
    async def positions(strategy: str | None = None) -> dict[str, Any]:
        return {"positions": engine.open_positions_view(resolve_strategy(strategy))}

    @app.get("/api/activity")
    async def activity(limit: int = 200) -> dict[str, Any]:
        return {"activity": store.activity(engine.runtime.mode, min(max(limit, 1), 1000))}

    @app.get("/api/settlements")
    async def settlements(
        limit: int = 200, range: str = "all", strategy: str | None = None
    ) -> dict[str, Any]:
        range_name = range if range in RANGE_SECONDS else "all"
        window = RANGE_SECONDS[range_name]
        since = (time.time() - window) if window else None
        picked = resolve_strategy(strategy)
        return {
            "settlements": store.settlements(
                engine.runtime.mode,
                min(max(limit, 1), 1000),
                since,
                picked,
            ),
            "summary": store.win_loss_summary(engine.runtime.mode, since, picked),
            "range": range_name,
            "strategy": picked or "all",
        }

    @app.get("/api/by-asset")
    async def by_asset(range: str = "all", strategy: str | None = None) -> dict[str, Any]:
        range_name = range if range in RANGE_SECONDS else "all"
        window = RANGE_SECONDS[range_name]
        since = (time.time() - window) if window else None
        picked = resolve_strategy(strategy)
        return {
            "rows": store.by_asset(engine.runtime.mode, since, picked),
            "range": range_name,
            "strategy": picked or "all",
        }

    @app.get("/api/signals")
    async def signals() -> dict[str, Any]:
        engine.prune_signals()
        return {"signals": list(engine.latest_signals.values())}

    @app.get("/api/equity")
    async def equity(range: str = "all", strategy: str | None = None) -> dict[str, Any]:
        window = RANGE_SECONDS.get(range if range in RANGE_SECONDS else "all")
        since = (time.time() - window) if window else None
        picked = resolve_strategy(strategy)
        return {
            "equity": store.equity_curve(
                engine.runtime.mode, since, strategy=picked
            ),
            "range": range,
            "strategy": picked or "all",
        }

    # ---- vault ----------------------------------------------------------

    @app.get("/api/vault")
    async def get_vault() -> dict[str, Any]:
        status = vault.status().__dict__
        status["availability"] = engine.live.availability()
        return status

    @app.post("/api/vault")
    async def save_vault(body: dict = Body(...)) -> dict[str, Any]:
        """Validate and store credentials.

        The response is deliberately free of secrets — only a masked view and
        the derived address go back to the browser.
        """
        try:
            bundle = vault.validate_bundle(
                private_key=body.get("private_key", ""),
                wallet_address=body.get("wallet_address", ""),
                api_key=body.get("api_key", ""),
                api_secret=body.get("api_secret", ""),
                api_passphrase=body.get("api_passphrase", ""),
                funder_address=body.get("funder_address", ""),
                signature_type=int(body.get("signature_type", 0) or 0),
            )
        except vault.VaultError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid input: {exc}")

        report: dict[str, Any] = {"checked": False, "ok": False, "reason": None}
        if body.get("verify_online", True):
            report = await asyncio.to_thread(vault.check_clob_credentials, bundle)
            if report.get("checked") and not report.get("ok"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Polymarket rejected these credentials: {report.get('reason')}",
                )
            if report.get("ok"):
                bundle["clob_version"] = "2"

        vault.save(bundle)
        engine.live.disconnect()  # force a reconnect with the new credentials

        return {
            "ok": True,
            "address": bundle["address"],
            "funder": bundle["funder_address"],
            "derived_api_key": report.get("derived_api_key"),
            "verified": bool(report.get("ok")),
            "status": vault.status().__dict__,
        }

    @app.post("/api/vault/clob/refresh")
    async def refresh_clob_credentials() -> dict[str, Any]:
        """Explicitly replace legacy L2 credentials with a CLOB V2 set."""
        try:
            result = await asyncio.to_thread(vault.refresh_clob_credentials)
        except vault.VaultError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        engine.live.disconnect()
        return {**result, "status": vault.status().__dict__}

    @app.delete("/api/vault")
    async def clear_vault() -> dict[str, Any]:
        vault.clear()
        engine.live.disconnect()
        if engine.runtime.mode == "live":
            engine.runtime.update(mode="paper", paused=True)
        return {"ok": True, "status": vault.status().__dict__}

    @app.post("/api/vault/chainlink")
    async def save_chainlink(body: dict = Body(...)) -> dict[str, Any]:
        """Store Chainlink Data Streams credentials and reconnect the feed."""
        try:
            result = vault.save_chainlink(
                body.get("api_key", ""), body.get("api_secret", "")
            )
        except vault.VaultError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Verify against the live API before claiming success.
        # Credentials can be added after the engine started unconfigured, in
        # which case no Chainlink HTTP client exists yet. Reconnect cleanly so
        # runtime credential changes work and stale polling tasks are removed.
        await engine.chainlink.stop()
        connected = (
            await engine.chainlink.start()
            if engine.runtime.running
            else await engine.chainlink.discover_feeds()
        )
        if not connected:
            raise HTTPException(
                status_code=400,
                detail=f"Chainlink rejected those credentials: "
                       f"{engine.chainlink.health.get('error')}",
            )
        return {**result, "feeds": engine.chainlink.feeds,
                "feed_count": len(engine.chainlink.feeds)}

    @app.delete("/api/vault/chainlink")
    async def clear_chainlink() -> dict[str, Any]:
        vault.clear_chainlink()
        await engine.chainlink.stop()
        engine.chainlink.feeds.clear()
        engine.chainlink.health.update(active=False, configured=False,
                                       error="cleared", feeds=0)
        return {"ok": True}

    @app.get("/api/chainlink")
    async def chainlink_state() -> dict[str, Any]:
        return engine.chainlink.snapshot()

    @app.get("/api/balance")
    async def balance() -> dict[str, Any]:
        availability = engine.live.availability()
        if not availability["ready"]:
            return {"ok": False, "error": "; ".join(availability["blockers"])}
        return await asyncio.to_thread(engine.live.balance)

    # ---- static ---------------------------------------------------------

    @app.middleware("http")
    async def no_cache_assets(request: Request, call_next):
        """Serve the dashboard uncached.

        The browser will otherwise hold a stale index.html or app.js from
        memory cache and revalidate lazily, so a UI edit silently appears not
        to have taken effect. There is no CDN and no bandwidth concern on
        loopback, so correctness beats caching here.
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "running": engine.runtime.running, "mode": engine.runtime.mode}

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})

    return app


app = create_app()
