"""SQLite persistence for positions, settlements, and the activity log.

Paper and live rows live in the same tables with a ``mode`` discriminator, and
every read is filtered by mode. Mixing paper fills into a live P/L (or the
reverse) would make the headline number meaningless, so the filter is applied
at the query level rather than left to callers to remember.

Rows carry a second discriminator, ``strategy``, for the same reason. Paper is
allowed to run several strategies at once so their curves can be compared over
identical windows, and a P/L that silently blends them would answer no question
at all. ``strategy=None`` on any read means "every strategy", which is what the
dashboard's All view and every pre-existing caller ask for.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .fees import taker_fee
from .settings import ensure_data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mode          TEXT    NOT NULL,
    slug          TEXT    NOT NULL,
    asset         TEXT    NOT NULL,
    window        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    confidence    REAL    NOT NULL,
    entry_price   REAL    NOT NULL,
    shares        REAL    NOT NULL,
    stake_usd     REAL    NOT NULL,
    anchor_price  REAL,
    opened_at     REAL    NOT NULL,
    window_end    REAL    NOT NULL,
    switches      INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'open',
    order_id      TEXT,
    question      TEXT,
    strategy      TEXT    NOT NULL DEFAULT 'directional',
    anchor_source TEXT
);

CREATE TABLE IF NOT EXISTS settlements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id   INTEGER,
    mode          TEXT    NOT NULL,
    slug          TEXT    NOT NULL,
    asset         TEXT    NOT NULL,
    window        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    confidence    REAL,
    entry_price   REAL    NOT NULL,
    shares        REAL    NOT NULL,
    stake_usd     REAL    NOT NULL,
    payout_usd    REAL    NOT NULL,
    pnl_usd       REAL    NOT NULL,
    won           INTEGER NOT NULL,
    anchor_price  REAL,
    close_price   REAL,
    settled_at    REAL    NOT NULL,
    method        TEXT,
    strategy      TEXT    NOT NULL DEFAULT 'directional',
    anchor_source TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    mode      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    asset     TEXT,
    slug      TEXT,
    message   TEXT NOT NULL,
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS metadata (
    key       TEXT PRIMARY KEY,
    value     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pos_status   ON positions(mode, status);
CREATE INDEX IF NOT EXISTS idx_pos_slug     ON positions(mode, slug, status);
CREATE INDEX IF NOT EXISTS idx_settle_time  ON settlements(mode, settled_at);
CREATE INDEX IF NOT EXISTS idx_activity_ts  ON activity(mode, ts);
"""

