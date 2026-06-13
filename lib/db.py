"""SQLite mirror of the committed JSON state.

Rebuilt from scratch on each call to ``build()``.  The .db file is gitignored
and fully reconstructable from the JSON data files.  It is the substrate the
agent runs SQL over for resolution post-mortems and calibration analysis.

Public API
----------
build(db_path) -> Path
    Delete any existing DB, create fresh tables, populate from JSON via
    store.* loaders, return the path.

connect(db_path) -> sqlite3.Connection
    Open with row_factory = sqlite3.Row.

query(sql, params, db_path) -> list[dict]
    Run a read query, return rows as plain dicts.

Convenience helpers (return list[dict] or dict):
    calibration_by_category(db_path)
    forecast_trajectory(ticker, db_path)
    resolved_with_briers(db_path)
    overall_brier(db_path)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from lib import config, store


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_MARKETS = """
CREATE TABLE IF NOT EXISTS markets (
    ticker      TEXT PRIMARY KEY,
    event_ticker TEXT,
    title       TEXT,
    category    TEXT,
    close_time  TEXT,
    status      TEXT,
    added_at    TEXT
);
"""

_CREATE_FORECASTS = """
CREATE TABLE IF NOT EXISTS forecasts (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                    TEXT,
    as_of                     TEXT,
    my_probability            REAL,
    my_confidence             TEXT,
    market_implied_probability REAL,
    edge                      REAL,
    lean                      TEXT,
    conviction                TEXT,
    ev_per_contract           REAL,
    ev_limit_per_contract     REAL,
    trigger                   TEXT,
    strategy_id               TEXT,
    rationale_summary         TEXT
);
"""

_CREATE_RESOLUTIONS = """
CREATE TABLE IF NOT EXISTS resolutions (
    ticker                TEXT PRIMARY KEY,
    title                 TEXT,
    category              TEXT,
    subcategory           TEXT,
    resolved_at           TEXT,
    outcome               INTEGER,
    final_my_probability  REAL,
    final_market_implied  REAL,
    brier_mine            REAL,
    brier_market          REAL,
    num_forecasts         INTEGER,
    strategy_id           TEXT,
    entry_side            TEXT,
    entry_price           REAL,
    realized_pnl          REAL,
    roi                   REAL,
    won                   INTEGER,
    clv                   REAL
);
"""

_CREATE_LESSONS = """
CREATE TABLE IF NOT EXISTS lessons (
    id                TEXT PRIMARY KEY,
    created_at        TEXT,
    source            TEXT,
    ticker            TEXT,
    category          TEXT,
    outcome           INTEGER,
    brier_mine        REAL,
    brier_market      REAL,
    beat_market       INTEGER,
    lesson            TEXT,
    pattern_tag       TEXT,
    applied_to_skill  INTEGER
);
"""


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(db_path: "str | Path" = config.DB_PATH) -> Path:
    """Delete any existing DB at *db_path*, create fresh tables, populate from
    JSON state, commit, and return the path.

    Robust to None values in any field.  Uses parameterized inserts throughout.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Always start fresh.
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    try:
        _create_tables(conn)
        _populate_markets(conn)
        _populate_forecasts(conn)
        _populate_resolutions(conn)
        _populate_lessons(conn)
        conn.commit()
    finally:
        conn.close()

    return db_path


def _create_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(_CREATE_MARKETS)
    cur.execute(_CREATE_FORECASTS)
    cur.execute(_CREATE_RESOLUTIONS)
    cur.execute(_CREATE_LESSONS)


