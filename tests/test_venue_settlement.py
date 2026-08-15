"""Settlement reads the venue's outcome, not a spot approximation of it.

Comparing a spot anchor to a spot close only ever *infers* who won. The
exchanges we poll are not the Chainlink stream the venue resolves on, and a
five-minute crypto window usually finishes close enough to its open that the
basis between them decides the verdict. Replaying 27,679 settled paper windows
against the venue's published outcome, the inference was wrong 13.7% of the
time — enough to turn a measured +$18.6k into -$8.8k.

Settlement therefore runs in two phases, because its two jobs have opposite
deadlines. Capital must be released the instant a window closes or exposure
piles up against the bankroll cap; correctness cannot happen then, because the
venue publishes minutes to tens of minutes later (measured: a window 186s old
was still unresolved). So a window books provisionally on spot, and the
reconciler replaces that verdict with the venue's once it exists.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from polybot.engine import Engine, SETTLE_GRACE_SECONDS as SETTLE_GRACE
from polybot.store import Store

END = 300.0


SLUG = "btc-updown-5m-0"


def _engine(store, resolution=None, spot_state=None, cached=True):
    """A minimal engine.

    ``cached`` seeds the resolution cache the way a reconciler sweep would, so
    the settlement path can be tested on a window the venue has already
    answered. Pass False to test a window it has not.
    """
    engine = object.__new__(Engine)
    engine.runtime = SimpleNamespace(mode="paper")
    engine.store = store
    engine.spot = SimpleNamespace(get=MagicMock(return_value=spot_state))
    engine.poly = SimpleNamespace(resolution=AsyncMock(return_value=resolution))
    engine._anchors = {}
    engine._anchor_sources = {}
    engine._resolutions = {SLUG: resolution} if (cached and resolution) else {}
    engine._last_reconcile = 0.0
    engine.paper = SimpleNamespace()
    return engine


def _store_with_position(tmp, side="up", shares=10.0, stake=5.0):
    store = Store(Path(tmp) / "venue.db")
    store.open_position(
        mode="paper", slug=SLUG, asset="btc", window="5m",
        side=side, confidence=0.7, entry_price=0.5, shares=shares,
        stake_usd=stake, anchor_price=100.0, opened_at=1.0, window_end=END,
        switches=0, anchor_source="chainlink",
    )
    return store


class VenueSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_winning_side_is_paid_one_dollar_a_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_position(tmp, side="up", shares=10.0, stake=5.0)
            engine = _engine(store, resolution="up")
            try:
                await engine._settle_closed(END + 10.0)
                row = store.settlements("paper")[0]
                self.assertEqual(row["won"], 1)
                self.assertAlmostEqual(row["payout_usd"], 10.0)
                self.assertAlmostEqual(row["pnl_usd"], 5.0)
                self.assertEqual(row["method"], "venue")
                self.assertEqual(row["anchor_source"], "venue")
                self.assertEqual(store.open_positions("paper"), [])
            finally:
                store.close()

    async def test_a_losing_side_pays_nothing_and_forfeits_the_stake(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_position(tmp, side="up", shares=10.0, stake=5.0)
            engine = _engine(store, resolution="down")
            try:
                await engine._settle_closed(END + 10.0)
                row = store.settlements("paper")[0]
                self.assertEqual(row["won"], 0)
                self.assertAlmostEqual(row["payout_usd"], 0.0)
                self.assertAlmostEqual(row["pnl_usd"], -5.0)
            finally:
                store.close()

    async def test_the_venue_overrules_a_spot_read_that_disagrees(self):
        """The exact 2026-08-15 03:00 window: spot said won, venue said lost."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_position(tmp, side="up", shares=233.5, stake=4.99)
            # A spot series that would have scored this a win.
            spot = SimpleNamespace(
                price_at_or_before=MagicMock(return_value=63067.53)
            )
            engine = _engine(store, resolution="down", spot_state=spot)
            engine.paper = SimpleNamespace(
                settle=MagicMock(side_effect=AssertionError(
                    "spot must not decide a window the venue has resolved"
                ))
            )
            try:
                await engine._settle_closed(END + 10.0)
                row = store.settlements("paper")[0]
                self.assertEqual(row["won"], 0)
                self.assertAlmostEqual(row["pnl_usd"], -4.99)
                self.assertEqual(row["method"], "venue")
            finally:
                store.close()

    async def test_capital_is_released_immediately_on_the_spot_proxy(self):
        """Waiting for the venue would pin exposure for the better part of an hour."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_position(tmp)
            spot = SimpleNamespace(price_at_or_before=MagicMock(return_value=101.0))
            engine = _engine(store, resolution=None, spot_state=spot)
            engine.paper = SimpleNamespace(settle=MagicMock(return_value={
                "won": True, "payout_usd": 10.0, "pnl_usd": 5.0,
            }))
            try:
                await engine._settle_closed(END + SETTLE_GRACE + 1)
                self.assertEqual(store.open_positions("paper"), [])
                row = store.settlements("paper")[0]
                self.assertEqual(row["method"], "spot")
            finally:
                store.close()

    async def test_settlement_still_works_without_a_feed_at_all(self):
        """The venue needs no price, so a retired asset can still settle."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_position(tmp, side="down", shares=8.0, stake=4.0)
            engine = _engine(store, resolution="down", spot_state=None)
            try:
                await engine._settle_closed(END + 10.0)
                row = store.settlements("paper")[0]
                self.assertEqual(row["won"], 1)
                self.assertAlmostEqual(row["payout_usd"], 8.0)
                self.assertIsNone(row["close_price"])
            finally:
                store.close()


