"""The @antsaslyku replication, pinned to what the wallet actually does.

Each test names the measurement it protects. The numbers come from a complete
scrape of wallet 0x3c58...776b: 654,200 activity rows over 113 days, 569,015
fills, 101,541 windows reconstructed to a known winner. See polybot/strategy.py
for the full derivation.
"""

import time
import unittest
from types import SimpleNamespace

from polybot.feeds.polymarket import Book, BookSide, MarketWindow
from polybot.feeds.spot import EXCHANGE, AssetState
from polybot.strategy import SANE_MAX_EDGE, AntsaslykuStrategy, WindowState

# Anchored to real time: AssetState prunes its tick buffer against
# ``time.time()``, so a fixture pinned to a fixed past timestamp is pruned away
# before the model ever sees it.
NOW = time.time()
START = NOW - 60.0
END = START + 300.0


def market(asset="btc", window="5m", up_ask=0.54, down_ask=0.48, depth=200.0):
    m = MarketWindow(
        asset=asset, window=window, start=int(START), end=int(END),
        slug=f"{asset}-updown-{window}-{int(START)}",
        condition_id="0xtest", question="test", token_up="1", token_down="2",
    )
    for side, ask in (("up", up_ask), ("down", down_ask)):
        m.books[side] = Book(
            asks=BookSide([(ask, depth / max(ask, 0.01))]),
            bids=BookSide([(max(0.01, ask - 0.02), depth / max(ask, 0.01))]),
            ts=NOW,
        )
    return m


def spot(drift_ratio=1.0005, now=NOW, wobble=0.0001):
    """A price series anchored at START and drifting to ``drift_ratio`` by ``now``.

    ``wobble`` keeps sigma non-zero so ``per_second_sigma`` can form an estimate;
    without it the model divides by zero and declines to have a view at all.
    """
    state = AssetState("btc")
    base = 60000.0
    # Pre-window history, for the volatility estimate only.
    t = START - 900.0
    i = 0
    while t < START:
        state.push(base * (1.0 + wobble * ((i % 7) - 3)), t, EXCHANGE)
        t += 5.0
        i += 1
    # The anchor, then a linear drift up to `now`.
    state.push(base, START, EXCHANGE)
    span = max(1.0, now - START)
    t = START + 5.0
    while t < now:
        frac = (t - START) / span
        state.push(
            base * (1.0 + (drift_ratio - 1.0) * frac + wobble * ((i % 7) - 3)),
            t, EXCHANGE,
        )
        t += 5.0
        i += 1
    state.push(base * drift_ratio, now - 0.5, EXCHANGE)
    state.sources.add(EXCHANGE)
    return state


def build(**cfg):
    return AntsaslykuStrategy({
        "fees": {"taker_rate": 0.07, "exponent": 1.0},
        "strategy": {"min_depth_usd": 10.0, **cfg},
    })


class UniverseTests(unittest.TestCase):
    """The wallet touches btc/eth/sol/xrp on 5m and 15m. Nothing else."""

    def test_an_unlisted_asset_is_never_scored(self):
        s = build()
        self.assertIsNone(s.evaluate(market(asset="doge"), spot(), NOW))

    def test_an_unlisted_window_is_never_scored(self):
        s = build()
        self.assertIsNone(s.evaluate(market(window="1h"), spot(), NOW))

    def test_the_four_measured_assets_are_all_scored(self):
        s = build()
        for asset in ("btc", "eth", "sol", "xrp"):
            with self.subTest(asset=asset):
                self.assertIsNotNone(s.evaluate(market(asset=asset), spot(), NOW))


class SizingTests(unittest.TestCase):
    """Median fill is $5.04 at EVERY ladder index from 1 to 26.

    An earlier build of this repo modelled a $60 'escalation clip' at fill 8.
    Over 113 days that is measurably false, and these tests exist so it cannot
    quietly come back.
    """

    def test_the_clip_is_flat_five_dollars(self):
        s = build()
        self.assertEqual(s.base_clip_usd, 5.0)
        self.assertEqual(s.evaluate(market(), spot(), NOW).size_usd, 5.0)

    def test_the_clip_does_not_escalate_with_ladder_depth(self):
        s = build()
        m, st = market(), None
        sizes = []
        for i in range(12):
            intent = s.evaluate(m, spot(), NOW)
            sizes.append(intent.size_usd)
            st = s.record_fill(m.slug, intent.side, intent.size_usd, 10.0,
                               intent.stage, NOW - 30 + i)
        self.assertEqual(set(sizes), {5.0}, "clip size must not vary by fill index")

    def test_risk_multiplier_scales_the_clip(self):
        s = build(risk_multiplier=0.4)
        self.assertAlmostEqual(s.clip_for(0.55), 2.0)

    def test_the_venue_minimum_outranks_the_risk_multiplier(self):
        """A shrunken clip still cannot be smaller than 5 shares."""
        s = build(risk_multiplier=0.4)
        intent = s.evaluate(market(), spot(), NOW)
        # 5 shares at the 55c limit price is $2.75, above the scaled $2.00.
        self.assertAlmostEqual(intent.size_usd, 2.75)