# Applied after SCHEMA so they exist whether the column arrived via CREATE
# (fresh database) or via the migration below (existing one).
STRATEGY_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_pos_strategy    ON positions(mode, strategy, status);
CREATE INDEX IF NOT EXISTS idx_settle_strategy ON settlements(mode, strategy, settled_at);
"""

# Every strategy that has ever written rows carries its own name here. The
# default matches the only pipeline that existed before the column did, so an
# ALTER on a populated database labels the entire back history correctly.
DEFAULT_STRATEGY = "directional"


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (ensure_data_dir() / "polybot.db")
        # The engine writes from its loop while the HTTP layer reads; one
        # connection shared under a lock is simpler than a pool and easily fast
        # enough at this volume.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_strategy_column()
            self._migrate_anchor_source_column()
            self._conn.executescript(STRATEGY_INDEXES)
            self._conn.commit()

    # -- migrations --------------------------------------------------------

    def _has_column(self, table: str, column: str) -> bool:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == column for r in rows)

    def _migrate_strategy_column(self) -> None:
        """Add the per-strategy discriminator to databases that predate it.

        Purely additive: ``ADD COLUMN`` with a NOT NULL default rewrites no
        existing value and cannot fail partway through in a way that loses a
        row. Every historical row is labelled ``directional`` because that is
        the only entry pipeline that wrote positions before this column
        existed. Maker settlements are then relabelled from their own
        ``method`` marker, which is the one unambiguous record of their origin.
        """
        for table in ("positions", "settlements"):
            if not self._has_column(table, "strategy"):
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN strategy TEXT NOT NULL "
                    f"DEFAULT '{DEFAULT_STRATEGY}'"
                )

        # The maker pipeline never wrote a positions row — it settles straight
        # from its own inventory — so only settlements need correcting, and
        # `method` identifies them exactly.
        self._conn.execute(
            "UPDATE settlements SET strategy='maker' "
            "WHERE method='maker' AND strategy=?",
            (DEFAULT_STRATEGY,),
        )

    def _migrate_anchor_source_column(self) -> None:
        """Record which price series a window was anchored on.

        A position is settled by comparing its anchor against a close price,
        and the two must be read from the same feed. Storing the anchor's
        source is what lets settlement guarantee that across a restart, when
        the engine's in-memory anchor table is empty.

        Left NULL for rows written before the column existed. NULL means
        "unknown", not "exchange": those windows were anchored from a buffer
        that blended both feeds, so the honest record is that we cannot say.
        """
        for table in ("positions", "settlements"):
            if not self._has_column(table, "anchor_source"):
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN anchor_source TEXT"
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def rebaseline_taker_fees(
        self,
        rate: float,
        exponent: float,
    ) -> dict[str, Any]:
        """Apply the CLOB V2 taker fee to legacy gross-cost history once.

        Earlier records saved the order notional as stake while displaying P/L
        as payout minus notional. CLOB V2 charges the taker fee in addition to
        that notional. The migration is transactional and marker-guarded so a
        restart can never charge the same historical trade twice.
        """
        marker = "clob_v2_fee_rebaseline_2026_07"
        with self._lock:
            existing = self._conn.execute(
                "SELECT value FROM metadata WHERE key=?", (marker,)
            ).fetchone()
            if existing:
                return {"applied": False, "marker": marker, "value": existing["value"]}

            repaired = 0
            adjusted_positions = 0
            adjusted_settlements = 0
            total_fee = 0.0
            try:
                self._conn.execute("BEGIN IMMEDIATE")

                # One early V2 response serialized a whole-number decimal
                # amount ("12") that the legacy parser treated as 12 base
                # units. Repair any row with that unmistakable shape before
                # calculating its fee.
                for table in ("positions", "settlements"):
                    rows = self._conn.execute(
                        f"SELECT id, stake_usd, shares, entry_price FROM {table} "
                        "WHERE entry_price > 1 AND shares > 0 AND shares < 0.01"
                    ).fetchall()
                    for row in rows:
                        shares = float(row["shares"]) * 1_000_000
                        price = float(row["stake_usd"]) / shares
                        if not (0 < price < 1):
                            continue
                        if table == "settlements":
                            settled = self._conn.execute(
                                "SELECT won FROM settlements WHERE id=?", (row["id"],)
                            ).fetchone()
                            payout = shares if int(settled["won"]) else 0.0
                            self._conn.execute(
                                "UPDATE settlements SET shares=?, entry_price=?, "
                                "payout_usd=?, pnl_usd=? WHERE id=?",
                                (
                                    shares,
                                    price,
                                    payout,
                                    payout - float(row["stake_usd"]),
                                    row["id"],
                                ),
                            )
                        else:
                            self._conn.execute(
                                "UPDATE positions SET shares=?, entry_price=? WHERE id=?",
                                (shares, price, row["id"]),
                            )
                        repaired += 1

                rows = self._conn.execute(
                    "SELECT id, entry_price, shares, stake_usd FROM positions "
                    "WHERE entry_price > 0 AND entry_price < 1 AND shares > 0"
                ).fetchall()
                for row in rows:
                    fee = taker_fee(
                        float(row["entry_price"]),
                        float(row["shares"]),
                        rate,
                        exponent,
                    )
                    self._conn.execute(
                        "UPDATE positions SET stake_usd=? WHERE id=?",
                        (float(row["stake_usd"]) + fee, row["id"]),
                    )
                    adjusted_positions += 1

                rows = self._conn.execute(
                    "SELECT id, entry_price, shares, stake_usd, payout_usd "
                    "FROM settlements WHERE entry_price > 0 AND entry_price < 1 "
                    "AND shares > 0"
                ).fetchall()
                for row in rows:
                    fee = taker_fee(
                        float(row["entry_price"]),
                        float(row["shares"]),
                        rate,
                        exponent,
                    )
                    stake = float(row["stake_usd"]) + fee
                    self._conn.execute(
                        "UPDATE settlements SET stake_usd=?, pnl_usd=? WHERE id=?",
                        (stake, float(row["payout_usd"]) - stake, row["id"]),
                    )
                    adjusted_settlements += 1
                    total_fee += fee

                value = (
                    f"positions={adjusted_positions};settlements={adjusted_settlements};"
                    f"repaired={repaired};fees={total_fee:.6f}"
                )
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES(?,?)", (marker, value)
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return {
            "applied": True,
            "marker": marker,
            "positions": adjusted_positions,
            "settlements": adjusted_settlements,
            "repaired": repaired,
            "total_fee": total_fee,
        }

    # -- writes ------------------------------------------------------------

    def open_position(self, **kw: Any) -> int:
        cols = (
            "mode", "slug", "asset", "window", "side", "confidence", "entry_price",
            "shares", "stake_usd", "anchor_price", "opened_at", "window_end",
            "switches", "order_id", "question", "anchor_source", "strategy",
        )
        # `strategy` is NOT NULL, and a caller that predates the column must not
        # be able to write a null into it.
        values = [kw.get(c) for c in cols]
        values[-1] = kw.get("strategy") or DEFAULT_STRATEGY
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO positions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                values,
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def close_position(self, position_id: int, status: str = "settled") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE positions SET status=? WHERE id=?", (status, position_id)
            )
            self._conn.commit()

    def bump_switches(self, position_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE positions SET switches = switches + 1 WHERE id=?", (position_id,)
            )
            self._conn.commit()

    def record_settlement(self, **kw: Any) -> int:
        cols = (
            "position_id", "mode", "slug", "asset", "window", "side", "confidence",
            "entry_price", "shares", "stake_usd", "payout_usd", "pnl_usd", "won",
            "anchor_price", "close_price", "settled_at", "method", "anchor_source",
            "strategy",
        )
        values = [kw.get(c) for c in cols]
        values[-1] = kw.get("strategy") or DEFAULT_STRATEGY
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO settlements ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                values,
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def provisional_settlements(self, since: float) -> list[dict[str, Any]]:
        """Settlements decided by the spot proxy that the venue could correct.

        The venue publishes an outcome long after a window closes — minutes to
        tens of minutes — so waiting for it before booking would pin capital
        against the open-exposure cap and stop the bot trading. Instead a
        window settles provisionally on spot, and this is what the reconciler
        walks to replace those verdicts with the venue's.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, mode, slug, side, shares, stake_usd, payout_usd, "
                "pnl_usd, won FROM settlements "
                "WHERE method='spot' AND settled_at >= ? ORDER BY settled_at",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def apply_venue_outcome(
        self, settlement_id: int, won: int, payout_usd: float, pnl_usd: float
    ) -> None:
        """Replace a provisional verdict with the one the venue published."""
        with self._lock:
            self._conn.execute(
                "UPDATE settlements SET won=?, payout_usd=?, pnl_usd=?, "
                "method='venue', anchor_source='venue' WHERE id=?",
                (int(won), float(payout_usd), float(pnl_usd), int(settlement_id)),
            )
            self._conn.commit()

    def log(
        self,
        mode: str,
        kind: str,
        message: str,
        asset: str | None = None,
        slug: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO activity (ts, mode, kind, asset, slug, message, detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (time.time(), mode, kind, asset, slug, message, detail),
            )
            self._conn.commit()

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _strategy_clause(strategy: str | None) -> tuple[str, list[Any]]:
        """SQL fragment and params for an optional strategy filter.

        ``None`` deliberately means every strategy rather than the default one,
        so that callers written before the column existed keep seeing complete
        history.
        """
        if not strategy or strategy == "all":
            return "", []
        return " AND strategy=?", [strategy]

    def strategies_seen(self, mode: str) -> list[dict[str, Any]]:
        """Strategies that have written rows in this mode, for the UI selector.

        Sourced from both tables so a strategy that has opened positions but
        not yet settled anything still appears — otherwise it would be missing
        from the selector for exactly as long as it takes to prove itself.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT strategy, SUM(settlements) AS settlements, "
                "       SUM(open_positions) AS open_positions "
                "FROM ("
                "  SELECT strategy, COUNT(*) AS settlements, 0 AS open_positions "
                "  FROM settlements WHERE mode=? GROUP BY strategy"
                "  UNION ALL"
                "  SELECT strategy, 0 AS settlements, COUNT(*) AS open_positions "
                "  FROM positions WHERE mode=? AND status='open' GROUP BY strategy"
                ") GROUP BY strategy ORDER BY settlements DESC, strategy ASC",
                (mode, mode),
            ).fetchall()
        return [
            {
                "strategy": r["strategy"],
                "settlements": int(r["settlements"] or 0),
                "open_positions": int(r["open_positions"] or 0),
            }
            for r in rows
        ]

    def open_positions(
        self, mode: str, strategy: str | None = None
    ) -> list[dict[str, Any]]:
        clause, extra = self._strategy_clause(strategy)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE mode=? AND status='open'"
                + clause
                + " ORDER BY opened_at DESC",
                [mode, *extra],
            ).fetchall()
        return [dict(r) for r in rows]

    def open_position_for(self, mode: str, slug: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM positions WHERE mode=? AND slug=? AND status='open' "
                "ORDER BY opened_at DESC LIMIT 1",
                (mode, slug),
            ).fetchone()
        return dict(row) if row else None

    def entries_for_window(self, mode: str, slug: str) -> int:
        """Entries made in this window, open or already settled.

        Counts settled rows too — a window that has been partly closed out has
        still consumed its budget of attempts, and the cap exists precisely to
        stop repeated re-entry into the same losing window.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM positions WHERE mode=? AND slug=?",
                (mode, slug),
            ).fetchone()
        return int(row["n"] or 0)

    def side_switches_for_window(self, mode: str, slug: str) -> int:
        """Highest completed side-switch count recorded for one market.

        The count is carried by every later entry, so consulting the whole
        window prevents an ordinary add-on leg from accidentally resetting the
        reversal allowance back to zero.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(switches),0) AS n FROM positions "
                "WHERE mode=? AND slug=?",
                (mode, slug),
            ).fetchone()
        return int(row["n"] or 0)

    def window_exposure(self, mode: str, slug: str) -> float:
        """Open exposure for one individual market slug."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(stake_usd),0) AS s FROM positions "
                "WHERE mode=? AND slug=? AND status='open'",
                (mode, slug),
            ).fetchone()
        return float(row["s"] or 0.0)

    def interval_exposure(self, mode: str, window: str, window_end: float) -> float:
        """Combined open exposure across every asset in one timed interval.

        A 5-minute BTC market and a 5-minute XRP market ending at the same
        instant share one budget. This prevents the configured cap from being
        multiplied by the number of enabled assets.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(stake_usd),0) AS s FROM positions "
                "WHERE mode=? AND window=? AND ABS(window_end - ?) < 0.5 "
                "AND status='open'",
                (mode, window, float(window_end)),
            ).fetchone()
        return float(row["s"] or 0.0)

    def open_exposure(self, mode: str, strategy: str | None = None) -> float:
        """Total capital currently committed across every open position."""
        clause, extra = self._strategy_clause(strategy)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(stake_usd),0) AS s FROM positions "
                "WHERE mode=? AND status='open'" + clause,
                [mode, *extra],
            ).fetchone()
        return float(row["s"] or 0.0)

    def exposure_since(
        self, mode: str, since: float, strategy: str | None = None
    ) -> float:
        """Total staked since a timestamp, open or settled — the daily cap basis."""
        clause, extra = self._strategy_clause(strategy)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(stake_usd),0) AS s FROM positions "
                "WHERE mode=? AND opened_at >= ?" + clause,
                [mode, since, *extra],
            ).fetchone()
        return float(row["s"] or 0.0)

    def settlements(
        self,
        mode: str,
        limit: int = 500,
        since: float | None = None,
        strategy: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM settlements WHERE mode=?"
        params: list[Any] = [mode]
        if since is not None:
            query += " AND settled_at>=?"
            params.append(since)
        clause, extra = self._strategy_clause(strategy)
        query += clause
        params.extend(extra)
        query += " ORDER BY settled_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def win_loss_summary(
        self,
        mode: str,
        since: float | None = None,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        """Complete outcome summary for one display range.

        This is intentionally computed independently of the detailed-table
        limit so an All-time profit factor cannot silently describe only the
        newest 200 settlements.
        """
        query = (
            "SELECT COUNT(*) AS n, COALESCE(SUM(won),0) AS wins, "
            "COALESCE(SUM(CASE WHEN won=1 THEN pnl_usd ELSE 0 END),0) AS gross_win, "
            "COALESCE(SUM(CASE WHEN won=0 THEN pnl_usd ELSE 0 END),0) AS gross_loss, "
            "MAX(pnl_usd) AS best, MIN(pnl_usd) AS worst "
            "FROM settlements WHERE mode=?"
        )
        params: list[Any] = [mode]
        if since is not None:
            query += " AND settled_at>=?"
            params.append(since)
        clause, extra = self._strategy_clause(strategy)
        query += clause
        params.extend(extra)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()

        total = int(row["n"] or 0)
        wins = int(row["wins"] or 0)
        return {
            "settlements": total,
            "wins": wins,
            "losses": total - wins,
            "gross_win": round(float(row["gross_win"] or 0.0), 4),
            "gross_loss": round(float(row["gross_loss"] or 0.0), 4),
            "best": round(float(row["best"]), 4) if row["best"] is not None else None,
            "worst": round(float(row["worst"]), 4) if row["worst"] is not None else None,
        }

    def cohort_evidence(
        self, mode: str, asset: str, window: str, block_size: int = 50
    ) -> dict[str, Any]:
        """Return two non-overlapping recent performance blocks for a cohort."""
        block_size = max(1, int(block_size))
        with self._lock:
            rows = self._conn.execute(
                "SELECT stake_usd, pnl_usd FROM settlements "
                "WHERE mode=? AND asset=? AND window=? "
                "ORDER BY settled_at DESC, id DESC LIMIT ?",
                (mode, asset, window, block_size * 2),
            ).fetchall()

        def metrics(block: list[Any]) -> dict[str, Any]:
            stake = sum(float(row["stake_usd"]) for row in block)
            pnl = sum(float(row["pnl_usd"]) for row in block)
            return {
                "settlements": len(block),
                "stake_usd": round(stake, 4),
                "pnl_usd": round(pnl, 4),
                "roi": round(pnl / stake, 6) if stake > 0 else None,
            }

        latest = metrics(list(rows[:block_size]))
        preceding = metrics(list(rows[block_size:block_size * 2]))
        qualified = bool(
            len(rows) >= block_size * 2
            and latest["roi"] is not None
            and preceding["roi"] is not None
            and float(latest["roi"]) > 0
            and float(preceding["roi"]) > 0
        )
        return {
            "asset": asset,
            "window": window,
            "evidence_qualified": qualified,
            "latest_50": latest,
            "preceding_50": preceding,
        }

    def recent_pnl(self, mode: str, limit: int = 25) -> list[float]:
        """Oldest-first P/L of the most recent settlements."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pnl_usd FROM settlements WHERE mode=? ORDER BY settled_at DESC LIMIT ?",
                (mode, limit),
            ).fetchall()
        return [float(r["pnl_usd"]) for r in reversed(rows)]

    def pnl_since(self, mode: str, since: float) -> list[float]:
        """Oldest-first settled P/L at or after a rolling time boundary."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pnl_usd FROM settlements "
                "WHERE mode=? AND settled_at>=? ORDER BY settled_at ASC",
                (mode, since),
            ).fetchall()
        return [float(r["pnl_usd"]) for r in rows]

    def equity_curve(
        self,
        mode: str,
        since: float | None = None,
        max_points: int = 5000,
        strategy: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cumulative P/L over time, oldest first, with a bounded wire payload.

        SQLite computes the complete running total. When history grows beyond
        ``max_points``, only evenly spaced display samples plus the exact final
        point cross into Python. Aggregate statistics remain unsampled.

        The running total is computed *after* the strategy filter, so switching
        the selector redraws a curve that starts at zero for that strategy
        rather than showing its slice of a blended total.
        """
        clause, extra = self._strategy_clause(strategy)
        query = "SELECT settled_at, pnl_usd FROM settlements WHERE mode=?"
        params: list[Any] = [mode]
        if since is not None:
            query += " AND settled_at >= ?"
            params.append(since)
        query += clause
        params.extend(extra)
        query += " ORDER BY settled_at ASC"
        with self._lock:
            count_query = "SELECT COUNT(*) AS n FROM settlements WHERE mode=?"
            count_params: list[Any] = [mode]
            if since is not None:
                count_query += " AND settled_at >= ?"
                count_params.append(since)
            count_query += clause
            count_params.extend(extra)
            total = int(self._conn.execute(count_query, count_params).fetchone()["n"])

            if total <= max_points:
                rows = self._conn.execute(query, params).fetchall()
                sampled = False
            else:
                stride = max(1, (total + max_points - 1) // max_points)
                where = "mode=?"
                sample_params: list[Any] = [mode]
                if since is not None:
                    where += " AND settled_at >= ?"
                    sample_params.append(since)
                where += clause
                sample_params.extend(extra)
                sample_params.append(stride)
                rows = self._conn.execute(
                    "WITH curve AS ("
                    " SELECT settled_at,"
                    "        SUM(pnl_usd) OVER (ORDER BY settled_at, id) AS cumulative,"
                    "        ROW_NUMBER() OVER (ORDER BY settled_at, id) AS rn,"
                    "        COUNT(*) OVER () AS total"
                    f" FROM settlements WHERE {where}"
                    ")"
                    " SELECT settled_at, cumulative, rn, total FROM curve"
                    " WHERE (rn - 1) % ? = 0 OR rn = total"
                    " ORDER BY rn",
                    sample_params,
                ).fetchall()
                sampled = True

        out: list[dict[str, Any]] = []
        cumulative = 0.0
        for row in rows:
            if sampled:
                cumulative = float(row["cumulative"])
                out.append({
                    "t": float(row["settled_at"]),
                    "v": round(cumulative, 4),
                    "i": int(row["rn"]),
                    "n": int(row["total"]),
                })
            else:
                cumulative += float(row["pnl_usd"])
                out.append({"t": float(row["settled_at"]), "v": round(cumulative, 4)})
        return out

    def stats(
        self,
        mode: str,
        since: float | None = None,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate settled performance, optionally limited to a time window.

        ``since`` scopes only the settlement aggregates. Open positions are
        always counted in full — a position is open now regardless of which
        chart range happens to be selected. ``strategy`` scopes both, because
        an open position does belong to exactly one strategy.
        """
        query = (
            "SELECT COUNT(*) AS n, "
            "       COALESCE(SUM(pnl_usd),0) AS pnl, "
            "       COALESCE(SUM(won),0) AS wins, "
            "       COALESCE(SUM(stake_usd),0) AS staked, "
            "       COALESCE(MAX(pnl_usd),0) AS best, "
            "       COALESCE(MIN(pnl_usd),0) AS worst, "
            "       MIN(settled_at) AS first_at, "
            "       MAX(settled_at) AS last_at "
            "FROM settlements WHERE mode=?"
        )
        params: list[Any] = [mode]
        if since is not None:
            query += " AND settled_at >= ?"
            params.append(since)
        clause, extra = self._strategy_clause(strategy)
        query += clause
        params.extend(extra)

        with self._lock:
            row = self._conn.execute(query, params).fetchone()
            open_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM positions WHERE mode=? AND status='open'"
                + clause,
                [mode, *extra],
            ).fetchone()

        n = int(row["n"] or 0)
        wins = int(row["wins"] or 0)
        staked = float(row["staked"] or 0.0)
        pnl = float(row["pnl"] or 0.0)
        return {
            "settlements": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": (wins / n) if n else 0.0,
            "total_pnl": round(pnl, 4),
            "staked": round(staked, 2),
            "roi": (pnl / staked) if staked else 0.0,
            "biggest_win": round(float(row["best"] or 0.0), 2),
            "biggest_loss": round(float(row["worst"] or 0.0), 2),
            "open_positions": int(open_row["n"] or 0),
            "first_at": float(row["first_at"]) if row["first_at"] else None,
            "last_at": float(row["last_at"]) if row["last_at"] else None,
        }

    def by_asset(
        self,
        mode: str,
        since: float | None = None,
        strategy: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT asset, window, COUNT(*) AS n, COALESCE(SUM(won),0) AS wins, "
            "       COALESCE(SUM(pnl_usd),0) AS pnl, COALESCE(SUM(stake_usd),0) AS staked "
            "FROM settlements WHERE mode=?"
        )
        params: list[Any] = [mode]
        if since is not None:
            query += " AND settled_at>=?"
            params.append(since)
        clause, extra = self._strategy_clause(strategy)
        query += clause
        params.extend(extra)
        query += " GROUP BY asset, window ORDER BY pnl DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        out = []
        for r in rows:
            n = int(r["n"])
            staked = float(r["staked"] or 0.0)
            pnl = float(r["pnl"] or 0.0)
            out.append({
                "asset": r["asset"],
                "window": r["window"],
                "settlements": n,
                "wins": int(r["wins"]),
                "win_rate": (int(r["wins"]) / n) if n else 0.0,
                "pnl": round(pnl, 2),
                "staked": round(staked, 2),
                "roi": (pnl / staked) if staked else 0.0,
            })
        return out

    def activity(self, mode: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activity WHERE mode=? ORDER BY ts DESC LIMIT ?",
                (mode, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def reset(self, mode: str, strategy: str | None = None) -> None:
        """Wipe rows for one mode, optionally for a single strategy.

        Scoping to a strategy lets a paper cohort be restarted without
        discarding the other strategy's comparison history. The activity log is
        only cleared on a full-mode reset, because its rows are not attributed
        to a strategy and a partial wipe would leave a misleading trail.
        """
        clause, extra = self._strategy_clause(strategy)
        with self._lock:
            for table in ("positions", "settlements"):
                self._conn.execute(
                    f"DELETE FROM {table} WHERE mode=?" + clause, [mode, *extra]
                )
            if not clause:
                self._conn.execute("DELETE FROM activity WHERE mode=?", (mode,))
            self._conn.commit()