class StrandedPositionTests(unittest.IsolatedAsyncioTestCase):
    """A restart must not leave a position unable to ever settle.

    Pinning the close to the series that produced the anchor is what stops the
    inter-feed basis being booked as P/L. But a restart rebuilds the tick
    buffer from nothing, so a stream that reconnects *after* a window closed
    can never supply that window's close — and the position sits at 0s
    forever. Coherence is the requirement; the specific feed is not.
    """

    def _store(self, tmp, anchor_source="chainlink"):
        store = Store(Path(tmp) / "stranded.db")
        store.open_position(
            mode="live", slug=SLUG, asset="btc", window="5m", side="up",
            confidence=0.7, entry_price=0.5, shares=10.0, stake_usd=5.0,
            anchor_price=100.0, opened_at=1.0, window_end=END, switches=0,
            anchor_source=anchor_source,
        )
        return store

    def _spot(self, coverage):
        """A buffer where only some (ts, source) pairs have data."""
        def at(ts, source=None):
            return coverage.get((ts, source))
        return SimpleNamespace(price_at_or_before=MagicMock(side_effect=at))

    async def test_it_falls_back_to_a_whole_other_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            # Chainlink reconnected after the close, so it covers neither end.
            # Exchange was backfilled and covers both.
            spot = self._spot({
                (END, "exchange"): 102.0,
                (END - 300.0, "exchange"): 100.0,
            })
            engine = _engine(store, resolution=None, spot_state=spot)
            engine.paper = SimpleNamespace(settle=MagicMock(return_value={
                "won": True, "payout_usd": 10.0, "pnl_usd": 5.0,
            }))
            try:
                await engine._settle_closed(END + SETTLE_GRACE + 1)
                self.assertEqual(store.open_positions("live"), [])
                row = store.settlements("live")[0]
                self.assertEqual(row["anchor_source"], "exchange")
                # Both ends re-derived together, never spliced onto the old anchor.
                self.assertAlmostEqual(row["anchor_price"], 100.0)
                self.assertAlmostEqual(row["close_price"], 102.0)
            finally:
                store.close()

    async def test_it_never_splices_a_close_onto_a_foreign_anchor(self):
        """The fallback needs both ends, or it is not a fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            # Exchange has a close but no open: unusable, not half-usable.
            spot = self._spot({(END, "exchange"): 102.0})
            engine = _engine(store, resolution=None, spot_state=spot)
            engine.paper = SimpleNamespace(settle=MagicMock(
                side_effect=AssertionError("settled across two series")
            ))
            try:
                await engine._settle_closed(END + SETTLE_GRACE + 1)
                self.assertEqual(len(store.open_positions("live")), 1)
            finally:
                store.close()

    async def test_the_recorded_series_still_wins_when_it_can_serve(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            spot = self._spot({
                (END, "chainlink"): 103.0,
                (END, "exchange"): 102.0,
                (END - 300.0, "exchange"): 100.0,
            })
            engine = _engine(store, resolution=None, spot_state=spot)
            engine.paper = SimpleNamespace(settle=MagicMock(return_value={
                "won": True, "payout_usd": 10.0, "pnl_usd": 5.0,
            }))
            try:
                await engine._settle_closed(END + SETTLE_GRACE + 1)
                row = store.settlements("live")[0]
                self.assertEqual(row["anchor_source"], "chainlink")
                self.assertAlmostEqual(row["close_price"], 103.0)
            finally:
                store.close()

    async def test_the_reconciler_asks_the_venue_about_stranded_windows(self):
        """The route that needs no price must be reachable from a stuck position."""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            engine = _engine(store, resolution="down", cached=False)
            try:
                await engine.reconcile_settlements(END + 600.0)
                self.assertEqual(engine._resolutions.get(SLUG), "down")

                # Now it can settle, with no price series at all.
                engine.spot = SimpleNamespace(get=MagicMock(return_value=None))
                await engine._settle_closed(END + 610.0)
                row = store.settlements("live")[0]
                self.assertEqual(row["method"], "venue")
                self.assertEqual(row["won"], 0)
                self.assertAlmostEqual(row["pnl_usd"], -5.0)
                self.assertEqual(store.open_positions("live"), [])
            finally:
                store.close()


class ReconcilerTests(unittest.IsolatedAsyncioTestCase):
    """The pass that actually makes the ledger true."""

    def _settled(self, store, side="up", shares=233.5, stake=4.99,
                 won=1, pnl=228.51, method="spot", at=1000.0):
        return store.record_settlement(
            position_id=None, mode="live", slug=SLUG, asset="btc",
            window="5m", side=side, confidence=0.76, entry_price=0.02,
            shares=shares, stake_usd=stake, payout_usd=shares if won else 0.0,
            pnl_usd=pnl, won=won, anchor_price=63056.5,
            close_price=63067.53, settled_at=at, method=method,
        )

    async def test_a_wrong_provisional_verdict_is_corrected(self):
        """The real incident: spot booked +$228.51 on a window that lost."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "rec.db")
            engine = _engine(store, resolution="down", cached=False)
            try:
                sid = self._settled(store)
                result = await engine.reconcile_settlements(2000.0)

                self.assertEqual(result["corrected"], 1)
                row = [r for r in store.settlements("live") if r["id"] == sid][0]
                self.assertEqual(row["won"], 0)
                self.assertAlmostEqual(row["payout_usd"], 0.0)
                self.assertAlmostEqual(row["pnl_usd"], -4.99)
                self.assertEqual(row["method"], "venue")
                self.assertAlmostEqual(result["delta"], -233.50, places=2)
            finally:
                store.close()

    async def test_a_correct_verdict_is_confirmed_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "rec.db")
            engine = _engine(store, resolution="up", cached=False)
            try:
                sid = self._settled(store, side="up", won=1, pnl=228.51)
                result = await engine.reconcile_settlements(2000.0)

                self.assertEqual(result["corrected"], 0)
                row = [r for r in store.settlements("live") if r["id"] == sid][0]
                self.assertEqual(row["won"], 1)
                self.assertAlmostEqual(row["pnl_usd"], 228.51)
                # Stamped, so the row records that the venue confirmed it.
                self.assertEqual(row["method"], "venue")
            finally:
                store.close()

    async def test_an_unresolved_window_is_left_provisional_for_the_next_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "rec.db")
            engine = _engine(store, resolution=None)
            try:
                sid = self._settled(store)
                result = await engine.reconcile_settlements(2000.0)

                self.assertEqual(result["corrected"], 0)
                row = [r for r in store.settlements("live") if r["id"] == sid][0]
                self.assertEqual(row["method"], "spot")
                self.assertEqual(row["won"], 1)
            finally:
                store.close()

    async def test_already_reconciled_rows_are_not_re_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "rec.db")
            engine = _engine(store, resolution="down", cached=False)
            try:
                self._settled(store)
                await engine.reconcile_settlements(2000.0)
                first = engine.poly.resolution.await_count

                result = await engine.reconcile_settlements(2100.0)
                self.assertEqual(result["checked"], 0)
                self.assertEqual(engine.poly.resolution.await_count, first)
            finally:
                store.close()

    async def test_a_failing_lookup_leaves_the_ledger_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "rec.db")
            engine = _engine(store)
            engine.poly.resolution = AsyncMock(side_effect=RuntimeError("gamma down"))
            try:
                sid = self._settled(store)
                result = await engine.reconcile_settlements(2000.0)
                self.assertEqual(result["corrected"], 0)
                row = [r for r in store.settlements("live") if r["id"] == sid][0]
                self.assertEqual(row["method"], "spot")
            finally:
                store.close()

    async def test_rows_older_than_the_lookback_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "rec.db")
            engine = _engine(store, resolution="down", cached=False)
            try:
                self._settled(store, at=1000.0)
                result = await engine.reconcile_settlements(1000.0 + 7 * 3600)
                self.assertEqual(result["checked"], 0)
            finally:
                store.close()

    async def test_one_lookup_serves_every_position_in_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "rec.db")
            engine = _engine(store, resolution="down", cached=False)
            try:
                for _ in range(4):
                    self._settled(store)
                result = await engine.reconcile_settlements(2000.0)
                self.assertEqual(result["corrected"], 4)
                self.assertEqual(engine.poly.resolution.await_count, 1)
            finally:
                store.close()


