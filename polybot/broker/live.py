"""Live execution against the Polymarket CLOB.

⚠️  READ THIS BEFORE ENABLING.

This module signs and submits orders that spend real pUSD. It is written
against the official ``py-clob-client-v2`` (so EIP-712 order signing and L2 HMAC
auth are handled by Polymarket's own code rather than reimplemented here), but
**the code path in this build has never been executed against real funds.** Its
first live order is its first test.

Three independent locks must all be open before an order can go out:

1. ``LIVE_TRADING_ENABLED=1`` in the environment.
2. Credentials present in the vault.
3. The dashboard's execution mode set to ``live``.

Buys are sent FOK (fill-or-kill) from the same full-depth plan used by paper
execution. That prevents a partial real position that the paper broker would
have rejected. Emergency sells remain FAK so panic-close can liquidate whatever
the book will immediately absorb. Neither order type leaves a resting order.
"""

from __future__ import annotations

import math
import re
import threading
import time
from typing import Any

import httpx

from ..fees import taker_fee
from ..settings import live_trading_enabled
from ..vault import current as vault_current
from .paper import Fill, PaperBroker

CLOB_HOST = "https://clob.polymarket.com"
DATA_API_HOST = "https://data-api.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


class LiveTradingDisabled(RuntimeError):
    """Raised when a live order is attempted without every lock open."""