class PriceScaledClipTests(unittest.TestCase):
    """The $5 clip is only the ORDINARY-price clip.

    Measured median fill size by price paid across all 569,015 fills:
    under 2c $0.05, 2-5c $1.20, 5-10c $1.54, 10-20c $3.05, $5.00+ from 30c up.
    The wallet stakes dust on longshots.

    Observed 2026-08-15: a flat $5 clip bought 467 shares at 1c on three
    separate windows and lost all three — a ~100x over-size against what the
    wallet actually does there.
    """

    def test_the_clip_follows_the_measured_ladder(self):
        s = build()
        for price, expected in (
            (0.01, 0.05), (0.03, 1.20), (0.07, 1.55),
            (0.15, 3.05), (0.35, 5.0), (0.55, 5.0), (0.80, 5.0),
        ):
            with self.subTest(price=price):
                self.assertAlmostEqual(s.clip_for(price), expected)

    def test_a_longshot_is_not_bought_with_a_full_clip(self):
        s = build()
        m = market(up_ask=0.01, down_ask=0.97)
        intent = s.evaluate(m, spot(drift_ratio=0.999), NOW)
        self.assertLess(intent.size_usd, 1.50,
                        "a cent-priced longshot must not take a $5 clip")

    def test_disabling_the_ladder_restores_a_flat_clip(self):
        s = build(price_scaled_clips=False)
        self.assertAlmostEqual(s.clip_for(0.01), 5.0)

    def test_the_venue_minimum_is_five_shares_not_five_dollars(self):
        """orderMinSize is a share count. At 60c that is $3.00, which is why
        the wallet's activity shows $3.65 and $4.17 fills."""
        s = build()
        self.assertAlmostEqual(s.min_stake_for(0.60), 3.00)
        self.assertAlmostEqual(s.min_stake_for(0.85), 4.25)
        self.assertAlmostEqual(s.min_stake_for(0.01), 0.05)

    def test_sub_five_dollar_orders_are_allowed(self):
        """The wallet's activity is full of $3.65 and $4.17 fills."""
        s = build()
        # Drift down hard, so the cheap 'up' side is the one with the edge.
        m = market(up_ask=0.14, down_ask=0.95)
        intent = s.evaluate(m, spot(drift_ratio=0.9985), NOW)
        self.assertEqual(intent.side, "up")
        self.assertLess(intent.size_usd, 5.0)
        self.assertGreaterEqual(
            intent.size_usd, s.min_stake_for(intent.limit_price) - 1e-9
        )

    def test_the_clip_never_falls_below_the_venue_minimum(self):
        """5 shares at 85c is $4.25, above the ladder's own figure."""
        s = build(clip_under_2c_usd=0.001)
        self.assertGreaterEqual(s.clip_for(0.01) or 0, 0.0)
        m = market(up_ask=0.84, down_ask=0.14)
        intent = s.evaluate(m, spot(drift_ratio=1.0012), NOW)
        self.assertGreaterEqual(
            intent.size_usd, s.min_stake_for(intent.limit_price) - 1e-9
        )


