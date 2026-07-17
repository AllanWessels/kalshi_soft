#!/usr/bin/env python3
"""Add or drop watchlist entries (the deterministic half of ROUTINE Step 2).

The agent decides WHICH markets to track (judgment); this script performs the
mechanical add/drop against data/watchlist.json, pulling market metadata from
data/candidates.json so fields are accurate and not guessed. Enforces the cap.

Self-cleaning: every run purges dead entries (any non-active status) so the file
never accumulates dropped/resolved tombstones. A --drop therefore removes the
entry outright rather than leaving a marker.

Usage:
    python3 scripts/curate_watchlist.py --add TICKER1,TICKER2
    python3 scripts/curate_watchlist.py --drop TICKER          # removes the entry
    python3 scripts/curate_watchlist.py --list                 # also purges dead entries
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config, store, schemas  # noqa: E402


def _candidate_index() -> dict:
    return {c["ticker"]: c for c in store.load_candidates()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--add", default="", help="comma-separated tickers to add (from candidates.json)")
    ap.add_argument("--drop", default="", help="comma-separated tickers to mark dropped")
    ap.add_argument("--list", action="store_true", help="print the current watchlist")
    args = ap.parse_args()

    wl = store.load_watchlist()
    by_ticker = {m.ticker: m for m in wl.markets}
    cand = _candidate_index()

    added, skipped, dropped = [], [], []

    for t in [x.strip() for x in args.add.split(",") if x.strip()]:
        if t in by_ticker and by_ticker[t].status == "active":
            skipped.append(f"{t} (already active)")
            continue
        active_count = len(wl.active())
        if t not in by_ticker and active_count >= wl.cap:
            skipped.append(f"{t} (cap {wl.cap} reached)")
            continue
        c = cand.get(t)
        if not c:
            skipped.append(f"{t} (not in candidates.json — run fetch_candidates first)")
            continue
        # TRIAGE gate (Workstream C1): refuse measured negative-skill efficient segments —
        # Tetlock's Goldilocks rule, enforced mechanically so a hopeful curation judgment
        # can't re-admit markets the record says we cannot beat.
        from lib import taxonomy  # local import: keep module import cheap
        subcat = taxonomy.classify_subcategory(t, c.get("title", ""), c.get("category", ""))
        if subcat in config.TRIAGE_EXCLUDED_SUBCATS:
            skipped.append(f"{t} (TRIAGE: '{subcat}' is a measured negative-skill efficient "
                           f"segment — see PLAN_FOR_OPUS §C1)")
            continue
        # Use the true resolution date (expected_expiration_time, surfaced as
        # resolve_time by fetch_candidates) for tiering — some markets trade well
        # past when they actually settle (e.g. a primary that settles in June but
        # whose close_time is the November general).
        resolve_time = c.get("resolve_time") or c.get("close_time", "")
        entry = schemas.WatchlistEntry(
            ticker=t,
            event_ticker=c.get("event_ticker", ""),
            title=c.get("title", ""),
            category=c.get("category", ""),
            close_time=resolve_time,
            added_at=schemas.utc_now_iso(),
            liquidity_snapshot=schemas.LiquiditySnapshot(
                volume_24h=float(c.get("volume_24h", 0) or 0),
                open_interest=float(c.get("open_interest", 0) or 0),
                spread_cents=float(c.get("spread_cents", 0) or 0),
            ),
            status="active",
            reforecast_cadence_days=config.cadence_days_for(c.get("days_to_resolve", c.get("days_to_close", 9999)) or 9999),
        )
        if t in by_ticker:
            # re-activating a previously dropped/resolved entry
            idx = wl.markets.index(by_ticker[t])
            wl.markets[idx] = entry
        else:
            wl.markets.append(entry)
            by_ticker[t] = entry
        added.append(t)

    for t in [x.strip() for x in args.drop.split(",") if x.strip()]:
        if t in by_ticker:
            by_ticker[t].status = "dropped"
            dropped.append(t)

    # Self-cleaning: purge dead entries (any non-active status) so the file does
    # not accumulate tombstones run over run. Safe because non-active entries are
    # redundant — resolved markets are the source of truth in resolutions.json,
    # forecast records live in their own files, and reconcile re-discovers any
    # still-open market via the forecast-record union (not watchlist status). This
    # also removes anything dropped earlier in this same run.
    purged = [m.ticker for m in wl.markets if m.status != "active"]
    if purged:
        wl.markets = [m for m in wl.markets if m.status == "active"]

    if added or dropped or purged:
        store.save_watchlist(wl)

    if added:
        print(f"added:   {', '.join(added)}")
    if dropped:
        print(f"dropped: {', '.join(dropped)}")
    if purged:
        noun = "entry" if len(purged) == 1 else "entries"
        print(f"purged:  {len(purged)} dead {noun} ({', '.join(purged)})")
    if skipped:
        print(f"skipped: {'; '.join(skipped)}")

    if args.list or not (added or dropped or skipped):
        print(f"\nWatchlist ({len(wl.active())} active / cap {wl.cap}):")
        for m in wl.markets:
            print(f"  [{m.status:8}] {m.ticker:<34} {m.category:<10} {m.title[:54]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