class LiveBroker:
    """Thin wrapper over ``py_clob_client_v2`` with hard safety gating."""

    mode = "live"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.taker_rate = float(config["fees"].get("taker_rate", 0.07))
        self.fee_exponent = float(config["fees"].get("exponent", 1.0))
        self.min_order_size = float(config["risk"]["trade_floor"]["min_trade_size_usd"])
        self._client: Any = None
        self._lock = threading.RLock()
        self._address: str | None = None
        self.last_error: str | None = None

    # -- gating ------------------------------------------------------------

    @staticmethod
    def availability() -> dict[str, Any]:
        """Report which locks are open, for display in the UI."""
        creds = vault_current()
        has_key = bool(creds.get("POLY_PRIVATE_KEY"))
        has_api = all(
            creds.get(k) for k in ("POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE")
        )
        clob_v2_verified = creds.get("POLY_CLOB_VERSION") == "2"
        enabled = live_trading_enabled()
        blockers: list[str] = []
        if not enabled:
            blockers.append("LIVE_TRADING_ENABLED is not set to 1")
        if not has_key:
            blockers.append("no private key in the vault")
        if not has_api:
            blockers.append("no complete CLOB V2 API credential set")
        if has_api and not clob_v2_verified:
            blockers.append("saved CLOB credentials have not been verified for V2; re-derive them")
        return {
            "env_enabled": enabled,
            "has_private_key": has_key,
            "has_api_creds": has_api,
            "clob_v2_verified": clob_v2_verified,
            "ready": enabled and has_key and has_api and clob_v2_verified,
            "blockers": blockers,
        }

    def _require_ready(self) -> None:
        state = self.availability()
        if not state["ready"]:
            raise LiveTradingDisabled(
                "Live trading is not enabled: " + "; ".join(state["blockers"]) + "."
            )

    # -- client ------------------------------------------------------------

    def connect(self) -> Any:
        """Build (once) an authenticated CLOB client."""
        self._require_ready()
        with self._lock:
            if self._client is not None:
                return self._client

            from py_clob_client_v2 import ApiCreds, ClobClient

            creds = vault_current()
            try:
                sig_type = int(creds.get("POLY_SIGNATURE_TYPE") or 0)
            except ValueError:
                sig_type = 0

            api_creds = ApiCreds(
                api_key=creds["POLY_API_KEY"],
                api_secret=creds["POLY_API_SECRET"],
                api_passphrase=creds["POLY_API_PASSPHRASE"],
            )
            client = ClobClient(
                host=CLOB_HOST,
                key=creds["POLY_PRIVATE_KEY"],
                chain_id=CHAIN_ID,
                creds=api_creds,
                signature_type=sig_type,
                funder=creds.get("POLY_FUNDER_ADDRESS") or creds.get("POLY_WALLET_ADDRESS") or None,
            )

            self._address = creds.get("POLY_WALLET_ADDRESS")
            self._client = client
            return client

    def disconnect(self) -> None:
        with self._lock:
            self._client = None

    # -- reads -------------------------------------------------------------

    def balance(self) -> dict[str, Any]:
        """Read venue cash, open-position value, and exchange allowance.

        The CLOB collateral response is spendable pUSD only.  Polymarket's
        portfolio figure also includes outcome-token positions, so treating
        collateral alone as account equity can understate the account while a
        market is still open.  The public Data API supplies that second value;
        failure there never hides a successful collateral read.
        """
        client = self.connect()
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        try:
            raw = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            # CLOB V2 reports pUSD in six-decimal base units and returns one
            # allowance per exchange spender.
            balance = _from_base_units(raw.get("balance", 0))
            raw_allowances = raw.get("allowances", raw.get("allowance", 0))
            allowances = _allowance_values(raw_allowances)
            allowance = min(allowances) if allowances else 0.0
            creds = vault_current()
            profile_address = (
                creds.get("POLY_FUNDER_ADDRESS")
                or creds.get("POLY_WALLET_ADDRESS")
                or ""
            )
            positions_value: float | None = None
            positions_error: str | None = None
            if re.fullmatch(r"0x[a-fA-F0-9]{40}", profile_address):
                try:
                    response = httpx.get(
                        f"{DATA_API_HOST}/value",
                        params={"user": profile_address},
                        timeout=10.0,
                        headers={"User-Agent": "Polybot/3.0"},
                    )
                    response.raise_for_status()
                    positions_value = _sum_position_value(response.json())
                except Exception as exc:
                    positions_error = f"position value unavailable: {exc}"[:180]

            return {
                "ok": True,
                "collateral": "pUSD",
                "balance_pusd": balance,
                "positions_value_pusd": positions_value,
                "account_equity_pusd": (
                    balance + positions_value
                    if positions_value is not None
                    else None
                ),
                "positions_error": positions_error,
                "allowance_pusd": allowance,
                "allowances_pusd": allowances,
                "approved": bool(allowances) and allowance > 0 and allowance >= balance,
            }
        except Exception as exc:
            self.last_error = str(exc)[:300]
            return {"ok": False, "error": self.last_error}

    # -- writes ------------------------------------------------------------

    def buy(
        self,
        market: Any,
        side: str,
        stake_usd: float,
        max_price: float | None = None,
    ) -> Fill | None:
        """Submit a full-fill buy using the exact paper execution plan.

        Returns a :class:`Fill` on success. Raises :class:`LiveTradingDisabled`
        if the safety locks are shut — callers must not treat that as a
        transient failure.
        """
        self._require_ready()

        if stake_usd < self.min_order_size:
            raise ValueError(
                f"Order ${stake_usd:.2f} is below the ${self.min_order_size:.2f} venue minimum."
            )

        tick = float(market.tick_size or 0.01)
        paper_planner = PaperBroker(self.config)
        plan = paper_planner.plan_buy(market, side, stake_usd, max_price)
        if plan is None:
            return None
        limit_price = _round_to_tick(plan.limit_price, tick)

        # CLOB V2 marketable buys require the maker (pUSD) amount to have at
        # most two decimals. Use the SDK's native market-order builder so it
        # derives the taker shares at the venue's tick-specific precision.
        stake_cents = plan.gross_usd
        requested_shares = plan.shares
        if stake_cents <= 0 or requested_shares <= 0:
            return None

        client = self.connect()
        from py_clob_client_v2 import (
            MarketOrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        args = MarketOrderArgs(
            token_id=market.token_for(side),
            amount=stake_cents,
            price=limit_price,
            side=Side.BUY,
            order_type=OrderType.FOK,
        )

        try:
            resp = client.create_and_post_market_order(
                order_args=args,
                options=PartialCreateOrderOptions(tick_size=_tick_string(tick)),
                order_type=OrderType.FOK,
            )
        except Exception as exc:
            self.last_error = str(exc)[:300]
            raise

        if (
            not isinstance(resp, dict)
            or not resp.get("success", False)
            or resp.get("status") != "matched"
        ):
            self.last_error = str(resp)[:300]
            return None

        # V2 reports both amounts as six-decimal fixed integers. For a BUY the
        # maker provides pUSD and takes outcome shares. Production may serialize
        # them either as base-unit integers or already-decimal strings.
        filled_shares = min(
            requested_shares,
            _from_fill_amount_near(resp.get("takingAmount", 0), requested_shares),
        )
        notional = _from_fill_amount_near(
            resp.get("makingAmount", 0), plan.gross_usd
        )
        if filled_shares <= 0 or notional <= 0:
            self.last_error = f"Matched response omitted fill amounts: {str(resp)[:220]}"
            return None
        avg_price = notional / filled_shares
        fee_usd = taker_fee(
            avg_price, filled_shares, self.taker_rate, self.fee_exponent
        )

        return Fill(
            slug=market.slug,
            asset=market.asset,
            window=market.window,
            side=side,
            avg_price=avg_price,
            shares=filled_shares,
            stake_usd=notional + fee_usd,
            fee_usd=fee_usd,
            ts=time.time(),
            levels_cleared=plan.levels_cleared,
        )

    def sell(self, market: Any, side: str, shares: float) -> Fill | None:
        """Submit a FAK sell into resting bids. Used by panic-close."""
        self._require_ready()

        book = market.book_for(side)
        if book is None or book.bids.best is None or shares <= 0:
            return None

        tick = float(market.tick_size or 0.01)
        limit_price = max(0.01, _round_to_tick(book.bids.best - tick, tick))
        size = math.floor(shares * 100) / 100
        if size <= 0:
            return None

        client = self.connect()
        from py_clob_client_v2 import (
            MarketOrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        try:
            resp = client.create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=market.token_for(side),
                    amount=size,
                    price=limit_price,
                    side=Side.SELL,
                    order_type=OrderType.FAK,
                ),
                options=PartialCreateOrderOptions(tick_size=_tick_string(tick)),
                order_type=OrderType.FAK,
            )
        except Exception as exc:
            self.last_error = str(exc)[:300]
            raise

        if (
            not isinstance(resp, dict)
            or not resp.get("success", False)
            or resp.get("status") != "matched"
        ):
            self.last_error = str(resp)[:300]
            return None

        # For a SELL the maker provides outcome shares and takes pUSD.
        filled_shares = min(size, _from_fill_amount(resp.get("makingAmount", 0)))
        proceeds = _from_fill_amount(resp.get("takingAmount", 0))
        if filled_shares <= 0 or proceeds <= 0:
            self.last_error = f"Matched response omitted fill amounts: {str(resp)[:220]}"
            return None
        avg_price = proceeds / filled_shares
        return Fill(
            slug=market.slug,
            asset=market.asset,
            window=market.window,
            side=side,
            avg_price=avg_price,
            shares=filled_shares,
            stake_usd=proceeds,
            fee_usd=taker_fee(avg_price, filled_shares, self.taker_rate, self.fee_exponent),
            ts=time.time(),
            levels_cleared=1,
        )

    def cancel_all(self) -> dict[str, Any]:
        """Cancel every resting order. FAK orders leave none, but panic should
        not assume the account's only orders came from this bot."""
        try:
            return {"ok": True, "result": self.connect().cancel_all()}
        except Exception as exc:
            self.last_error = str(exc)[:300]
            return {"ok": False, "error": self.last_error}