class WindowMixTests(unittest.TestCase):
    """86.3% of the wallet's fills are 5m; only 13.7% are 15m.

    It entered 82,677 distinct 5m windows against 18,975 15m ones (4.36:1) and
    averaged 5.94 fills per 5m window against 4.11 per 15m. Left uncapped, 15m
    positions stay open three times longer, sit on the exposure budget and
    starve the 5m windows that carry most of the activity.
    """

    def test_the_fill_cap_is_lower_on_fifteen_minute_windows(self):
        s = build()
        self.assertEqual(s.max_fills_by_window["5m"], 40)
        self.assertEqual(s.max_fills_by_window["15m"], 28)
        self.assertAlmostEqual(
            s.max_fills_by_window["15m"] / s.max_fills_by_window["5m"],
            4.11 / 5.94, places=1,
        )

    def test_concurrent_fifteen_minute_windows_are_capped(self):
        s = build(max_open_windows_15m=2)
        for i in range(2):
            s.record_fill(f"btc-updown-15m-{i}", "up", 5.0, 10.0, "open", NOW - 60)
        intent = s.evaluate(market(window="15m"), spot(), NOW)
        self.assertIn("max open 15m windows", intent.reason)

    def test_five_minute_windows_are_unaffected_by_that_cap(self):
        s = build(max_open_windows_15m=1)
        s.record_fill("btc-updown-15m-1", "up", 5.0, 10.0, "open", NOW - 60)
        self.assertIsNone(s.evaluate(market(window="5m"), spot(), NOW).reason)


class PriceBandTests(unittest.TestCase):
    """Edge is positive from 0.02 to ~0.80 and gone by 0.85."""

    def test_the_band_is_the_measured_one(self):
        s = build()
        self.assertEqual(s.min_entry_price, 0.02)
        self.assertEqual(s.max_entry_price, 0.85)

    def test_an_ask_above_the_ceiling_is_refused(self):
        s = build()
        intent = s.evaluate(market(up_ask=0.90, down_ask=0.90), spot(), NOW)
        self.assertIn("above price ceiling", intent.reason)

    def test_a_cheap_underpriced_side_is_bought_against_the_drift(self):
        """The wallet's most profitable cohort is its cheap entries.

        0.05-0.10 returned +51.65% ROI: it paid an average 0.074 and won 12.8%
        of the time. Those are bets AGAINST the prevailing drift, so a
        favourite-only rule could never place them.
        """
        s = build()
        # Drift down hard enough that the model gives 'up' about 10% — and the
        # book is asking 5c for it. That is a ~4-point edge on a longshot,
        # which is the shape of the wallet's own cheap cohort.
        m = market(up_ask=0.05, down_ask=0.93)
        intent = s.evaluate(m, spot(drift_ratio=0.9985), NOW)
        self.assertEqual(intent.side, "up", "must buy the underpriced cheap side")
        self.assertIsNone(intent.reason, intent.reason)
        self.assertLess(intent.confidence, 0.2)
        self.assertLess(intent.edge, 0.10, "a real longshot edge is small")


class EdgeCapTests(unittest.TestCase):
    """The wallet's largest measured edge in 101,541 windows was +0.054."""

    def test_an_implausible_edge_is_refused(self):
        s = build()
        # A huge drift saturates the model to near-certainty while the book
        # still asks 3c — the exact shape of a broken feed.
        intent = s.evaluate(market(up_ask=0.03, down_ask=0.97),
                            spot(drift_ratio=1.02), NOW)
        self.assertIsNotNone(intent.reason)
        self.assertIn("implausible", intent.reason)

    def test_the_cap_cannot_be_raised_by_config(self):
        self.assertEqual(build(max_model_edge=9.0).max_model_edge, SANE_MAX_EDGE)


class TimingTests(unittest.TestCase):
    """First fill p10 7s / p50 53s / p90 291s; earliest observed 2s.

    Entries run the whole window — first-fill ROI is HIGHEST in the last decile
    (+16.19%), which is why max_entry_window_fraction is 1.0.
    """

    def test_too_early_is_refused(self):
        s = build()
        at = START + 1.0
        intent = s.evaluate(market(), spot(now=at), at)
        self.assertIn("into window", intent.reason)

    def test_two_seconds_in_is_allowed(self):
        s = build()
        self.assertEqual(s.min_entry_offset_seconds, 2.0)

    def test_the_last_decile_of_the_window_still_trades(self):
        s = build()
        self.assertEqual(s.max_entry_window_fraction, 1.0)
        at = END - 20.0
        # With 20s left the same drift is a far stronger signal, so the fixture
        # uses a gentler one to stay in the plausible-edge range.
        intent = s.evaluate(market(), spot(drift_ratio=1.0002, now=at), at)
        self.assertIsNone(intent.reason, intent.reason)

    def test_the_final_seconds_are_refused(self):
        s = build()
        at = END - 2.0
        intent = s.evaluate(market(), spot(now=at), at)
        self.assertIn("left", intent.reason)


