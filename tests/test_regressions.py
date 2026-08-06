import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx

from polybot import settings as settings_module
from polybot.engine import Engine, EngineStats, SIGNAL_RETENTION_SECONDS
from polybot.broker.live import (
    LiveBroker,
    _allowance_values,
    _from_base_units,
    _from_fill_amount,
    _from_fill_amount_near,
    _sum_position_value,
)
from polybot.broker.paper import PaperBroker
from polybot.feeds.chainlink import ChainlinkStreams
from polybot.feeds.chainlink import MAINNET_FEED_IDS, WEI, _decode_price
from polybot.feeds.spot import AssetState, SpotFeed
from polybot.fees import taker_fee
from polybot.risk import RiskManager
from polybot.settings import Runtime, live_trading_enabled
from polybot.signal import Signal, SignalModel
from polybot.store import Store


ROOT = Path(__file__).resolve().parents[1]


class SpotFeedSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_out_of_order_tick_does_not_replace_latest_price(self):
        now = __import__("time").time()
        state = AssetState("btc")
        state.push(101.0, now)
        state.push(100.0, now - 1.0)

        self.assertEqual(state.last_price, 101.0)
        self.assertEqual(state.last_update, now)
        self.assertEqual([tick.ts for tick in state.ticks], sorted(tick.ts for tick in state.ticks))
        self.assertEqual(state.price_at_or_before(now - 0.5), 100.0)

    async def test_coinbase_failure_is_not_reported_as_active(self):
        feed = SpotFeed(["btc"])
        response = SimpleNamespace(status_code=503)
        feed._client = SimpleNamespace(get=AsyncMock(return_value=response))

        await feed._poll_coinbase()

        self.assertFalse(feed.source_health["coinbase"]["active"])
        self.assertEqual(feed.source_health["coinbase"]["received"], 0)

    async def test_kraken_empty_response_is_not_reported_as_active(self):
        feed = SpotFeed(["btc"])
        response = SimpleNamespace(
            status_code=200,
            json=MagicMock(return_value={"error": [], "result": {}}),
        )
        feed._client = SimpleNamespace(get=AsyncMock(return_value=response))

        await feed._poll_kraken()

        self.assertFalse(feed.source_health["kraken"]["active"])
        self.assertEqual(feed.source_health["kraken"]["received"], 0)

    def test_signal_model_rejects_stale_spot_quote(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        model = SignalModel(config)
        state = AssetState("btc", last_price=100.0, last_update=1.0)

        self.assertIsNone(
            model.evaluate(SimpleNamespace(start=0.0), state, now=20.0)
        )


class ConfiguredMarketUniverseTests(unittest.TestCase):
    def test_only_requested_assets_are_enabled(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["assets"], ["btc", "sol", "xrp", "eth"])
        self.assertEqual(
            [(row["asset"], row["window"]) for row in config["excluded_markets"]],
            [("sol", "5m")],
        )
        self.assertEqual(config["bankroll"]["starting_balance_usd"], 150.0)
        self.assertEqual(config["bankroll"]["max_open_exposure_usd"], 60.0)
        self.assertEqual(config["entry"]["hard_window_cap_usd"], 25.0)
        self.assertFalse(config["entry"]["reversal_enabled"])
        self.assertEqual(config["entry"]["max_entries_per_window"], 2)


class DashboardLimitPersistenceTests(unittest.TestCase):
    def test_dashboard_limits_overlay_config_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            operator_path = root / "operator_limits.json"
            config_path.write_text(
                json.dumps({
                    "entry": {"min_confidence": 0.70},
                    "bankroll": {"max_open_exposure_usd": 60.0},
                }),
                encoding="utf-8",
            )
            operator_path.write_text(
                json.dumps({
                    "entry": {"min_confidence": 0.75},
                    "bankroll": {"max_open_exposure_usd": 80.0},
                }),
                encoding="utf-8",
            )
            with (
                patch.object(settings_module, "CONFIG_PATH", config_path),
                patch.object(settings_module, "OPERATOR_LIMITS_PATH", operator_path),
            ):
                config = settings_module.load_config()

        self.assertEqual(config["entry"]["min_confidence"], 0.75)
        self.assertEqual(config["bankroll"]["max_open_exposure_usd"], 80.0)

    def test_paper_and_live_daily_caps_are_identical(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(
            settings_module.daily_cap_for(config, "paper"),
            settings_module.daily_cap_for(config, "live"),
        )


class OperatorMarketExclusionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.engine = object.__new__(Engine)
        self.engine.config = self.config

    def test_only_sol_five_minute_is_disabled(self):
        self.assertFalse(self.engine.configured_market_allowed("sol", "5m"))
        self.assertTrue(self.engine.configured_market_allowed("sol", "15m"))
        self.assertTrue(self.engine.configured_market_allowed("btc", "5m"))
        self.assertTrue(self.engine.configured_market_allowed("eth", "5m"))
        self.assertTrue(self.engine.configured_market_allowed("xrp", "5m"))

    def test_rolling_evidence_gate_blocks_a_degraded_static_allowlist(self):
        self.config["entry"]["require_evidence_qualified_market"] = True
        self.engine.store = SimpleNamespace(
            cohort_evidence=MagicMock(
                return_value={"evidence_qualified": False}
            )
        )

        self.assertFalse(self.engine.configured_market_allowed("btc", "5m"))
        self.assertEqual(
            self.engine.configured_market_rejection("btc", "5m"),
            "market failed rolling paper evidence gate",
        )

    def test_rolling_evidence_gate_allows_a_qualified_cohort(self):
        self.config["entry"]["require_evidence_qualified_market"] = True
        self.engine.store = SimpleNamespace(
            cohort_evidence=MagicMock(
                return_value={"evidence_qualified": True}
            )
        )

        self.assertTrue(self.engine.configured_market_allowed("btc", "5m"))

    def test_90_percent_experiment_bypasses_evidence_only_in_paper(self):
        self.config["entry"].update(
            require_evidence_qualified_market=True,
            asset_min_confidence={"btc": 0.90},
            paper_experimental_markets=[
                {"asset": "btc", "window": "5m", "min_confidence": 0.90}
            ],
        )
        self.engine.store = SimpleNamespace(
            cohort_evidence=MagicMock(
                return_value={"evidence_qualified": False}
            )
        )
        self.engine.runtime = SimpleNamespace(mode="paper")

        self.assertTrue(self.engine.configured_market_allowed("btc", "5m"))
        self.engine.runtime.mode = "live"
        self.assertFalse(self.engine.configured_market_allowed("btc", "5m"))

    def test_configured_76_percent_experiment_bypasses_only_in_paper(self):
        self.config["entry"].update(
            require_evidence_qualified_market=True,
            paper_experimental_min_confidence=0.76,
            asset_min_confidence={"btc": 0.76},
            paper_experimental_markets=[
                {"asset": "btc", "window": "5m", "min_confidence": 0.76}
            ],
        )
        self.engine.store = SimpleNamespace(
            cohort_evidence=MagicMock(
                return_value={"evidence_qualified": False}
            )
        )
        self.engine.runtime = SimpleNamespace(mode="paper")

        self.assertTrue(self.engine.configured_market_allowed("btc", "5m"))
        self.engine.runtime.mode = "live"
        self.assertFalse(self.engine.configured_market_allowed("btc", "5m"))

    def test_static_exclusion_still_wins_over_paper_experiment(self):
        self.config["entry"].update(
            require_evidence_qualified_market=True,
            asset_min_confidence={"sol": 0.90},
            paper_experimental_markets=[
                {"asset": "sol", "window": "5m", "min_confidence": 0.90}
            ],
        )
        self.engine.runtime = SimpleNamespace(mode="paper")

        self.assertFalse(self.engine.configured_market_allowed("sol", "5m"))

    async def test_profit_override_cannot_score_or_trade_disabled_market(self):
        self.config["entry"]["profit_tuned"] = True
        for mode in ("paper", "live"):
            with self.subTest(mode=mode):
                engine = object.__new__(Engine)
                engine.config = self.config
                engine.runtime = SimpleNamespace(mode=mode, snapshot=lambda: {})
                engine.store = SimpleNamespace(
                    exposure_since=MagicMock(return_value=0.0),
                    stats=MagicMock(return_value={"total_pnl": 0.0}),
                )
                engine.live_market_selection = MagicMock(return_value={})
                engine.stats = EngineStats()
                engine.spot = SimpleNamespace(
                    get=MagicMock(side_effect=AssertionError("spot data consulted"))
                )
                engine.model = SimpleNamespace(
                    evaluate=MagicMock(side_effect=AssertionError("model consulted"))
                )
                market = SimpleNamespace(
                    asset="sol", window="5m", slug="sol-5m", end=300.0
                )

                await engine._scan([market], 100.0)

                self.assertEqual(
                    engine.stats.rejections.get("market disabled by operator"), 1
                )
                engine.spot.get.assert_not_called()
                engine.model.evaluate.assert_not_called()


class ConfidenceOnlyOverrideTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.config["entry"].update(
            confidence_only=True,
            profit_tuned=False,
            min_confidence=0.62,
            max_entry_price=0.80,
        )

    def _signal(self, confidence, entry_price=0.80):
        return Signal(
            asset="btc",
            slug="btc-5m",
            window="5m",
            side="up",
            confidence=confidence,
            raw_probability=confidence,
            entry_price=entry_price,
            expected_roi=-0.99,
            edge=-0.50,
            anchor_price=100.0,
            spot_price=101.0,
            drift_bps=100.0,
            sigma_remaining=0.01,
            seconds_remaining=1.0,
            ask_depth_usd=0.01,
        )

    def test_confidence_override_still_obeys_hard_price_ceiling(self):
        model = SignalModel(self.config)
        market = SimpleNamespace(
            start=0.0,
            end=300.0,
            accepting_orders=True,
            closed=False,
            seconds_elapsed=lambda now: 299.0,
        )

        self.assertIsNone(model._reject_reason(self._signal(0.62), market, 299.0))
        self.assertEqual(
            model._reject_reason(self._signal(0.90, 0.81), market, 299.0),
            "entry 0.81 above price ceiling",
        )
        self.assertEqual(
            model._reject_reason(self._signal(0.619), market, 299.0),
            "confidence 62% < 62%",
        )

        market.accepting_orders = False
        self.assertEqual(
            model._reject_reason(self._signal(0.90), market, 299.0),
            "market not accepting orders",
        )

    def test_asset_specific_confidence_floor_is_enforced(self):
        self.config["entry"]["asset_min_confidence"] = {"btc": 0.76}
        model = SignalModel(self.config)
        market = SimpleNamespace(
            start=0.0,
            end=300.0,
            accepting_orders=True,
            closed=False,
            seconds_elapsed=lambda now: 1.0,
        )

        self.assertEqual(
            model._reject_reason(self._signal(0.759), market, 1.0),
            "confidence 76% < 76%",
        )
        self.assertIsNone(model._reject_reason(self._signal(0.76), market, 1.0))

    async def _exercise_scan(
        self, mode, confidence=0.62, open_exposure=0.0, expect_entry=True
    ):
        engine = object.__new__(Engine)
        engine.config = self.config
        engine.runtime = SimpleNamespace(
            mode=mode,
            snapshot=lambda: {
                "max_per_trade_usd": 5.0,
                "max_per_window_usd": 0.0,
                "daily_cap_usd": 0.0,
            },
            update=MagicMock(),
        )
        engine.store = SimpleNamespace(
            exposure_since=MagicMock(return_value=0.0),
            open_exposure=MagicMock(return_value=open_exposure),
            stats=MagicMock(return_value={"total_pnl": 0.0}),
            window_exposure=MagicMock(return_value=0.0),
            entries_for_window=MagicMock(side_effect=AssertionError("entry cap consulted")),
            open_position_for=MagicMock(return_value=None),
            interval_exposure=MagicMock(return_value=0.0),
            open_position=MagicMock(return_value=1),
            log=MagicMock(),
        )
        engine.live_market_selection = MagicMock(return_value={"enabled": True})
        engine.live_market_allowed = MagicMock(return_value=False)
        engine.spot = SimpleNamespace(get=lambda asset: object())
        signal = SimpleNamespace(
            tradeable=True,
            reason=None,
            side="up",
            confidence=confidence,
            entry_price=0.80,
            ask_depth_usd=0.0,
            anchor_price=100.0,
            expected_roi=-0.99,
            seconds_remaining=1.0,
        )
        engine.model = SimpleNamespace(evaluate=MagicMock(return_value=signal))
        engine._remember_signal = MagicMock()
        engine._anchors = {}
        engine.risk = SimpleNamespace(
            size_for=MagicMock(return_value=5.0),
            check_entry=MagicMock(side_effect=AssertionError("risk gate consulted")),
            record_entry=MagicMock(),
            breaker_active=True,
            min_trade_usd=5.0,
        )
        engine.stats = EngineStats()
        fill = SimpleNamespace(
            avg_price=0.80,
            shares=5.0 / 0.80,
            stake_usd=5.0,
            ts=100.0,
            fee_usd=0.0,
        )
        engine._execute_buy = AsyncMock(return_value=fill)
        market = SimpleNamespace(
            asset="btc",
            slug="btc-5m",
            window="5m",
            end=300.0,
            question="BTC Up or Down?",
        )

        await engine._scan([market], 100.0)

        self.assertEqual(engine.stats.entries, 1 if expect_entry else 0)
        if expect_entry:
            engine._execute_buy.assert_awaited_once_with(market, "up", 5.0, 0.80)
        else:
            engine._execute_buy.assert_not_awaited()
            self.assertTrue(
                any(
                    reason.startswith("$150 bankroll open-exposure cap reached")
                    for reason in engine.stats.rejections
                )
            )
        engine.live_market_allowed.assert_not_called()
        engine.store.open_position_for.assert_not_called()
        engine.risk.check_entry.assert_not_called()
        engine.risk.record_entry.assert_not_called()

    async def test_paper_and_live_share_the_same_override_path(self):
        for mode in ("paper", "live"):
            with self.subTest(mode=mode):
                await self._exercise_scan(mode)

    async def test_profit_tuned_mode_uses_the_same_paper_and_live_engine_path(self):
        self.config["entry"].update(
            confidence_only=False,
            profit_tuned=True,
            min_confidence=0.70,
        )
        for mode in ("paper", "live"):
            with self.subTest(mode=mode):
                await self._exercise_scan(mode, confidence=0.70)

    async def test_profit_override_cannot_bypass_bankroll_cap(self):
        self.config["entry"].update(confidence_only=False, profit_tuned=True)
        for mode in ("paper", "live"):
            with self.subTest(mode=mode):
                await self._exercise_scan(
                    mode, confidence=0.70, open_exposure=60.0, expect_entry=False
                )


class HardWindowCapTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(Engine)
        self.engine.config = {
            "entry": {"hard_window_cap_usd": 50.0},
        }
        self.engine.risk = SimpleNamespace(min_trade_usd=5.0)
        self.engine.store = SimpleNamespace(interval_exposure=MagicMock())

    def test_request_is_trimmed_to_never_cross_fifty_dollars(self):
        self.engine.store.interval_exposure.return_value = 44.0

        size, reason = self.engine._apply_hard_window_cap(
            "live", "5m", 300.0, 10.0
        )

        self.assertEqual(size, 6.0)
        self.assertIsNone(reason)

    def test_new_order_is_blocked_when_less_than_venue_minimum_remains(self):
        self.engine.store.interval_exposure.return_value = 45.01

        size, reason = self.engine._apply_hard_window_cap(
            "live", "5m", 300.0, 5.0
        )

        self.assertIsNone(size)
        self.assertIn("$45.01/$50.00", reason)

    def test_assets_sharing_a_timed_window_share_one_budget(self):
        self.engine.store.interval_exposure.return_value = 50.0

        btc_size, _ = self.engine._apply_hard_window_cap(
            "paper", "5m", 300.0, 5.0
        )
        eth_size, _ = self.engine._apply_hard_window_cap(
            "paper", "5m", 300.0, 5.0
        )

        self.assertIsNone(btc_size)
        self.assertIsNone(eth_size)
        self.engine.store.interval_exposure.assert_has_calls(
            [
                call("paper", "5m", 300.0),
                call("paper", "5m", 300.0),
            ]
        )

    def test_different_closing_windows_have_independent_budgets(self):
        exposures = {300.0: 50.0, 600.0: 10.0}
        self.engine.store.interval_exposure.side_effect = (
            lambda mode, window, window_end: exposures[window_end]
        )

        current_size, _ = self.engine._apply_hard_window_cap(
            "live", "5m", 300.0, 5.0
        )
        next_size, _ = self.engine._apply_hard_window_cap(
            "live", "5m", 600.0, 5.0
        )

        self.assertIsNone(current_size)
        self.assertEqual(next_size, 5.0)


class MarketWindowCapTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(Engine)
        self.engine.config = {
            "entry": {
                "hard_market_window_cap_usd": 20.0,
                "hard_window_cap_usd": 50.0,
            },
        }
        self.engine.risk = SimpleNamespace(min_trade_usd=5.0)
        self.engine.store = SimpleNamespace(window_exposure=MagicMock())

    def test_exact_market_is_blocked_below_the_venue_minimum(self):
        self.engine.store.window_exposure.return_value = 16.0

        size, reason = self.engine._apply_market_window_cap(
            "paper", "btc-updown-5m-300", 10.0
        )

        self.assertIsNone(size)
        self.assertIn("$16.00/$20.00", reason)

    def test_different_market_slugs_receive_independent_budgets(self):
        exposures = {
            "btc-updown-5m-300": 20.0,
            "eth-updown-5m-300": 5.0,
        }
        self.engine.store.window_exposure.side_effect = (
            lambda mode, slug: exposures[slug]
        )

        btc_size, _ = self.engine._apply_market_window_cap(
            "live", "btc-updown-5m-300", 5.0
        )
        eth_size, eth_reason = self.engine._apply_market_window_cap(
            "live", "eth-updown-5m-300", 5.0
        )

        self.assertIsNone(btc_size)
        self.assertEqual(eth_size, 5.0)
        self.assertIsNone(eth_reason)


class BankrollCapTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(Engine)
        self.engine.config = {
            "bankroll": {
                "starting_balance_usd": 150.0,
                "max_open_exposure_usd": 60.0,
                "cash_reserve_usd": 90.0,
                "max_daily_loss_usd": 15.0,
                "max_daily_turnover_usd": 300.0,
            }
        }
        self.engine.risk = SimpleNamespace(min_trade_usd=5.0)
        self.engine.store = SimpleNamespace(
            open_exposure=MagicMock(return_value=0.0),
            exposure_since=MagicMock(return_value=0.0),
            stats=MagicMock(return_value={"total_pnl": 0.0}),
        )

    def test_open_cap_preserves_ninety_dollar_reserve(self):
        self.engine.store.open_exposure.return_value = 56.0

        size, reason = self.engine._apply_bankroll_cap("live", 5.0, 100.0)

        self.assertIsNone(size)
        self.assertIn("$56.00/$60.00", reason)

    def test_size_is_trimmed_to_remaining_open_capital(self):
        self.engine.store.open_exposure.return_value = 53.0

        size, reason = self.engine._apply_bankroll_cap("paper", 10.0, 100.0)

        self.assertEqual(size, 7.0)
        self.assertIsNone(reason)

    def test_daily_loss_stop_is_mandatory(self):
        self.engine.store.stats.return_value = {"total_pnl": -15.0}

        size, reason = self.engine._apply_bankroll_cap("live", 5.0, 100.0)

        self.assertIsNone(size)
        self.assertIn("daily loss stop", reason)

    def test_daily_profit_lock_stops_new_entries(self):
        self.engine.config["bankroll"]["daily_profit_lock_usd"] = 5000.0
        self.engine.store.stats.return_value = {"total_pnl": 5000.0}

        size, reason = self.engine._apply_bankroll_cap("paper", 5.0, 100.0)

        self.assertIsNone(size)
        self.assertIn("Daily profit lock", reason)

    def test_temporary_daily_loss_bypass_applies_only_to_paper(self):
        self.engine.store.stats.return_value = {"total_pnl": -15.0}
        self.engine.paper_daily_loss_bypass_until = 200.0

        paper_size, paper_reason = self.engine._apply_bankroll_cap(
            "paper", 5.0, 100.0
        )
        live_size, live_reason = self.engine._apply_bankroll_cap(
            "live", 5.0, 100.0
        )

        self.assertEqual(paper_size, 5.0)
        self.assertIsNone(paper_reason)
        self.assertIsNone(live_size)
        self.assertIn("daily loss stop", live_reason)

    def test_temporary_daily_loss_bypass_expires(self):
        self.engine.store.stats.return_value = {"total_pnl": -15.0}
        self.engine.paper_daily_loss_bypass_until = 200.0

        size, reason = self.engine._apply_bankroll_cap("paper", 5.0, 200.0)

        self.assertIsNone(size)
        self.assertIn("daily loss stop", reason)

    def test_daily_turnover_is_limited_to_twice_bankroll(self):
        self.engine.store.exposure_since.return_value = 296.0

        size, reason = self.engine._apply_bankroll_cap("paper", 5.0, 100.0)

        self.assertIsNone(size)
        self.assertIn("$296.00/$300.00", reason)


class LiveWalletRiskTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(Engine)
        self.engine.config = {
            "bankroll": {
                "starting_balance_usd": 200.0,
                "max_open_exposure_usd": 50.0,
                "cash_reserve_usd": 150.0,
            }
        }
        self.engine.risk = SimpleNamespace(min_trade_usd=5.0)
        self.engine.maker = None
        self.engine.store = SimpleNamespace(
            open_exposure=MagicMock(return_value=0.0)
        )
        self.engine.live = SimpleNamespace(balance=MagicMock())
        self.engine._last_wallet_risk = {}

    def test_real_equity_below_reserve_blocks_and_requires_pause(self):
        self.engine.live.balance.return_value = {
            "ok": True,
            "balance_pusd": 0.89,
            "positions_value_pusd": 17.20,
            "account_equity_pusd": 18.09,
            "positions_error": None,
        }

        size, reason, must_pause = self.engine._apply_live_wallet_cap(5.0)

        self.assertIsNone(size)
        self.assertTrue(must_pause)
        self.assertIn("$18.09 equity", reason)
        self.assertEqual(
            self.engine._last_wallet_risk["wallet_risk_budget_usd"], 0.0
        )

    def test_wallet_position_value_catches_untracked_exposure(self):
        self.engine.live.balance.return_value = {
            "ok": True,
            "balance_pusd": 152.0,
            "positions_value_pusd": 48.0,
            "account_equity_pusd": 200.0,
            "positions_error": None,
        }

        size, reason, must_pause = self.engine._apply_live_wallet_cap(5.0)

        self.assertIsNone(size)
        self.assertFalse(must_pause)
        self.assertIn("$48.00/$50.00", reason)

    def test_wallet_room_allows_only_capital_above_reserve(self):
        self.engine.store.open_exposure.return_value = 20.0
        self.engine.live.balance.return_value = {
            "ok": True,
            "balance_pusd": 180.0,
            "positions_value_pusd": 20.0,
            "account_equity_pusd": 200.0,
            "positions_error": None,
        }

        size, reason, must_pause = self.engine._apply_live_wallet_cap(5.0)

        self.assertEqual(size, 5.0)
        self.assertIsNone(reason)
        self.assertFalse(must_pause)
        self.assertEqual(
            self.engine._last_wallet_risk["wallet_new_order_room_usd"], 30.0
        )

    def test_missing_position_value_fails_closed(self):
        self.engine.live.balance.return_value = {
            "ok": True,
            "balance_pusd": 200.0,
            "positions_value_pusd": None,
            "account_equity_pusd": None,
            "positions_error": "position API unavailable",
        }

        size, reason, must_pause = self.engine._apply_live_wallet_cap(5.0)

        self.assertIsNone(size)
        self.assertTrue(must_pause)
        self.assertIn("position API unavailable", reason)


class ProfitTunedSignalTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.model = SignalModel(self.config)
        self.market = SimpleNamespace(
            start=0.0,
            end=300.0,
            accepting_orders=True,
            closed=False,
            seconds_elapsed=lambda now: now,
        )

    def _signal(self, confidence=0.70, price=0.79):
        return Signal(
            asset="btc",
            slug="btc-5m",
            window="5m",
            side="up",
            confidence=confidence,
            raw_probability=confidence,
            entry_price=price,
            expected_roi=-0.50,
            edge=-0.50,
            anchor_price=100.0,
            spot_price=101.0,
            drift_bps=100.0,
            sigma_remaining=0.01,
            seconds_remaining=1.0,
            ask_depth_usd=0.01,
        )

    def test_tuned_boundary_is_accepted_despite_legacy_roi_depth_and_time_gates(self):
        self.assertIsNone(
            self.model._reject_reason(self._signal(), self.market, 150.0)
        )

    def test_tuned_mode_blocks_each_empirical_loss_boundary(self):
        self.assertEqual(
            self.model._reject_reason(self._signal(confidence=0.699), self.market, 100.0),
            "confidence 70% < 70%",
        )
        self.assertEqual(
            self.model._reject_reason(self._signal(price=0.80), self.market, 100.0),
            "entry 0.80 above price ceiling",
        )
        self.assertEqual(
            self.model._reject_reason(self._signal(), self.market, 151.0),
            "too deep into the window (>50% elapsed)",
        )


class LiveTradingGateTests(unittest.TestCase):
    def test_project_env_overrides_stale_parent_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("LIVE_TRADING_ENABLED=1\n", encoding="utf-8")
            with (
                patch("polybot.settings.ENV_PATH", env_path),
                patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "0"}),
            ):
                self.assertTrue(live_trading_enabled())

    def test_legacy_clob_credentials_do_not_open_live_gate(self):
        creds = {
            "POLY_PRIVATE_KEY": "stored",
            "POLY_API_KEY": "key",
            "POLY_API_SECRET": "secret",
            "POLY_API_PASSPHRASE": "passphrase",
            "POLY_CLOB_VERSION": "",
        }
        with (
            patch("polybot.broker.live.vault_current", return_value=creds),
            patch("polybot.broker.live.live_trading_enabled", return_value=True),
        ):
            state = LiveBroker.availability()

        self.assertFalse(state["ready"])
        self.assertIn("verified for V2", " ".join(state["blockers"]))

    def test_saved_slider_below_venue_minimum_is_clamped(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        with patch(
            "polybot.settings.load_runtime_limits",
            return_value={
                "paper": {
                    "max_per_trade_usd": 3.0,
                    "max_per_window_usd": 4.0,
                    "daily_cap_usd": 100.0,
                }
            },
        ):
            runtime = Runtime.from_config(config)

        self.assertEqual(runtime.max_per_trade_usd, 5.0)
        self.assertEqual(runtime.max_per_window_usd, 5.0)

    def test_divergent_legacy_mode_limits_fall_back_to_shared_config(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        with patch(
            "polybot.settings.load_runtime_limits",
            return_value={
                "paper": {
                    "max_per_trade_usd": 20.0,
                    "max_per_window_usd": 100.0,
                    "daily_cap_usd": 1000.0,
                },
                "live": {
                    "max_per_trade_usd": 5.0,
                    "max_per_window_usd": 25.0,
                    "daily_cap_usd": 300.0,
                },
            },
        ):
            runtime = Runtime.from_config(config)

        self.assertEqual(runtime.max_per_trade_usd, config["sizing"]["max_clip_usd"])
        self.assertEqual(
            runtime.max_per_window_usd, config["sizing"]["max_per_window_usd"]
        )
        self.assertEqual(runtime.daily_cap_usd, config["sizing"]["daily_cap_usd"])


class ClobV2Tests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    def test_pusd_base_units_and_allowance_map(self):
        self.assertEqual(_from_base_units("12345678"), 12.345678)
        self.assertEqual(
            _allowance_values({"exchange": "5000000", "neg-risk": "7000000"}),
            [5.0, 7.0],
        )

    def test_fill_amount_accepts_base_units_and_decimal_strings(self):
        self.assertEqual(_from_fill_amount("5000000"), 5.0)
        self.assertEqual(_from_fill_amount("4.999996"), 4.999996)
        self.assertEqual(_from_fill_amount(4.999996), 4.999996)
        self.assertEqual(_from_fill_amount_near("12", 12.0), 12.0)
        self.assertEqual(_from_fill_amount_near("12000000", 12.0), 12.0)

    def test_position_value_uses_only_valid_nonnegative_rows(self):
        self.assertEqual(
            _sum_position_value([
                {"value": 12.5},
                {"value": "2.25"},
                {"value": -9},
                {"value": "bad"},
                None,
            ]),
            14.75,
        )
        self.assertEqual(_sum_position_value({"value": 99}), 0.0)

    def test_buy_records_v2_fixed_point_fill_amounts(self):
        broker = LiveBroker(self.config)
        posted = {}

        def post_market_order(**kwargs):
            posted.update(kwargs)
            return {
                "success": True,
                "status": "matched",
                "makingAmount": "4820000",
                "takingAmount": str(4.82 / 0.49),
            }

        client = SimpleNamespace(create_and_post_market_order=post_market_order)
        market = SimpleNamespace(
            slug="test-market",
            asset="btc",
            window="5m",
            tick_size=0.01,
            book_for=lambda _: SimpleNamespace(
                asks=SimpleNamespace(best=0.49, levels=[(0.49, 100.0)]),
                bids=SimpleNamespace(best=0.48),
            ),
            token_for=lambda _: "123",
        )
        with (
            patch.object(LiveBroker, "availability", return_value={"ready": True, "blockers": []}),
            patch.object(broker, "connect", return_value=client),
        ):
            fill = broker.buy(market, "up", 5.0)

        self.assertIsNotNone(fill)
        self.assertAlmostEqual(fill.shares, 4.82 / 0.49)
        self.assertLessEqual(fill.stake_usd, 5.0)
        self.assertAlmostEqual(fill.avg_price, 0.49)
        self.assertEqual(posted["order_args"].amount, 4.82)
        self.assertEqual(posted["order_args"].price, 0.49)
        self.assertEqual(str(posted["order_type"]), "FOK")

    def test_buy_keeps_notional_plus_fee_inside_slider_budget(self):
        broker = LiveBroker(self.config)
        posted = {}

        def post_market_order(**kwargs):
            posted.update(kwargs)
            return {
                "success": True,
                "status": "matched",
                "makingAmount": "4830000",
                "takingAmount": str(4.83 / 0.49),
            }

        client = SimpleNamespace(create_and_post_market_order=post_market_order)
        market = SimpleNamespace(
            slug="test-market",
            asset="btc",
            window="5m",
            tick_size=0.01,
            book_for=lambda _: SimpleNamespace(
                asks=SimpleNamespace(best=0.49, levels=[(0.49, 100.0)]),
                bids=SimpleNamespace(best=0.48),
            ),
            token_for=lambda _: "123",
        )
        with (
            patch.object(LiveBroker, "availability", return_value={"ready": True, "blockers": []}),
            patch.object(broker, "connect", return_value=client),
        ):
            fill = broker.buy(market, "up", 5.009)

        self.assertEqual(posted["order_args"].amount, 4.83)
        self.assertLessEqual(fill.stake_usd, 5.009)


class FeeParityTests(unittest.TestCase):
    def test_clob_v2_fee_formula(self):
        self.assertAlmostEqual(taker_fee(0.5, 10.0, 0.07, 1.0), 0.175)

    def test_paper_plan_uses_full_depth_and_total_cost_budget(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        broker = PaperBroker(config)
        market = SimpleNamespace(
            slug="test-market",
            asset="btc",
            window="5m",
            book_for=lambda _: SimpleNamespace(
                asks=SimpleNamespace(levels=[(0.49, 5.0), (0.50, 100.0)])
            ),
        )

        plan = broker.plan_buy(market, "up", 5.0, max_price=0.50)

        self.assertIsNotNone(plan)
        self.assertEqual(plan.levels_cleared, 2)
        self.assertEqual(plan.limit_price, 0.50)
        self.assertLessEqual(plan.total_usd, 5.0)
        self.assertGreaterEqual(plan.total_usd, 4.98)

    def test_fee_rebaseline_is_transactional_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "fees.db")
            position_id = store.open_position(
                mode="paper", slug="btc-5m", asset="btc", window="5m",
                side="up", confidence=0.7, entry_price=0.5, shares=10.0,
                stake_usd=5.0, anchor_price=100.0, opened_at=1.0,
                window_end=300.0, switches=0,
            )
            store.record_settlement(
                position_id=position_id, mode="paper", slug="btc-5m",
                asset="btc", window="5m", side="up", confidence=0.7,
                entry_price=0.5, shares=10.0, stake_usd=5.0,
                payout_usd=10.0, pnl_usd=5.0, won=1,
                anchor_price=100.0, close_price=101.0, settled_at=301.0,
                method="spot",
            )

            first = store.rebaseline_taker_fees(0.07, 1.0)
            second = store.rebaseline_taker_fees(0.07, 1.0)
            row = store.settlements("paper", 1)[0]
            store.close()

        self.assertTrue(first["applied"])
        self.assertFalse(second["applied"])
        self.assertAlmostEqual(row["stake_usd"], 5.175)
        self.assertAlmostEqual(row["pnl_usd"], 4.825)

    def test_fee_rebaseline_repairs_ambiguous_whole_number_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "repair.db")
            position_id = store.open_position(
                mode="live", slug="bnb-5m", asset="bnb", window="5m",
                side="up", confidence=0.7, entry_price=385000.0,
                shares=0.000012, stake_usd=4.62, anchor_price=100.0,
                opened_at=1.0, window_end=300.0, switches=0,
            )
            store.record_settlement(
                position_id=position_id, mode="live", slug="bnb-5m",
                asset="bnb", window="5m", side="up", confidence=0.7,
                entry_price=385000.0, shares=0.000012, stake_usd=4.62,
                payout_usd=0.0, pnl_usd=-4.62, won=0,
                anchor_price=100.0, close_price=99.0, settled_at=301.0,
                method="spot",
            )

            result = store.rebaseline_taker_fees(0.07, 1.0)
            row = store.settlements("live", 1)[0]
            store.close()

        self.assertEqual(result["repaired"], 2)
        self.assertAlmostEqual(row["entry_price"], 0.385)
        self.assertAlmostEqual(row["shares"], 12.0)
        self.assertAlmostEqual(row["stake_usd"], 4.818891)
        self.assertAlmostEqual(row["pnl_usd"], -4.818891)


class WinLossRangeTests(unittest.TestCase):
    def test_range_filters_summary_rows_and_asset_breakdown_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "win-loss-ranges.db")
            common = {
                "position_id": None,
                "mode": "live",
                "window": "5m",
                "side": "up",
                "confidence": 0.75,
                "entry_price": 0.5,
                "shares": 2.0,
                "stake_usd": 1.0,
                "anchor_price": 100.0,
                "close_price": 101.0,
                "method": "spot",
            }
            store.record_settlement(
                **common,
                slug="bnb-updown-5m-0",
                asset="bnb",
                payout_usd=2.0,
                pnl_usd=1.0,
                won=1,
                settled_at=100.0,
            )
            store.record_settlement(
                **common,
                slug="btc-updown-5m-0",
                asset="btc",
                payout_usd=0.0,
                pnl_usd=-1.0,
                won=0,
                settled_at=200.0,
            )

            recent_rows = store.settlements("live", 200, since=150.0)
            recent_summary = store.win_loss_summary("live", since=150.0)
            recent_assets = store.by_asset("live", since=150.0)
            all_summary = store.win_loss_summary("live")
            store.close()

        self.assertEqual([row["asset"] for row in recent_rows], ["btc"])
        self.assertEqual(recent_summary["settlements"], 1)
        self.assertEqual(recent_summary["wins"], 0)
        self.assertEqual(recent_summary["losses"], 1)
        self.assertEqual(recent_summary["gross_loss"], -1.0)
        self.assertEqual([row["asset"] for row in recent_assets], ["btc"])
        self.assertEqual(all_summary["settlements"], 2)
        self.assertEqual(all_summary["wins"], 1)
        self.assertEqual(all_summary["losses"], 1)


class LiveExecutionParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_hard_price_ceiling_blocks_before_book_refresh(self):
        engine = object.__new__(Engine)
        engine.config = {"entry": {"max_entry_price": 0.80}}
        engine.runtime = SimpleNamespace(mode="live", paused=False)
        engine.poly = SimpleNamespace(refresh_book=AsyncMock(return_value=True))
        engine.paper = SimpleNamespace(plan_buy=MagicMock(return_value=object()))
        engine.live = SimpleNamespace(buy=MagicMock())
        engine.stats = SimpleNamespace(reject=MagicMock())
        market = SimpleNamespace(asset="btc", slug="btc-5m")

        fill = await engine._execute_buy(market, "up", 5.0, 0.81)

        self.assertIsNone(fill)
        engine.poly.refresh_book.assert_not_awaited()
        engine.paper.plan_buy.assert_not_called()
        engine.live.buy.assert_not_called()
        engine.stats.reject.assert_called_once_with(
            "entry 0.81 above 0.80 hard price ceiling"
        )

    async def test_paper_execution_refreshes_the_same_book_as_live(self):
        engine = object.__new__(Engine)
        engine.runtime = SimpleNamespace(mode="paper")
        engine.poly = SimpleNamespace(refresh_book=AsyncMock(return_value=True))
        expected_fill = SimpleNamespace(stake_usd=5.0)
        engine.paper = SimpleNamespace(
            plan_buy=MagicMock(return_value=object()),
            buy=MagicMock(return_value=expected_fill),
        )
        engine.stats = SimpleNamespace(reject=MagicMock())
        market = SimpleNamespace(asset="btc", slug="btc-5m")

        fill = await engine._execute_buy(market, "up", 5.0, 0.50)

        self.assertIs(fill, expected_fill)
        engine.poly.refresh_book.assert_awaited_once_with(market, "up")
        engine.paper.plan_buy.assert_called_once_with(market, "up", 5.0, 0.50)
        engine.paper.buy.assert_called_once_with(market, "up", 5.0, 0.50)

    async def test_no_fill_gets_one_fresh_book_retry(self):
        engine = object.__new__(Engine)
        engine.runtime = SimpleNamespace(mode="live", update=MagicMock())
        engine.poly = SimpleNamespace(refresh_book=AsyncMock(return_value=True))
        engine.paper = SimpleNamespace(plan_buy=MagicMock(return_value=object()))
        expected_fill = SimpleNamespace(stake_usd=5.0)
        engine.live = SimpleNamespace(
            buy=MagicMock(
                side_effect=[
                    RuntimeError("FOK order couldn't be fully filled"),
                    expected_fill,
                ]
            )
        )
        engine.stats = SimpleNamespace(
            reject=MagicMock(),
            live_fill_misses=0,
        )
        engine.store = SimpleNamespace(log=MagicMock())
        market = SimpleNamespace(asset="bnb", slug="bnb-5m")

        fill = await engine._execute_buy(market, "up", 5.0, 0.50)

        self.assertIs(fill, expected_fill)
        self.assertEqual(engine.poly.refresh_book.await_count, 2)
        self.assertEqual(engine.live.buy.call_count, 2)
        self.assertEqual(engine.stats.live_fill_misses, 0)

    async def test_live_order_is_blocked_when_fresh_paper_plan_cannot_fill(self):
        engine = object.__new__(Engine)
        engine.runtime = SimpleNamespace(mode="live", update=MagicMock())
        engine.poly = SimpleNamespace(refresh_book=AsyncMock(return_value=True))
        engine.paper = SimpleNamespace(plan_buy=MagicMock(return_value=None))
        engine.live = SimpleNamespace(buy=MagicMock())
        engine.stats = SimpleNamespace(
            reject=MagicMock(),
            live_fill_misses=0,
        )
        engine.store = SimpleNamespace(log=MagicMock())
        market = SimpleNamespace(asset="bnb", slug="bnb-5m")

        fill = await engine._execute_buy(market, "up", 5.0, 0.50)

        self.assertIsNone(fill)
        engine.live.buy.assert_not_called()
        engine.stats.reject.assert_called_once_with(
            "fresh book cannot fill order"
        )

    async def test_pause_is_rechecked_after_the_final_book_refresh(self):
        engine = object.__new__(Engine)
        engine.runtime = SimpleNamespace(mode="live", paused=True)
        engine.poly = SimpleNamespace(refresh_book=AsyncMock(return_value=True))
        engine.paper = SimpleNamespace(plan_buy=MagicMock(return_value=object()))
        engine.live = SimpleNamespace(buy=MagicMock())
        engine.stats = SimpleNamespace(reject=MagicMock())
        market = SimpleNamespace(asset="btc", slug="btc-5m")

        fill = await engine._execute_buy(market, "up", 5.0, 0.50)

        self.assertIsNone(fill)
        engine.poly.refresh_book.assert_awaited_once_with(market, "up")
        engine.live.buy.assert_not_called()
        engine.stats.reject.assert_called_once_with(
            "engine paused before live order"
        )


class SettlementRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_inactive_paper_position_without_feed_is_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "settlement.db")
            store.open_position(
                mode="paper",
                slug="retired-updown-5m-0",
                asset="retired",
                window="5m",
                side="up",
                confidence=0.7,
                entry_price=0.5,
                shares=10.0,
                stake_usd=5.0,
                anchor_price=100.0,
                opened_at=1.0,
                window_end=300.0,
                switches=0,
            )
            engine = object.__new__(Engine)
            engine.runtime = SimpleNamespace(mode="live")
            engine.store = store
            engine.spot = SimpleNamespace(get=MagicMock(return_value=None))
            engine._anchors = {}

            await engine._settle_closed(601.0)

            self.assertEqual(store.open_positions("paper"), [])
            self.assertEqual(store.stats("paper")["open_positions"], 0)
            self.assertIn(
                "no settlement feed",
                store.activity("paper", 1)[0]["message"],
            )
            store.close()


class CohortEvidenceTests(unittest.TestCase):
    def test_requires_two_positive_non_overlapping_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "evidence.db")
            common = {
                "position_id": None,
                "mode": "paper",
                "slug": "bnb-updown-5m-0",
                "asset": "bnb",
                "window": "5m",
                "side": "up",
                "confidence": 0.8,
                "entry_price": 0.5,
                "shares": 2.0,
                "stake_usd": 1.0,
                "payout_usd": 2.0,
                "pnl_usd": 1.0,
                "won": 1,
                "anchor_price": 100.0,
                "close_price": 101.0,
                "method": "spot",
            }
            for i in range(100):
                store.record_settlement(**common, settled_at=float(i + 1))

            qualified = store.cohort_evidence("paper", "bnb", "5m")
            self.assertTrue(qualified["evidence_qualified"])
            self.assertEqual(qualified["latest_50"]["settlements"], 50)
            self.assertEqual(qualified["preceding_50"]["settlements"], 50)

            store.record_settlement(
                **{
                    **common,
                    "payout_usd": 0.0,
                    "pnl_usd": -100.0,
                    "won": 0,
                    "settled_at": 101.0,
                }
            )
            degraded = store.cohort_evidence("paper", "bnb", "5m")
            store.close()

        self.assertFalse(degraded["evidence_qualified"])
        self.assertLess(degraded["latest_50"]["roi"], 0)
        self.assertGreater(degraded["preceding_50"]["roi"], 0)