def _round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 2)
    # Tick sizes are decimal (0.01, 0.001); round the quotient to avoid binary
    # float error producing a price the venue rejects.
    return round(round(price / tick) * tick, 6)


def _from_base_units(value: Any) -> float:
    try:
        return float(value or 0) / 1_000_000
    except (TypeError, ValueError):
        return 0.0


def _from_fill_amount(value: Any) -> float:
    """Normalize CLOB fill amounts across both observed response encodings."""
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            if any(marker in text for marker in (".", "e", "E")):
                return float(text)
        except ValueError:
            return 0.0
    return _from_base_units(value)


def _from_fill_amount_near(value: Any, expected: float) -> float:
    """Resolve V2's ambiguous integer strings using the signed order amount.

    Production responses have used both six-decimal fixed integers (``5000000``)
    and already-decimal values, including whole-number strings such as ``12``.
    The expected amount from the signed FOK order makes that ambiguity
    deterministic without guessing from string length.
    """
    try:
        raw = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if raw <= 0:
        return 0.0
    candidates = (raw, raw / 1_000_000)
    if expected <= 0:
        return candidates[1]
    return min(
        candidates,
        key=lambda candidate: abs(math.log(max(candidate, 1e-18) / expected)),
    )


def _allowance_values(raw: Any) -> list[float]:
    if isinstance(raw, dict):
        return [_from_base_units(value) for value in raw.values()]
    if isinstance(raw, (list, tuple)):
        return [_from_base_units(value) for value in raw]
    return [_from_base_units(raw)] if raw is not None else []


def _sum_position_value(raw: Any) -> float:
    """Normalize the Data API ``/value`` response without trusting its shape."""
    if not isinstance(raw, list):
        return 0.0
    total = 0.0
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            total += max(0.0, float(row.get("value") or 0.0))
        except (TypeError, ValueError):
            continue
    return total


def _tick_string(tick: float) -> str:
    supported = ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001")
    rendered = format(float(tick), "g")
    if rendered not in supported:
        raise ValueError(f"Unsupported CLOB V2 tick size: {tick}")
    return rendered