class LadderTests(unittest.TestCase):
    """Mean 5.6 fills/window, median 3, p90 13."""

    def test_the_first_fill_is_an_open(self):
        s = build()
        self.assertEqual(s.evaluate(market(), spot(), NOW).stage, "open")

    def test_a_same_side_repeat_is_an_add(self):
        s = build()
        m = market()
        first = s.evaluate(m, spot(), NOW)
        s.record_fill(m.slug, first.side, 5.0, 10.0, "open", NOW - 30)
        self.assertEqual(s.evaluate(m, spot(), NOW).stage, "add")

    def test_the_fill_cap_stops_the_ladder(self):
        s = build(max_fills_per_window=2)
        m = market()
        for i in range(2):
            intent = s.evaluate(m, spot(), NOW)
            s.record_fill(m.slug, intent.side, 5.0, 10.0, intent.stage, NOW - 30)
        self.assertIn("fill cap", s.evaluate(m, spot(), NOW).reason)

    def test_cadence_blocks_a_burst(self):
        s = build()
        m = market()
        first = s.evaluate(m, spot(), NOW)
        s.record_fill(m.slug, first.side, 5.0, 10.0, "open", NOW)
        self.assertIn("cadence", s.evaluate(m, spot(), NOW).reason)


class HedgeTests(unittest.TestCase):
    """52.0% of the wallet's windows hedge, at a median combined $1.042.

    That is above $1.00, so the second leg is a rescue, not a lock — only 35.6%
    of its 52,816 hedges came in under $1.00. These tests hold both the shipped
    literal-clone behaviour and the lock-only alternative.
    """

    def _hedged(self, s):
        """Open 'up', then flip the drift so 'down' becomes the model's side."""
        m = market()
        first = s.evaluate(m, spot(drift_ratio=1.0005), NOW)
        self.assertEqual(first.side, "up")
        s.record_fill(m.slug, "up", 5.0, 9.0, "open", NOW - 30)
        down = spot(drift_ratio=0.9995)
        # The hedge needs a persistent flip, not one tick.
        for _ in range(s.hedge_after_adverse_ticks):
            intent = s.evaluate(m, down, NOW)
        return m, intent

    def test_a_flip_is_staged_as_a_hedge_not_a_new_open(self):
        _, intent = self._hedged(build())
        self.assertEqual(intent.stage, "hedge")
        self.assertEqual(intent.side, "down")

    def test_the_shipped_default_allows_the_hedge(self):
        """Operator decision 2026-08-15: reproduce the wallet literally."""
        s = build()
        self.assertTrue(s.hedge_enabled)
        self.assertEqual(s.hedge_max_combined_cost, 99.0)
        _, intent = self._hedged(s)
        self.assertIsNone(intent.reason, intent.reason)

    def test_disabling_the_hedge_refuses_the_second_leg(self):
        _, intent = self._hedged(build(hedge_enabled=False))
        self.assertIn("hedging disabled", intent.reason)

    def test_a_lock_only_cap_refuses_a_pair_costing_over_a_dollar(self):
        """hedge_max_combined_cost=1.0 admits only genuine arbitrage."""
        s = build(hedge_max_combined_cost=1.0)
        m = market()
        first = s.evaluate(m, spot(drift_ratio=1.0005), NOW)
        # Held at 60c; the opposite side is asking 55c, so the pair costs 1.15.
        s.record_fill(m.slug, "up", 6.0, 10.0, "open", NOW - 30)
        m.books["down"] = Book(
            asks=BookSide([(0.54, 400.0)]), bids=BookSide([(0.52, 400.0)]), ts=NOW
        )
        down = spot(drift_ratio=0.9995)
        for _ in range(s.hedge_after_adverse_ticks):
            intent = s.evaluate(m, down, NOW)
        self.assertIn("combined cost", intent.reason)

    def test_it_switches_sides_repeatedly_not_just_once(self):
        """Measured: 52,816 two-sided windows, mean 2.61 direction changes,
        median 2, max 35, and 18,740 windows with three or more. The wallet
        does not hedge once and stop — it flips back and forth as the drift
        moves, so the clone must be able to as well."""
        s = build()
        m = market()
        first = s.evaluate(m, spot(drift_ratio=1.0005), NOW)
        s.record_fill(m.slug, first.side, 5.0, 9.0, "open", NOW - 120)

        up, down = spot(drift_ratio=1.0005), spot(drift_ratio=0.9995)
        switches = 0
        for i in range(6):
            series = down if i % 2 == 0 else up
            intent = None
            # Two consecutive adverse reads arm the switch.
            for _ in range(s.hedge_after_adverse_ticks):
                intent = s.evaluate(m, series, NOW)
            if intent and intent.tradeable:
                s.record_fill(m.slug, intent.side, 5.0, 9.0, intent.stage,
                              NOW - 100 + i * 10)
                switches += 1
        self.assertGreaterEqual(
            switches, 3, "the clone must be able to switch sides more than once"
        )

    def test_a_single_confirming_scan_arms_the_switch(self):
        """Observed live: XRP 1:25-1:30 flipped four times inside a minute.
        At a 3s poll a two-tick confirmation costs 6s per switch, which is most
        of the move on a 300s window."""
        s = build()
        self.assertEqual(s.hedge_after_adverse_ticks, 1)
        m = market()
        s.evaluate(m, spot(drift_ratio=1.0005), NOW)
        s.record_fill(m.slug, "up", 5.0, 9.0, "open", NOW - 30)
        intent = s.evaluate(m, spot(drift_ratio=0.9995), NOW)
        self.assertEqual(intent.stage, "hedge")
        self.assertIsNone(intent.reason, intent.reason)

    def test_a_confirmation_can_still_be_required(self):
        s = build(hedge_after_adverse_ticks=2)
        m = market()
        s.evaluate(m, spot(drift_ratio=1.0005), NOW)
        s.record_fill(m.slug, "up", 5.0, 9.0, "open", NOW - 30)
        intent = s.evaluate(m, spot(drift_ratio=0.9995), NOW)
        self.assertIn("adverse streak", intent.reason)

    def test_once_both_sides_are_held_either_is_an_add(self):
        """The wallet stops 'hedging' and simply adds to whichever side it
        currently likes — a first-side-relative rule would mislabel that and
        gate it behind a confirmation it does not need."""
        s = build()
        m = market()
        s.record_fill(m.slug, "up", 5.0, 9.0, "open", NOW - 60)
        s.record_fill(m.slug, "down", 5.0, 9.0, "hedge", NOW - 40)
        for drift in (1.0005, 0.9995):
            with self.subTest(drift=drift):
                intent = s.evaluate(m, spot(drift_ratio=drift), NOW)
                self.assertEqual(intent.stage, "add")
                self.assertIsNone(intent.reason, intent.reason)

    def test_the_btc_sequence_observed_live_is_reproducible(self):
        """BTC 1:25-1:30PM ET: Up 71c -> Down 39c -> Down 39c -> Down 12c.

        Three regime changes in one 5m window. The drift runs up (buy Up at
        71c), reverses (buy Down at 39c twice), then runs up again — leaving
        Down at 12c, which is bought as a cheap longshot rather than a
        conviction bet. Each leg's drift is chosen so the model's claimed edge
        stays inside the plausible range; the point is the mechanism, not the
        exact cents.
        """
        s = build()
        m = market(up_ask=0.70, down_ask=0.31)
        first = s.evaluate(m, spot(drift_ratio=1.0012), NOW)
        self.assertEqual(first.side, "up")
        self.assertIsNone(first.reason, first.reason)
        s.record_fill(m.slug, "up", first.size_usd, 6.8, "open", NOW - 120)

        # Drift reverses: Down reprices to 38c and is now the favoured side.
        for _ in range(2):
            m.books["down"] = Book(
                asks=BookSide([(0.38, 900.0)]),
                bids=BookSide([(0.36, 900.0)]), ts=NOW,
            )
            intent = s.evaluate(m, spot(drift_ratio=0.9998), NOW)
            self.assertEqual(intent.side, "down")
            self.assertIsNone(intent.reason, intent.reason)
            s.record_fill(m.slug, "down", intent.size_usd,
                          intent.size_usd / intent.limit_price,
                          intent.stage, NOW - 100)

        # Drift runs up again, leaving Down at 12c — bought as a longshot, and
        # sized down accordingly rather than taking a full clip. Both sides are
        # repriced together: a 12c Down implies an ~88c Up, and at that price
        # Up no longer clears its own all-in cost.
        m.books["down"] = Book(
            asks=BookSide([(0.11, 900.0)]),
            bids=BookSide([(0.09, 900.0)]), ts=NOW,
        )
        m.books["up"] = Book(
            asks=BookSide([(0.87, 900.0)]),
            bids=BookSide([(0.85, 900.0)]), ts=NOW,
        )
        last = s.evaluate(m, spot(drift_ratio=1.0012), NOW)
        self.assertEqual(last.side, "down")
        self.assertIsNone(last.reason, last.reason)
        self.assertLess(last.size_usd, 3.5, "a 12c longshot is not a full clip")
        s.record_fill(m.slug, "down", last.size_usd, 33.3, last.stage, NOW - 60)

        state = s.state_for(m.slug)
        self.assertEqual(state.sides_held, 2)
        self.assertEqual(state.fills, 4)