class LiveMarketSelectionTests(unittest.TestCase):
    def setUp(self):
        rows = [
            {"asset": "doge", "window": "15m", "settlements": 112, "roi": 0.48, "pnl": 48},
            {"asset": "bnb", "window": "15m", "settlements": 100, "roi": 0.43, "pnl": 43},
            {"asset": "bnb", "window": "5m", "settlements": 589, "roi": 0.39, "pnl": 195},
            {"asset": "doge", "window": "5m", "settlements": 475, "roi": 0.34, "pnl": 160},
            {"asset": "hype", "window": "5m", "settlements": 402, "roi": 0.25, "pnl": 100},
            {"asset": "sol", "window": "15m", "settlements": 170, "roi": 0.06, "pnl": 10},
            {"asset": "eth", "window": "5m", "settlements": 5, "roi": 0.90, "pnl": 4},
        ]
        engine = object.__new__(Engine)
        engine.config = {
            "live_market_selection": {
                "enabled": True,
                "min_paper_settlements": 100,
                "min_paper_roi": 0.10,
                "max_markets": 5,
            }
        }
        engine.assets = ["doge", "bnb", "hype", "sol", "eth"]
        engine.windows = ["5m", "15m"]
        engine.store = SimpleNamespace(by_asset=lambda mode: rows if mode == "paper" else [])
        engine.runtime = SimpleNamespace(mode="live")
        self.engine = engine

    def test_only_top_proven_paper_markets_are_live_eligible(self):
        selection = self.engine.live_market_selection()
        eligible = [(row["asset"], row["window"]) for row in selection["eligible"]]

        self.assertEqual(
            eligible,
            [
                ("doge", "15m"),
                ("bnb", "15m"),
                ("bnb", "5m"),
                ("doge", "5m"),
                ("hype", "5m"),
            ],
        )
        self.assertTrue(self.engine.live_market_allowed("doge", "15m", selection))
        self.assertFalse(self.engine.live_market_allowed("sol", "15m", selection))
        self.assertFalse(self.engine.live_market_allowed("eth", "5m", selection))

    def test_paper_mode_keeps_testing_every_market(self):
        self.engine.runtime.mode = "paper"
        selection = self.engine.live_market_selection()

        self.assertTrue(self.engine.live_market_allowed("eth", "5m", selection))
        self.assertTrue(self.engine.live_market_allowed("sol", "15m", selection))

    def test_disabled_whitelist_allows_every_paper_market_in_live(self):
        self.engine.config["live_market_selection"]["enabled"] = False
        selection = self.engine.live_market_selection()

        self.assertTrue(self.engine.live_market_allowed("eth", "5m", selection))
        self.assertTrue(self.engine.live_market_allowed("sol", "15m", selection))

    def test_one_normal_entry_slot_is_reserved_for_side_switching(self):
        self.engine.config["entry"] = {
            "reversal_enabled": True,
            "max_side_switches": 1,
        }
        self.engine.max_entries_per_window = 3

        self.assertEqual(self.engine.normal_entry_cap(), 2)
        self.engine.config["entry"]["reversal_enabled"] = False
        self.assertEqual(self.engine.normal_entry_cap(), 3)


class ConfirmedSideSwitchTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, mode="paper"):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        config["entry"].update(
            reversal_enabled=True,
            reversal_live_enabled=False,
            reversal_min_confidence=0.72,
            reversal_min_expected_roi=0.04,
            reversal_confirmation_seconds=0.0,
            reversal_min_observations=1,
            max_side_switches=1,
        )
        engine = object.__new__(Engine)
        engine.config = config
        engine.runtime = SimpleNamespace(
            mode=mode,
            snapshot=lambda: {
                "max_per_trade_usd": 5.0,
                "max_per_window_usd": 25.0,
                "daily_cap_usd": 300.0,
            },
        )
        engine.stats = EngineStats()
        engine._anchors = {}
        engine._reversal_candidates = {}
        engine.risk = SimpleNamespace(
            min_trade_usd=5.0,
            size_for=MagicMock(return_value=5.0),
            check_entry=MagicMock(
                return_value=SimpleNamespace(allowed=True, size_usd=5.0)
            ),
            record_entry=MagicMock(),
        )
        engine.store = SimpleNamespace(
            window_exposure=MagicMock(return_value=5.0),
            interval_exposure=MagicMock(return_value=5.0),
            exposure_since=MagicMock(return_value=5.0),
            stats=MagicMock(return_value={"total_pnl": 0.0}),
            open_position=MagicMock(return_value=2),
            log=MagicMock(),
        )
        engine._apply_market_window_cap = MagicMock(return_value=(5.0, None))
        engine._apply_hard_window_cap = MagicMock(return_value=(5.0, None))
        engine._apply_bankroll_cap = MagicMock(return_value=(5.0, None))
        engine._execute_buy = AsyncMock(
            return_value=SimpleNamespace(
                avg_price=0.31,
                shares=15.8,
                stake_usd=5.0,
                fee_usd=0.1,
                ts=100.0,
            )
        )
        return engine

    @staticmethod
    def _market():
        return SimpleNamespace(
            asset="btc",
            slug="btc-updown-5m-0",
            window="5m",
            end=300.0,
            question="Bitcoin Up or Down?",
        )

    @staticmethod
    def _signal(expected_roi=0.10):
        return SimpleNamespace(
            side="down",
            confidence=0.80,
            expected_roi=expected_roi,
            entry_price=0.32,
            ask_depth_usd=100.0,
            anchor_price=100.0,
        )

    def test_requires_three_observations_spanning_six_seconds(self):
        engine = self._engine()
        engine.config["entry"].update(
            reversal_confirmation_seconds=6.0,
            reversal_min_observations=3,
        )

        self.assertFalse(engine._reversal_confirmed("btc-5m", "down", 100.0))
        self.assertFalse(engine._reversal_confirmed("btc-5m", "down", 103.0))
        self.assertTrue(engine._reversal_confirmed("btc-5m", "down", 106.0))

    def test_direction_change_restarts_confirmation(self):
        engine = self._engine()
        engine.config["entry"].update(
            reversal_confirmation_seconds=6.0,
            reversal_min_observations=3,
        )
        engine._reversal_confirmed("btc-5m", "down", 100.0)
        engine._reversal_confirmed("btc-5m", "down", 103.0)

        self.assertFalse(engine._reversal_confirmed("btc-5m", "up", 106.0))
        self.assertFalse(engine._reversal_confirmed("btc-5m", "up", 109.0))
        self.assertTrue(engine._reversal_confirmed("btc-5m", "up", 112.0))

    async def test_switch_uses_normal_clip_instead_of_equal_share_hedge(self):
        engine = self._engine()
        position = {"id": 1, "side": "up", "shares": 100.0}

        await engine._maybe_reverse(
            self._market(), self._signal(), position, side_switches=0, now=100.0
        )

        engine.risk.size_for.assert_called_once_with(0.80, 5.0)
        engine._execute_buy.assert_awaited_once_with(
            self._market(), "down", 5.0, 0.32
        )
        opened = engine.store.open_position.call_args.kwargs
        self.assertEqual(opened["stake_usd"], 5.0)
        self.assertEqual(opened["switches"], 1)

    async def test_live_switch_stays_disabled_without_paper_evidence(self):
        engine = self._engine(mode="live")

        await engine._maybe_reverse(
            self._market(), self._signal(), {"side": "up"}, 0, 100.0
        )

        engine._execute_buy.assert_not_awaited()
        self.assertEqual(
            engine.stats.rejections.get("reversal awaiting Paper evidence for Live"),
            1,
        )

    async def test_switch_requires_positive_fee_adjusted_edge(self):
        engine = self._engine()

        await engine._maybe_reverse(
            self._market(), self._signal(expected_roi=0.039), {"side": "up"}, 0, 100.0
        )

        engine._execute_buy.assert_not_awaited()
        self.assertEqual(
            engine.stats.rejections.get("reversal expected ROI below threshold"), 1
        )

    def test_window_switch_count_does_not_reset_on_later_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "switches.db")
            common = {
                "mode": "paper",
                "slug": "btc-5m",
                "asset": "btc",
                "window": "5m",
                "side": "up",
                "confidence": 0.8,
                "entry_price": 0.5,
                "shares": 10.0,
                "stake_usd": 5.0,
                "anchor_price": 100.0,
                "opened_at": 1.0,
                "window_end": 300.0,
            }
            store.open_position(**common, switches=1)
            store.open_position(**{**common, "opened_at": 2.0, "switches": 0})

            self.assertEqual(
                store.side_switches_for_window("paper", "btc-5m"), 1
            )
            store.close()


