"""The 2026-08-15 03:00 UTC incident: one buffer, two price feeds.

Chainlink Data Streams prices were being pushed into the same tick buffer that
Coinbase and Kraken poll into, on the theory that the resolution source should
"become the buffer's source of truth". Nothing stopped the exchange pollers, so
the buffer interleaved two series that sit a real basis apart — dollars, on BTC.

Every read then became a coin flip between two different prices, and every
subtraction across them measured the basis instead of the market:

* ``drift = ln(spot / anchor)`` picked its numerator and denominator from
  different feeds. Over a short remaining horizon ``sigma_rem`` is small enough
  that a few basis points of that saturates the model, so it reported 76%
  confidence on the side the book was pricing at 2c — and scored the
  disagreement as +920% expected ROI rather than as evidence it was wrong.
* Settlement compared an exchange anchor (63056.5) against a Chainlink close
  (63067.52805979524) and booked the $11.03 between them as a win: +$228.51
  recorded on a position the venue resolved as a loss.

Both halves are covered here — the separation itself, and the price floor that
should have refused the trade even with the feed broken.
"""

import time
import unittest

from polybot.feeds.spot import EXCHANGE, RESOLUTION, AssetState
from polybot.strategy import SANE_MAX_EDGE, AntsaslykuStrategy

# The real window, rebased onto "just now" so the buffer's own 1200s retention
# does not prune the fixture out from under the test.
WINDOW_END = time.time() - 1.0
WINDOW_START = WINDOW_END - 300.0

# Prices as recorded. The exchange series drifts 10c down across the window —
# UP lost, and the venue resolved it DOWN. The Chainlink series does the same
# thing $11 higher: that gap is the basis, and it is not a price move.
EXCHANGE_OPEN = 63056.5
EXCHANGE_CLOSE = 63056.4
CHAINLINK_CLOSE = 63067.52805979524
BASIS = CHAINLINK_CLOSE - EXCHANGE_CLOSE


def _btc() -> AssetState:
    """The buffer as it stood: both feeds live, a basis apart."""
    state = AssetState("btc")
    state.push(EXCHANGE_OPEN, WINDOW_START - 1.0, EXCHANGE)
    state.push(EXCHANGE_OPEN + BASIS, WINDOW_START - 0.5, RESOLUTION)
    for offset in range(1, 300, 10):
        drift = EXCHANGE_OPEN - offset * (0.1 / 300.0)
        state.push(drift, WINDOW_START + offset, EXCHANGE)
        state.push(drift + BASIS, WINDOW_START + offset + 0.4, RESOLUTION)
    state.push(EXCHANGE_CLOSE, WINDOW_END - 0.6, EXCHANGE)
    state.push(CHAINLINK_CLOSE, WINDOW_END - 0.2, RESOLUTION)
    return state


class SeriesSeparationTests(unittest.TestCase):
    def setUp(self):
        self.state = _btc()

    def test_a_source_read_never_returns_the_other_feeds_price(self):
        self.assertEqual(
            self.state.price_at_or_before(WINDOW_END, RESOLUTION), CHAINLINK_CLOSE
        )
        self.assertEqual(
            self.state.price_at_or_before(WINDOW_END, EXCHANGE), EXCHANGE_CLOSE
        )

    def test_the_unscoped_read_is_what_mixed_them(self):
        """The old call site took whichever feed happened to tick last."""
        blended = self.state.price_at_or_before(WINDOW_END)
        self.assertEqual(blended, CHAINLINK_CLOSE)
        # Anchored on the exchange open, that close is an $11 phantom rally on
        # a window that actually finished 10c down.
        self.assertGreater(blended - EXCHANGE_OPEN, 11.0)
        self.assertLess(
            self.state.price_at_or_before(WINDOW_END, EXCHANGE) - EXCHANGE_OPEN, 0.0
        )

    def test_settling_within_one_series_reaches_the_venues_verdict(self):
        for source in (EXCHANGE, RESOLUTION):
            with self.subTest(source=source):
                anchor = self.state.price_at_or_before(WINDOW_START, source)
                close = self.state.price_at_or_before(WINDOW_END, source)
                self.assertIsNotNone(anchor)
                self.assertIsNotNone(close)
                self.assertLess(close, anchor, "UP must lose on either series")

    def test_latest_is_tracked_per_series(self):
        self.assertEqual(self.state.latest_from(EXCHANGE)[1], EXCHANGE_CLOSE)
        self.assertEqual(self.state.latest_from(RESOLUTION)[1], CHAINLINK_CLOSE)

    def test_volatility_is_not_measured_across_the_basis(self):
        """Interleaved feeds oscillate around their basis; that is not vol."""
        blended = self.state.resample(400.0)
        scoped = self.state.resample(400.0, source=EXCHANGE)
        self.assertGreater(len(blended), 0)
        self.assertGreater(len(scoped), 0)
        self.assertGreater(max(blended) - min(blended), max(scoped) - min(scoped))

    def test_untagged_pushes_stay_exchange_prices(self):
        state = AssetState("btc")
        state.push(100.0, WINDOW_END)
        self.assertEqual(state.price_at_or_before(WINDOW_END, EXCHANGE), 100.0)
        self.assertIsNone(state.price_at_or_before(WINDOW_END, RESOLUTION))