class LockTests(unittest.TestCase):
    """Risk-free locks: take them deliberately.

    Re-tested 2026-08-15 after an operator challenge. The original test summed
    the two sides' VWAPs and asked whether a matched *pair* cost under $1 —
    valid only when the share counts match, and the wallet's legs are lopsided
    by a median 2.11x. The correct test is the worst case: guaranteed payout is
    min(up_shares, down_shares) against TOTAL cost. Measured that way, 7.31% of
    its 52,816 two-sided windows are genuine locks.
    """

    def test_a_lock_is_detected_and_sized_to_the_shortfall(self):
        s = build()
        st = s.state_for("btc-updown-5m-1")
        # 20 shares of Up for $6.00. The other side is available at 12c, so
        # 20 shares of Down costs ~$2.42 all-in for a guaranteed $20.
        st.record("up", 6.0, 20.0, NOW - 60)
        lock = s.lock_for(st, "down", 0.12)
        self.assertIsNotNone(lock)
        size_usd, profit = lock
        q = s.all_in_cost(0.12)
        self.assertAlmostEqual(size_usd, 20.0 * q)
        self.assertAlmostEqual(profit, 20.0 - 6.0 - 20.0 * q)
        self.assertGreater(profit, 0)

    def test_no_lock_when_the_other_side_is_too_expensive(self):
        s = build()
        st = s.state_for("btc-updown-5m-1")
        st.record("up", 6.0, 10.0, NOW - 60)   # 60c a share
        # 10 shares of Down at 55c costs more than the $10 it could pay back.
        self.assertIsNone(s.lock_for(st, "down", 0.55))

    def test_the_shortfall_is_what_matters_when_both_sides_are_held(self):
        """The third fill of an already two-sided window can still lock."""
        s = build()
        st = s.state_for("btc-updown-5m-1")
        st.record("up", 6.0, 25.0, NOW - 60)
        st.record("down", 1.0, 5.0, NOW - 30)   # already holds 5 Down
        lock = s.lock_for(st, "down", 0.10)
        self.assertIsNotNone(lock)
        size_usd, profit = lock
        q = s.all_in_cost(0.10)
        # Only the 20-share shortfall is bought, not another 25.
        self.assertAlmostEqual(size_usd, 20.0 * q)
        self.assertAlmostEqual(profit, 25.0 - 7.0 - 20.0 * q)

    def test_an_already_balanced_window_offers_no_lock(self):
        s = build()
        st = s.state_for("btc-updown-5m-1")
        st.record("up", 5.0, 10.0, NOW - 60)
        st.record("down", 5.0, 10.0, NOW - 30)
        self.assertIsNone(s.lock_for(st, "down", 0.10))

    def test_a_lock_too_small_to_be_worth_it_is_skipped(self):
        s = build(min_lock_profit_usd=2.00)
        st = s.state_for("btc-updown-5m-1")
        # 20 shares held for $6.00 — a 30c basis, so even a 60c hedge locks,
        # but only for $1.66, which is under this floor.
        st.record("up", 6.0, 20.0, NOW - 60)
        self.assertIsNone(s.lock_for(st, "down", 0.60))
        # The same position at 12c locks ~$11.6 and is taken.
        self.assertIsNotNone(s.lock_for(st, "down", 0.12))

    def test_an_expensive_basis_offers_no_lock_at_all(self):
        s = build()
        st = s.state_for("btc-updown-5m-1")
        st.record("up", 12.0, 20.0, NOW - 60)   # 60c basis
        self.assertIsNone(s.lock_for(st, "down", 0.45))

    def test_the_screenshot_window_is_evaluated_correctly(self):
        """BTC 1:30-1:35: Up 12.5sh $5.21, Down 37.2sh $7.52, cost $12.73.

        Guaranteed payout is min(12.5, 37.2) = $12.50 against $12.73 spent, so
        the worst case is -$0.23 — favourable, but NOT a lock. The combined
        VWAP of $0.619 makes it look like a 38% lock only because the legs are
        3.0x lopsided. This is the exact case the old test got wrong.
        """
        s = build()
        st = s.state_for("btc-updown-5m-1")
        st.record("up", 5.21, 12.5, NOW - 120)
        st.record("down", 7.52, 37.2, NOW - 60)
        guaranteed = min(st.side_shares["up"], st.side_shares["down"])
        self.assertAlmostEqual(guaranteed, 12.5)
        self.assertAlmostEqual(st.cost_usd, 12.73)
        self.assertLess(guaranteed, st.cost_usd, "worst case is a small loss")
        # Combined VWAP would have called this a lock; the correct test does not.
        combined = st.vwap("up") + st.vwap("down")
        self.assertLess(combined, 1.0)
        # Down already outnumbers Up, so there is no shortfall to buy.
        self.assertIsNone(s.lock_for(st, "down", 0.12))

    def test_a_lock_bypasses_the_combined_cost_ceiling(self):
        """A guaranteed profit needs no further justification."""
        s = build(hedge_max_combined_cost=1.0)
        m = market(up_ask=0.60, down_ask=0.11)
        s.record_fill(m.slug, "up", 6.0, 20.0, "open", NOW - 60)
        intent = s.evaluate(m, spot(drift_ratio=0.9985), NOW)
        self.assertEqual(intent.side, "down")
        self.assertTrue(intent.is_lock)
        self.assertGreater(intent.lock_profit_usd, 0)
        self.assertIsNone(intent.reason, intent.reason)

    def test_locks_can_be_switched_off(self):
        s = build(lock_when_available=False)
        m = market(up_ask=0.60, down_ask=0.11)
        s.record_fill(m.slug, "up", 6.0, 20.0, "open", NOW - 60)
        intent = s.evaluate(m, spot(drift_ratio=0.9985), NOW)
        self.assertFalse(intent.is_lock)
        self.assertEqual(intent.lock_profit_usd, 0.0)