class ResolutionParsingTests(unittest.TestCase):
    def _feed(self, payload, status=200):
        from polybot.feeds.polymarket import PolymarketFeed

        feed = PolymarketFeed(["btc"], ["5m"])
        feed._client = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(
                status_code=status, json=lambda: payload
            ))
        )
        return feed

    def _resolve(self, payload, status=200):
        return asyncio.run(self._feed(payload, status).resolution("btc-updown-5m-0"))

    def _event(self, **market):
        return [{"markets": [{"closed": True, **market}]}]

    def test_reads_the_paid_outcome_by_label_not_by_index(self):
        self.assertEqual(
            self._resolve(self._event(
                outcomes='["Up", "Down"]', outcomePrices='["0", "1"]'
            )),
            "down",
        )
        self.assertEqual(
            self._resolve(self._event(
                outcomes='["Down", "Up"]', outcomePrices='["0", "1"]'
            )),
            "up",
        )

    def test_an_open_window_has_no_outcome(self):
        self.assertIsNone(self._resolve([{"markets": [{
            "closed": False,
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["1", "0"]',
        }]}]))

    def test_an_unpaid_or_voided_market_is_not_an_outcome(self):
        for prices in ('["0.5", "0.5"]', '["0.6", "0.4"]', '["0", "0"]'):
            with self.subTest(prices=prices):
                self.assertIsNone(self._resolve(self._event(
                    outcomes='["Up", "Down"]', outcomePrices=prices
                )))

    def test_malformed_and_empty_responses_return_none(self):
        for payload in ([], [{}], [{"markets": []}],
                        self._event(outcomes='["Up"]', outcomePrices='["1","0"]')):
            with self.subTest(payload=payload):
                self.assertIsNone(self._resolve(payload))

    def test_a_non_200_returns_none(self):
        self.assertIsNone(self._resolve(self._event(
            outcomes='["Up", "Down"]', outcomePrices='["1", "0"]'
        ), status=503))


if __name__ == "__main__":
    unittest.main()
