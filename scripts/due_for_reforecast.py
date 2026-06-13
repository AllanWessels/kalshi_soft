"""due_for_reforecast.py — deterministically decide which watchlist markets need
a fresh forecast this run, using only stored data (no API calls).

Usage
-----
    python3 scripts/due_for_reforecast.py [--event-driven T1,T2,...] [--limit N] [--summary]

    --event-driven T1,T2,...
        Force-include the listed tickers regardless of cadence (reason=event_driven).
        The orchestrating agent sets this when it has spotted breaking news.

    --limit N
        Keep only the N most-urgent due markets (those closing soonest). The list
        is already sorted by days_to_close ascending, so this truncates the tail
        (the deferred markets carry over to the next run automatically). N<=0 or
        omitted means no limit.

    --summary
        Print a human-readable count-by-reason summary to STDERR in addition to
        the normal JSON output on STDOUT.

Output
------
Prints a single JSON array to STDOUT (machine-parseable); nothing else on STDOUT.
Each element:
    {
        "ticker":       str,
        "title":        str,
        "category":     str,
        "close_time":   str,          # ISO-8601 or "" if missing
        "days_to_close": float,       # rounded to 2 dp; 9999.0 when close_time absent
        "cadence_days": float,
        "reason":       str           # "new" | "scheduled" | "near_close" | "event_driven"
    }
Sorted by days_to_close ascending (most urgent first).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import datetime as dt
import json
from typing import Callable, Optional

from lib import config, store, schemas

# ---------------------------------------------------------------------------
# Core decision logic (pure function — testable without touching the filesystem)
# ---------------------------------------------------------------------------

_LARGE_DAYS = 9999.0  # sentinel when close_time is absent or unparseable


def _days_fractional(delta: dt.timedelta) -> float:
    """Convert a timedelta to fractional days."""
    return delta.total_seconds() / 86400.0


def compute_due(
    watchlist: schemas.Watchlist,
    load_record_fn: Callable[[str], Optional[schemas.ForecastRecord]],
    now: dt.datetime,
    event_driven: set[str] | frozenset[str] = frozenset(),
) -> list[dict]:
    """Return the list of due-items for *watchlist* at reference time *now*.

    Parameters
    ----------
    watchlist:
        The full watchlist (all statuses); only ``status=="active"`` entries are
        considered for cadence logic.
    load_record_fn:
        Callable that accepts a ticker string and returns the matching
        ``ForecastRecord | None``.  Injected so the function is side-effect-free
        and unit-testable.
    now:
        UTC reference datetime (timezone-aware).
    event_driven:
        Tickers that should be force-included with reason ``"event_driven"``.
        Any ticker listed here that is NOT on the watchlist is silently ignored.
    """
    due: list[dict] = []

    # Build a map from ticker -> entry for fast event_driven lookups later.
    active_by_ticker: dict[str, schemas.WatchlistEntry] = {}
    for entry in watchlist.active():
        active_by_ticker[entry.ticker] = entry

    processed: set[str] = set()

    for ticker, entry in active_by_ticker.items():
        processed.add(ticker)

        # ------------------------------------------------------------------ #
        # 1. days_to_close
        # ------------------------------------------------------------------ #
        if entry.close_time:
            try:
                close_dt = schemas.parse_iso(entry.close_time)
                days_to_close = _days_fractional(close_dt - now)
            except (ValueError, TypeError):
                days_to_close = _LARGE_DAYS
        else:
            days_to_close = _LARGE_DAYS

        cadence = config.cadence_days_for(days_to_close)

        # ------------------------------------------------------------------ #
        # 2. Load forecast record
        # ------------------------------------------------------------------ #
        record: Optional[schemas.ForecastRecord] = None
        try:
            record = load_record_fn(ticker)
        except Exception:
            record = None  # treat as missing

        # ------------------------------------------------------------------ #
        # 3. Determine if due
        # ------------------------------------------------------------------ #
        reason: Optional[str] = None

        if ticker in event_driven:
            reason = "event_driven"
        elif record is None or not record.history:
            reason = "new"
        elif record.current is not None:
            try:
                last_as_of = schemas.parse_iso(record.current.as_of)
                age_days = _days_fractional(now - last_as_of)
                if age_days >= cadence:
                    reason = "near_close" if days_to_close <= 7 else "scheduled"
            except (ValueError, TypeError):
                # Unparseable as_of → treat as overdue
                reason = "near_close" if days_to_close <= 7 else "scheduled"
        else:
            # current is None but history is non-empty (shouldn't normally happen
            # but be safe): treat the same as "new"
            reason = "new"

        if reason is not None:
            due.append(
                {
                    "ticker": ticker,
                    "title": entry.title,
                    "category": entry.category,
                    "close_time": entry.close_time,
                    "days_to_close": round(days_to_close, 2),
                    "cadence_days": cadence,
                    "reason": reason,
                }
            )

    # ------------------------------------------------------------------ #
    # 4. event_driven tickers NOT already on the active watchlist are ignored
    # (as specified: silently skip unknown tickers)
    # ------------------------------------------------------------------ #

    # Sort most urgent first; use ticker as a secondary key for determinism.
    due.sort(key=lambda x: (x["days_to_close"], x["ticker"]))
    return due


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="List watchlist markets due for a fresh forecast this run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--event-driven",
        metavar="T1,T2,...",
        default="",
        help="Comma-separated tickers to force-include (reason=event_driven).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Keep only the N most-urgent due markets (closing soonest). "
        "0 or negative means no limit.",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print a human-readable count-by-reason summary to STDERR.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Parse --event-driven
    event_driven: set[str] = set()
    if args.event_driven.strip():
        event_driven = {t.strip() for t in args.event_driven.split(",") if t.strip()}

    # Load watchlist (missing file → empty watchlist; never crash)
    try:
        watchlist = store.load_watchlist()
    except Exception:
        watchlist = schemas.Watchlist()

    now = schemas.parse_iso(schemas.utc_now_iso())  # timezone-aware UTC

    # Compute due list (already sorted most-urgent-first by days_to_close)
    due = compute_due(
        watchlist=watchlist,
        load_record_fn=store.load_forecast,
        now=now,
        event_driven=event_driven,
    )

    # Apply --limit: keep the N markets closing soonest; the rest carry over.
    total_due = len(due)
    deferred = 0
    if args.limit and args.limit > 0 and total_due > args.limit:
        deferred = total_due - args.limit
        due = due[: args.limit]

    # --- STDOUT: machine-readable JSON only ---
    print(json.dumps(due, indent=2, ensure_ascii=False))

    # --- STDERR: optional human summary ---
    if args.summary:
        counts: dict[str, int] = {}
        for item in due:
            r = item["reason"]
            counts[r] = counts.get(r, 0) + 1

        total = len(due)
        if deferred:
            header = (
                f"due_for_reforecast: {total} market(s) selected this run "
                f"(limit={args.limit}; {deferred} deferred of {total_due} due)"
            )
        else:
            header = f"due_for_reforecast: {total} market(s) due this run"
        lines = [header]
        for reason in ("new", "scheduled", "near_close", "event_driven"):
            n = counts.get(reason, 0)
            if n:
                lines.append(f"  {reason}: {n}")
        print("\n".join(lines), file=sys.stderr)

    return 0


# ---------------------------------------------------------------------------
# Inline smoke-test (run only when executed directly, not when imported)
# ---------------------------------------------------------------------------

def _run_inline_tests() -> None:
    """Minimal unit exercises for compute_due(); exits with non-zero on failure."""
    import traceback

    errors: list[str] = []

    # Reference "now": 2026-06-01T00:00:00Z
    NOW = schemas.parse_iso("2026-06-01T00:00:00Z")

    # ------------------------------------------------------------------ #
    # Test A: no forecast record → reason="new"
    # ------------------------------------------------------------------ #
    try:
        wl = schemas.Watchlist()
        far_close = "2027-01-01T00:00:00Z"  # ~214 days out
        wl.markets.append(
            schemas.WatchlistEntry(
                ticker="TEST-NEW",
                title="Will X happen by Jan 2027?",
                category="politics",
                close_time=far_close,
                status="active",
            )
        )
        result = compute_due(wl, load_record_fn=lambda t: None, now=NOW)
        assert len(result) == 1, f"Expected 1 item, got {len(result)}"
        assert result[0]["reason"] == "new", f"Expected 'new', got {result[0]['reason']}"
        assert result[0]["cadence_days"] == 7.0, f"cadence wrong: {result[0]['cadence_days']}"
    except Exception as exc:
        errors.append(f"Test A (new): {exc}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------ #
    # Test B: record with a recent forecast → NOT due
    # ------------------------------------------------------------------ #
    try:
        wl = schemas.Watchlist()
        far_close = "2027-01-01T00:00:00Z"
        wl.markets.append(
            schemas.WatchlistEntry(
                ticker="TEST-RECENT",
                title="Recent forecast market",
                category="economy",
                close_time=far_close,
                status="active",
            )
        )
        # Forecast made 1 day ago; cadence is 7 days → not yet due
        rec = schemas.ForecastRecord(ticker="TEST-RECENT")
        recent_entry = schemas.ForecastEntry(
            my_probability=0.55,
            trigger="scheduled",
            as_of="2026-05-31T00:00:00Z",  # 1 day before NOW
        )
        rec.history.append(recent_entry)
        rec.current = recent_entry

        result = compute_due(wl, load_record_fn=lambda t: rec, now=NOW)
        assert result == [], f"Expected [], got {result}"
    except Exception as exc:
        errors.append(f"Test B (not due): {exc}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------ #
    # Test C: record with a stale forecast → reason="scheduled"
    # ------------------------------------------------------------------ #
    try:
        wl = schemas.Watchlist()
        far_close = "2027-01-01T00:00:00Z"
        wl.markets.append(
            schemas.WatchlistEntry(
                ticker="TEST-STALE",
                title="Stale forecast market",
                category="economy",
                close_time=far_close,
                status="active",
            )
        )
        # Forecast made 10 days ago; cadence is 7 days → due
        rec = schemas.ForecastRecord(ticker="TEST-STALE")
        stale_entry = schemas.ForecastEntry(
            my_probability=0.45,
            trigger="scheduled",
            as_of="2026-05-22T00:00:00Z",  # 10 days before NOW
        )
        rec.history.append(stale_entry)
        rec.current = stale_entry

        result = compute_due(wl, load_record_fn=lambda t: rec, now=NOW)
        assert len(result) == 1, f"Expected 1 item, got {len(result)}"
        assert result[0]["reason"] == "scheduled", f"Expected 'scheduled', got {result[0]['reason']}"
    except Exception as exc:
        errors.append(f"Test C (scheduled): {exc}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------ #
    # Test D: close in 3 days, stale → reason="near_close"
    # ------------------------------------------------------------------ #
    try:
        wl = schemas.Watchlist()
        near_close = "2026-06-04T00:00:00Z"  # 3 days from NOW
        wl.markets.append(
            schemas.WatchlistEntry(
                ticker="TEST-NEAR",
                title="Near-close market",
                category="politics",
                close_time=near_close,
                status="active",
            )
        )
        # cadence for 3 days_to_close = 0.0 → always due
        rec = schemas.ForecastRecord(ticker="TEST-NEAR")
        old_entry = schemas.ForecastEntry(
            my_probability=0.60,
            trigger="scheduled",
            as_of="2026-05-25T00:00:00Z",  # 7 days ago
        )
        rec.history.append(old_entry)
        rec.current = old_entry

        result = compute_due(wl, load_record_fn=lambda t: rec, now=NOW)
        assert len(result) == 1, f"Expected 1 item, got {len(result)}"
        assert result[0]["reason"] == "near_close", f"Expected 'near_close', got {result[0]['reason']}"
        assert result[0]["cadence_days"] == 0.0
    except Exception as exc:
        errors.append(f"Test D (near_close): {exc}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------ #
    # Test E: event_driven override even with fresh forecast
    # ------------------------------------------------------------------ #
    try:
        wl = schemas.Watchlist()
        far_close = "2027-01-01T00:00:00Z"
        wl.markets.append(
            schemas.WatchlistEntry(
                ticker="TEST-ED",
                title="Event-driven market",
                category="statements",
                close_time=far_close,
                status="active",
            )
        )
        # Fresh forecast (just 1 hour ago)
        rec = schemas.ForecastRecord(ticker="TEST-ED")
        fresh_entry = schemas.ForecastEntry(
            my_probability=0.70,
            trigger="scheduled",
            as_of="2026-05-31T23:00:00Z",
        )
        rec.history.append(fresh_entry)
        rec.current = fresh_entry

        result = compute_due(
            wl,
            load_record_fn=lambda t: rec,
            now=NOW,
            event_driven={"TEST-ED"},
        )
        assert len(result) == 1, f"Expected 1 item, got {len(result)}"
        assert result[0]["reason"] == "event_driven", f"Expected 'event_driven', got {result[0]['reason']}"
    except Exception as exc:
        errors.append(f"Test E (event_driven): {exc}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------ #
    # Test F: inactive entry is NOT included
    # ------------------------------------------------------------------ #
    try:
        wl = schemas.Watchlist()
        wl.markets.append(
            schemas.WatchlistEntry(
                ticker="TEST-RESOLVED",
                title="Resolved market",
                category="politics",
                close_time="2026-01-01T00:00:00Z",
                status="resolved",
            )
        )
        result = compute_due(wl, load_record_fn=lambda t: None, now=NOW)
        assert result == [], f"Resolved entry should be skipped, got {result}"
    except Exception as exc:
        errors.append(f"Test F (inactive): {exc}\n{traceback.format_exc()}")

    # ------------------------------------------------------------------ #
    # Report
    # ------------------------------------------------------------------ #
    if errors:
        print("INLINE TEST FAILURES:", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)
    else:
        print("inline tests: all passed", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # If the first argument is "--self-test", run inline tests instead of the CLI.
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        _run_inline_tests()
        sys.exit(0)

    sys.exit(main())