class ExposureTests(unittest.TestCase):
    """Peak 8 concurrent windows; cost/window median $19.28, p90 $96.43."""

    def test_the_open_window_cap_is_the_measured_peak(self):
        self.assertEqual(build().max_open_windows, 8)

    def test_a_new_window_is_refused_once_the_cap_is_reached(self):
        s = build(max_open_windows=1)
        s.record_fill("other-slug", "up", 5.0, 10.0, "open", NOW - 60)
        self.assertIn("max open windows", s.evaluate(market(), spot(), NOW).reason)

    def test_the_per_window_cost_cap_binds(self):
        s = build(max_cost_per_window_usd=8.0)
        m = market()
        s.record_fill(m.slug, "up", 5.0, 10.0, "open", NOW - 30)
        self.assertIn("window cost cap", s.evaluate(m, spot(), NOW).reason)

    def test_the_strategy_capital_cap_binds(self):
        s = build(max_capital_usd=6.0)
        s.record_fill("other-slug", "up", 5.0, 10.0, "open", NOW - 60)
        self.assertIn("capital cap", s.evaluate(market(), spot(), NOW).reason)

    def test_thin_books_are_refused(self):
        s = build(min_depth_usd=500.0)
        self.assertIn("depth", s.evaluate(market(depth=20.0), spot(), NOW).reason)