def _populate_markets(conn: sqlite3.Connection) -> None:
    wl = store.load_watchlist()
    rows = [
        (
            m.ticker,
            m.event_ticker or None,
            m.title or None,
            m.category or None,
            m.close_time or None,
            m.status or None,
            m.added_at or None,
        )
        for m in wl.markets
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO markets "
        "(ticker, event_ticker, title, category, close_time, status, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _populate_forecasts(conn: sqlite3.Connection) -> None:
    rows = []
    for rec in store.iter_forecasts():
        for entry in rec.history:
            rows.append((
                rec.ticker,
                entry.as_of or None,
                entry.my_probability if entry.my_probability is not None else None,
                entry.my_confidence or None,
                entry.market_implied_probability,
                entry.edge,
                entry.lean or None,
                entry.conviction or None,
                entry.ev_per_contract,
                entry.ev_limit_per_contract,
                entry.trigger or None,
                getattr(entry, "strategy_id", "") or None,
                entry.rationale_summary or None,
            ))
    conn.executemany(
        "INSERT INTO forecasts "
        "(ticker, as_of, my_probability, my_confidence, market_implied_probability, "
        " edge, lean, conviction, ev_per_contract, ev_limit_per_contract, "
        " trigger, strategy_id, rationale_summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _populate_resolutions(conn: sqlite3.Connection) -> None:
    rf = store.load_resolutions()
    rows = [
        (
            r.ticker,
            r.title or None,
            r.category or None,
            r.subcategory or None,
            r.resolved_at or None,
            r.outcome if r.outcome is not None else None,
            r.final_my_probability if r.final_my_probability is not None else None,
            r.final_market_implied,
            r.brier_mine,
            r.brier_market,
            r.num_forecasts if r.num_forecasts is not None else None,
            r.strategy_id or None,
            r.entry_side or None,
            r.entry_price,
            r.realized_pnl,
            r.roi,
            (int(r.won) if r.won is not None else None),
            r.clv,
        )
        for r in rf.resolved
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO resolutions "
        "(ticker, title, category, subcategory, resolved_at, outcome, final_my_probability, "
        " final_market_implied, brier_mine, brier_market, num_forecasts, "
        " strategy_id, entry_side, entry_price, realized_pnl, roi, won, clv) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _populate_lessons(conn: sqlite3.Connection) -> None:
    lf = store.load_lessons()
    rows = [
        (
            l.id or None,
            l.created_at or None,
            l.source or None,
            l.ticker or None,
            l.category or None,
            l.outcome,
            l.brier_mine,
            l.brier_market,
            int(l.beat_market) if l.beat_market is not None else None,
            l.lesson or None,
            l.pattern_tag or None,
            int(l.applied_to_skill) if l.applied_to_skill is not None else None,
        )
        for l in lf.lessons
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO lessons "
        "(id, created_at, source, ticker, category, outcome, brier_mine, brier_market, "
        " beat_market, lesson, pattern_tag, applied_to_skill) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


# ---------------------------------------------------------------------------
# Connection + query helpers
# ---------------------------------------------------------------------------

def connect(db_path: "str | Path" = config.DB_PATH) -> sqlite3.Connection:
    """Open the DB with ``row_factory = sqlite3.Row`` (column access by name)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def query(
    sql: str,
    params: tuple = (),
    db_path: "str | Path" = config.DB_PATH,
) -> list[dict]:
    """Execute a read query and return rows as plain dicts."""
    conn = connect(db_path)
    try:
        cur = conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Convenience analysis helpers
# ---------------------------------------------------------------------------

def calibration_by_category(
    db_path: "str | Path" = config.DB_PATH,
) -> list[dict[str, Any]]:
    """Per-category calibration stats from the resolutions table.

    Returns a list of dicts with keys: category, n, brier_mine_mean,
    brier_market_mean, skill (= brier_market_mean - brier_mine_mean;
    positive means we beat the market).
    """
    sql = """
        SELECT
            category,
            COUNT(*)                    AS n,
            AVG(brier_mine)             AS brier_mine_mean,
            AVG(brier_market)           AS brier_market_mean,
            AVG(brier_market) - AVG(brier_mine) AS skill
        FROM resolutions
        WHERE brier_mine IS NOT NULL
        GROUP BY category
        ORDER BY category
    """
    return query(sql, db_path=db_path)


def forecast_trajectory(
    ticker: str,
    db_path: "str | Path" = config.DB_PATH,
) -> list[dict[str, Any]]:
    """All forecast entries for *ticker*, ordered chronologically.

    Returns rows with: as_of, my_probability, market_implied_probability,
    lean, ev_per_contract.
    """
    sql = """
        SELECT as_of, my_probability, market_implied_probability,
               lean, ev_per_contract
        FROM forecasts
        WHERE ticker = ?
        ORDER BY as_of
    """
    return query(sql, (ticker,), db_path=db_path)


def resolved_with_briers(
    db_path: "str | Path" = config.DB_PATH,
) -> list[dict[str, Any]]:
    """All resolved markets ordered by resolved_at."""
    sql = """
        SELECT *
        FROM resolutions
        ORDER BY resolved_at
    """
    return query(sql, db_path=db_path)


def overall_brier(
    db_path: "str | Path" = config.DB_PATH,
) -> dict[str, Any]:
    """Aggregate Brier stats over all resolutions.

    Returns dict with: n, brier_mine_mean, brier_market_mean,
    beat_rate (fraction where brier_mine < brier_market).
    """
    rows = query(
        "SELECT brier_mine, brier_market FROM resolutions "
        "WHERE brier_mine IS NOT NULL",
        db_path=db_path,
    )
    n = len(rows)
    if n == 0:
        return {"n": 0, "brier_mine_mean": None, "brier_market_mean": None, "beat_rate": None}
    brier_mine_mean = sum(r["brier_mine"] for r in rows) / n
    valid_market = [r for r in rows if r["brier_market"] is not None]
    brier_market_mean: Any = (
        sum(r["brier_market"] for r in valid_market) / len(valid_market)
        if valid_market else None
    )
    beat_count = sum(
        1 for r in rows
        if r["brier_market"] is not None and r["brier_mine"] < r["brier_market"]
    )
    beat_rate: Any = beat_count / len(valid_market) if valid_market else None
    return {
        "n": n,
        "brier_mine_mean": brier_mine_mean,
        "brier_market_mean": brier_market_mean,
        "beat_rate": beat_rate,
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    import shutil

    # Build against the real JSON data but write to a temp DB so we don't
    # corrupt the live data/forecasts.db if something goes wrong.
    tmp_dir = Path(tempfile.mkdtemp(prefix="db_test_"))
    try:
        tmp_db = tmp_dir / "test.db"
        build(tmp_db)

        # The markets table should have one row per watchlist entry (all statuses).
        wl = store.load_watchlist()
        expected = len(wl.markets)
        rows = query("SELECT COUNT(*) AS n FROM markets", db_path=tmp_db)
        actual = rows[0]["n"]
        assert actual == expected, (
            f"markets count mismatch: got {actual}, expected {expected}"
        )

        # Forecasts: one row per history entry across all ForecastRecords.
        forecast_records = store.iter_forecasts()
        expected_fc = sum(len(r.history) for r in forecast_records)
        fc_rows = query("SELECT COUNT(*) AS n FROM forecasts", db_path=tmp_db)
        assert fc_rows[0]["n"] == expected_fc, (
            f"forecasts count mismatch: got {fc_rows[0]['n']}, expected {expected_fc}"
        )

        print("db OK")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