class PricingSourceTests(unittest.TestCase):
    def test_exchange_wins_because_it_leads(self):
        state = _btc()
        self.assertEqual(
            state.pricing_source(WINDOW_START, WINDOW_END, 15.0), EXCHANGE
        )

    def test_a_stream_that_missed_the_open_is_not_eligible(self):
        """Chainlink connecting mid-window must not anchor that window."""
        state = AssetState("btc")
        state.push(EXCHANGE_OPEN, WINDOW_START - 1.0, EXCHANGE)
        state.push(EXCHANGE_CLOSE, WINDOW_END - 1.0, EXCHANGE)
        state.push(CHAINLINK_CLOSE, WINDOW_END - 1.0, RESOLUTION)
        self.assertEqual(
            state.pricing_source(WINDOW_START, WINDOW_END, 15.0), EXCHANGE
        )

    def test_none_when_neither_series_covers_both_ends(self):
        state = AssetState("btc")
        state.push(EXCHANGE_OPEN, WINDOW_END - 1.0, EXCHANGE)
        self.assertIsNone(state.pricing_source(WINDOW_START, WINDOW_END, 15.0))

    def test_a_stale_series_is_refused_even_though_it_covers_the_open(self):
        state = AssetState("btc")
        state.push(EXCHANGE_OPEN, WINDOW_START - 1.0, EXCHANGE)
        self.assertIsNone(state.pricing_source(WINDOW_START, WINDOW_END, 15.0))


class LongshotEdgeCapTests(unittest.TestCase):
    """The backstop that should have refused the trade regardless.

    Our probability and the book's price are two estimates of the same
    quantity. A 74-point disagreement is not a 50x edge; it is a broken feed.

    The replicated strategy trades the whole 0.02-0.85 band on purpose — the
    cloned wallet's cheapest entries are its most profitable — so a bare price
    floor can no longer be the guard. ``max_model_edge`` is: the wallet's
    largest measured edge across 101,541 windows was +0.054, so a claimed edge
    past 0.25 is treated as evidence the feed is wrong.
    """

    def _strategy(self, **cfg):
        return AntsaslykuStrategy({
            "fees": {"taker_rate": 0.07, "exponent": 1.0},
            "strategy": {"min_depth_usd": 50.0, **cfg},
        })

    def _intent(self, strategy, price, confidence=0.7572202884005876):
        """The 03:04:10 entry exactly: 76% confident, book asking 2c."""
        from polybot.strategy import Intent
        intent = Intent(
            slug="btc-updown-5m-1786762800", asset="btc", window="5m", side="up",
            size_usd=5.0, limit_price=price, stage="open", fill_index=0,
            confidence=confidence,
            edge=confidence - strategy.all_in_cost(price),
            drift_bps=1.7, seconds_into_window=250.8, ask_depth_usd=400.0,
        )
        return intent

    def _market(self):
        class _M:
            start, end = WINDOW_START, WINDOW_END
            accepting_orders, closed = True, False

            def seconds_elapsed(self, now):
                return now - self.start
        return _M()

    def _reason(self, strategy, price, confidence=0.7572202884005876):
        from polybot.signal import Read
        intent = self._intent(strategy, price, confidence)
        read = Read(
            p_up=confidence, drift=0.00017, sigma_remaining=0.0004,
            anchor_price=63056.5, spot_price=63067.5, source=EXCHANGE,
            seconds_remaining=49.2, seconds_elapsed=250.8,
        )
        return strategy._reject_reason(
            intent, self._market(), strategy.state_for(intent.slug),
            read, time.time(),
        )

    def test_the_2c_longshot_is_refused_as_an_implausible_edge(self):
        strategy = self._strategy()
        reason = self._reason(strategy, 0.02)
        self.assertIsNotNone(reason)
        self.assertIn("implausible", reason)

    def test_the_other_two_fills_from_that_session_are_refused_too(self):
        strategy = self._strategy()
        for price in (0.03, 0.04):
            with self.subTest(price=price):
                self.assertIn("implausible", self._reason(strategy, price) or "")

    def test_a_cheap_entry_the_model_agrees_with_is_allowed(self):
        """The band exists to be used: 5c at 8% confidence is the wallet's own
        most profitable cohort (+51.65% ROI) and must not be blocked."""
        strategy = self._strategy()
        self.assertIsNone(self._reason(strategy, 0.05, confidence=0.09))

    def test_the_ceiling_still_binds_from_the_other_direction(self):
        strategy = self._strategy()
        reason = self._reason(strategy, 0.90, confidence=0.95)
        self.assertIn("above price ceiling", reason or "")

    def test_a_normal_coin_flip_entry_still_trades(self):
        strategy = self._strategy()
        self.assertIsNone(self._reason(strategy, 0.54, confidence=0.57))

    def test_the_edge_cap_cannot_be_configured_away(self):
        """Why the guard had to change, and why it is not a mere default.

        The old profile refused this fill with a 0.30 price floor. This
        strategy deliberately buys down to 0.02 — the cloned wallet's cheapest
        entries are its most profitable — so the floor is gone and the edge cap
        is the only thing between a broken feed and the trade. It is therefore
        clamped at SANE_MAX_EDGE rather than merely defaulted there: config
        cannot raise it, and the 2c fill stays refused.
        """
        strategy = self._strategy(max_model_edge=5.0)
        self.assertEqual(strategy.max_model_edge, SANE_MAX_EDGE)
        self.assertIn("implausible", self._reason(strategy, 0.02) or "")


if __name__ == "__main__":
    unittest.main()