class SettlementTests(unittest.TestCase):
    """Zero SELL rows in 569,015 trades: every position is held to resolution."""

    def test_the_winning_side_pays_a_dollar_a_share(self):
        s = build()
        s.record_fill("btc-updown-5m-1", "up", 5.0, 10.0, "open", NOW)
        result = s.settle("btc-updown-5m-1", up_won=True)
        self.assertEqual(result["payout_usd"], 10.0)
        self.assertEqual(result["pnl_usd"], 5.0)

    def test_the_losing_side_pays_nothing(self):
        s = build()
        s.record_fill("btc-updown-5m-1", "up", 5.0, 10.0, "open", NOW)
        result = s.settle("btc-updown-5m-1", up_won=False)
        self.assertEqual(result["payout_usd"], 0.0)
        self.assertEqual(result["pnl_usd"], -5.0)

    def test_a_hedged_window_pays_only_the_winner(self):
        s = build()
        slug = "btc-updown-5m-1"
        s.record_fill(slug, "up", 6.0, 10.0, "open", NOW)
        s.record_fill(slug, "down", 5.0, 10.0, "hedge", NOW)
        result = s.settle(slug, up_won=True)
        # $11 paid in, $10 back: the measured cost of a pair above $1.
        self.assertEqual(result["payout_usd"], 10.0)
        self.assertEqual(result["pnl_usd"], -1.0)
        self.assertEqual(result["sides_held"], 2)

    def test_settling_releases_the_window_state(self):
        s = build()
        s.record_fill("btc-updown-5m-1", "up", 5.0, 10.0, "open", NOW)
        s.settle("btc-updown-5m-1", up_won=True)
        self.assertEqual(s.open_windows(), 0)
        self.assertFalse(s.settle("btc-updown-5m-1", up_won=True)["known"])