class SignalRetentionTests(unittest.TestCase):
    def test_signal_is_deleted_at_exactly_24_hours(self):
        engine = object.__new__(Engine)
        engine.latest_signals = {
            "expired": {"observed_at": 100.0},
            "fresh": {"observed_at": 100.001},
        }

        removed = engine.prune_signals(100.0 + SIGNAL_RETENTION_SECONDS)

        self.assertEqual(removed, 1)
        self.assertEqual(set(engine.latest_signals), {"fresh"})


class WindowExposureTests(unittest.TestCase):
    def test_interval_cap_combines_assets_but_not_different_intervals(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "limits.db")
            common = {
                "mode": "live",
                "side": "up",
                "confidence": 0.7,
                "entry_price": 0.5,
                "shares": 10.0,
                "anchor_price": 100.0,
                "opened_at": 50.0,
                "switches": 0,
            }
            btc = store.open_position(
                **common, slug="btc-5m", asset="btc", window="5m",
                stake_usd=5.0, window_end=300.0,
            )
            store.open_position(
                **common, slug="xrp-5m", asset="xrp", window="5m",
                stake_usd=7.0, window_end=300.0,
            )
            store.open_position(
                **common, slug="sol-next-5m", asset="sol", window="5m",
                stake_usd=9.0, window_end=600.0,
            )
            store.open_position(
                **common, slug="eth-15m", asset="eth", window="15m",
                stake_usd=11.0, window_end=300.0,
            )

            self.assertEqual(store.interval_exposure("live", "5m", 300.0), 12.0)
            self.assertEqual(store.interval_exposure("live", "5m", 600.0), 9.0)
            self.assertEqual(store.interval_exposure("live", "15m", 300.0), 11.0)
            self.assertEqual(store.open_exposure("live"), 32.0)

            store.close_position(btc)
            self.assertEqual(store.interval_exposure("live", "5m", 300.0), 7.0)
            self.assertEqual(store.open_exposure("live"), 27.0)
            store.close()