class StateReleaseTests(unittest.TestCase):
    """The strategy's in-memory ladder state must track the ledger.

    ``max_capital_usd`` and ``max_open_windows`` are enforced from
    ``windows_state``, which nothing else clears. Observed on 2026-08-15: after
    a ledger reset the dashboard reported $59.93 of capital at work against
    zero open positions, and those budgets stayed consumed by windows that no
    longer existed. Left alone the strategy ratchets itself shut.
    """

    def test_dropping_a_window_frees_its_capital_and_slot(self):
        s = build()
        s.record_fill("btc-updown-5m-1", "up", 5.0, 10.0, "open", NOW)
        s.record_fill("eth-updown-5m-1", "up", 5.0, 10.0, "open", NOW)
        self.assertEqual(s.open_windows(), 2)
        self.assertAlmostEqual(s.open_cost_usd(), 10.0)

        s.drop_window("btc-updown-5m-1")
        self.assertEqual(s.open_windows(), 1)
        self.assertAlmostEqual(s.open_cost_usd(), 5.0)

    def test_the_engine_releases_state_when_a_window_settles(self):
        """_release_window is what ties the two together."""
        from polybot.engine import Engine
        engine = object.__new__(Engine)
        engine.antsaslyku = build()
        engine.antsaslyku.record_fill("btc-updown-5m-1", "up", 5.0, 10.0, "open", NOW)
        self.assertEqual(engine.antsaslyku.open_windows(), 1)

        engine._release_window("btc-updown-5m-1")
        self.assertEqual(engine.antsaslyku.open_windows(), 0)
        self.assertAlmostEqual(engine.antsaslyku.open_cost_usd(), 0.0)

    def test_capital_freed_by_a_settlement_can_be_reused(self):
        s = build(max_capital_usd=6.0)
        s.record_fill("btc-updown-5m-1", "up", 5.0, 10.0, "open", NOW - 60)
        self.assertIn("capital cap", s.evaluate(market(), spot(), NOW).reason)
        s.drop_window("btc-updown-5m-1")
        self.assertIsNone(s.evaluate(market(), spot(), NOW).reason)


class BookkeepingTests(unittest.TestCase):
    def test_vwap_tracks_the_average_paid(self):
        st = WindowState("s")
        st.record("up", 5.0, 10.0, NOW)
        st.record("up", 5.0, 5.0, NOW)
        self.assertAlmostEqual(st.vwap("up"), 10.0 / 15.0)

    def test_stale_windows_are_pruned(self):
        s = build()
        s.record_fill("old", "up", 5.0, 10.0, "open", NOW)
        s.windows_state["old"].opened_at = NOW - 10_000
        self.assertEqual(s.prune(NOW), 1)
        self.assertEqual(s.open_windows(), 0)

    def test_the_snapshot_is_json_shaped(self):
        s = build()
        s.record_fill("btc-updown-5m-1", "up", 5.0, 10.0, "open", NOW)
        snap = s.snapshot()
        self.assertEqual(snap["name"], "antsaslyku")
        self.assertEqual(snap["open_windows"], 1)
        self.assertEqual(snap["stats"]["fills"], 1)
        self.assertIn("hedge_enabled", snap["config"])

    def test_fees_are_included_in_the_all_in_cost(self):
        """usdcSize/(shares*price) averaged 1.0245 across 569,015 real fills."""
        s = build()
        self.assertGreater(s.all_in_cost(0.50), 0.50)
        # At 50c and rate 0.07 the fee is 1.75c/share.
        self.assertAlmostEqual(s.all_in_cost(0.50), 0.5175, places=4)


if __name__ == "__main__":
    unittest.main()