class DrawdownCooldownTests(unittest.TestCase):
    def setUp(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.risk = RiskManager(config)
        self.risk.dd_cooldown = 10.0
        self.risk.dd_max_usd = 100.0
        self.losses = [0.0, -30.0, -30.0, -30.0, -30.0, -30.0]

    def test_expired_cooldown_does_not_retrip_on_same_drawdown(self):
        self.risk.update_hourly_loss(self.losses, now=100.0)
        self.assertEqual(self.risk.breaker_until, 110.0)

        self.risk.update_hourly_loss(self.losses, now=110.0)

        self.assertEqual(self.risk.breaker_until, 0.0)
        self.assertIsNone(self.risk.breaker_reason)

    def test_worse_drawdown_does_not_restart_expired_cooldown(self):
        self.risk.update_hourly_loss(self.losses, now=100.0)
        worse = [0.0, -30.0, -30.0, -30.0, -30.0, -40.0]

        self.risk.update_hourly_loss(worse, now=110.0)

        self.assertEqual(self.risk.breaker_until, 0.0)
        self.assertIsNone(self.risk.breaker_reason)

    def test_recovery_rearms_breaker_for_a_new_breach(self):
        self.risk.update_hourly_loss(self.losses, now=100.0)
        self.risk.update_hourly_loss([-99.0], now=110.0)

        self.risk.update_hourly_loss([-101.0], now=111.0)

        self.assertEqual(self.risk.breaker_until, 121.0)

    def test_hourly_net_loss_below_limit_does_not_trip(self):
        self.risk.update_hourly_loss([-120.0, 30.01], now=100.0)

        self.assertEqual(self.risk.breaker_until, 0.0)


class ChainlinkRuntimeCredentialsTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_creates_client_when_credentials_added_at_runtime(self):
        response = {
            "feeds": [{
                "feedID": MAINNET_FEED_IDS["btc"],
            }]
        }

        async def handler(request):
            self.assertEqual(request.headers["Authorization"], "client-id")
            return httpx.Response(200, json=response)

        stream = ChainlinkStreams(["btc"])
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def ensure_client():
            stream._client = client
            return client

        stream._ensure_client = ensure_client
        with patch.dict(
            os.environ,
            {
                "CHAINLINK_API_KEY": "client-id",
                "CHAINLINK_API_SECRET": "client-secret",
            },
        ):
            self.assertTrue(await stream.discover_feeds())
            self.assertIn("btc", stream.feeds)

        await client.aclose()

    def test_decodes_production_v3_full_report(self):
        from eth_abi import encode

        feed_id = bytes.fromhex(MAINNET_FEED_IDS["btc"][2:])
        blob = encode(
            [
                "bytes32", "uint32", "uint32", "uint192", "uint192",
                "uint32", "int192", "int192", "int192",
            ],
            [feed_id, 1, 2, 0, 0, 3, 123_456 * WEI, 0, 0],
        )
        envelope = encode(
            ["bytes32[3]", "bytes", "bytes32[]", "bytes32[]", "bytes32"],
            [[b"\0" * 32] * 3, blob, [], [], b"\0" * 32],
        )

        price = _decode_price({
            "feedID": MAINNET_FEED_IDS["btc"],
            "fullReport": "0x" + envelope.hex(),
        })

        self.assertEqual(price, 123_456.0)


if __name__ == "__main__":
    unittest.main()
