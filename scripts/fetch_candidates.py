"""fetch_candidates.py — Discover and score candidate soft markets on Kalshi.

Usage
-----
python3 scripts/fetch_candidates.py [--limit N] [--min-volume V] [--max-days D]

Flags
-----
--limit N       Cap the number of open markets scanned (default: all).
--min-volume V  Override config.MIN_VOLUME_24H for this run.
--max-days D    Override config.MAX_DAYS_TO_CLOSE for this run.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import datetime
import traceback

from lib import config, store, kalshi_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_fp(value) -> float:
    """Parse a *_fp (fixed-point string) field to float, defaulting to 0.0."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _days_to_close(close_time_iso: str, now_utc: datetime.datetime) -> float:
    """Return fractional days from *now_utc* to the close time ISO string."""
    if not close_time_iso:
        return 0.0
    try:
        close_dt = datetime.datetime.fromisoformat(
            close_time_iso.replace("Z", "+00:00")
        )
        delta = close_dt - now_utc
        return delta.total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 0.0


def _liquidity_score(volume_24h: float, open_interest: float) -> float:
    return volume_24h + 0.1 * open_interest


def _market_implied_prob(yes_bid: float | None, yes_ask: float | None) -> float | None:
    """Midpoint of yes bid/ask as a probability in [0, 1]."""
    if yes_bid is None or yes_ask is None:
        return None
    if yes_bid <= 0 and yes_ask <= 0:
        return None
    return (yes_bid + yes_ask) / 2.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover candidate soft markets on Kalshi and write data/candidates.json"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap how many open markets to scan (default: all). Useful for cheap test runs.",
    )
    parser.add_argument(
        "--min-volume", type=float, default=None,
        help="Override config.MIN_VOLUME_24H for this run.",
    )
    parser.add_argument(
        "--max-days", type=float, default=None,
        help="Override config.MAX_DAYS_TO_CLOSE for this run.",
    )
    parser.add_argument(
        "--top", type=int, default=150,
        help="Persist only the top-N candidates by liquidity_score (default 150). "
             "Keeps the committed pool small and cheap for the agent to read.",
    )
    args = parser.parse_args()

    # Effective thresholds (command-line overrides win over config defaults).
    min_volume_24h = args.min_volume if args.min_volume is not None else config.MIN_VOLUME_24H
    max_days = args.max_days if args.max_days is not None else config.MAX_DAYS_TO_RESOLVE
    min_open_interest = config.MIN_OPEN_INTEREST
    max_spread_cents = config.MAX_SPREAD_CENTS
    min_days = config.MIN_DAYS_TO_CLOSE

    # Build client (public reads need no auth, but we try to load secrets anyway).
    try:
        key_id, pem = config.load_secrets()
        client = kalshi_client.KalshiClient(key_id, pem)
    except Exception as exc:
        print(f"fetch_candidates: failed to initialise KalshiClient: {exc}")
        sys.exit(0)

    # --- Pass 1: enumerate events with nested markets for rich titles ----------
    # We iterate events (with_nested_markets=True) so we always have the event-
    # level title. Each market inside gets the event title as its primary title;
    # if the market-level title differs meaningfully we prefer it.

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    total_scanned = 0
    total_soft = 0
    categories_soft: dict[str, int] = {}

    candidates: list[dict] = []

    print("Scanning Kalshi open events…", flush=True)

    try:
        for event in client.iter_events(status="open", with_nested_markets=True):
            event_title = event.get("title", "") or ""
            event_ticker = event.get("event_ticker", "") or ""
            event_category_hint = event.get("category", "") or ""

            markets = event.get("markets") or []

            for market in markets:
                # Respect --limit N by counting every market regardless of category.
                if args.limit is not None and total_scanned >= args.limit:
                    break

                total_scanned += 1

                ticker = market.get("ticker", "")
                if not ticker:
                    continue

                # Prefer the market title (most specific); fall back to event title.
                market_title = (market.get("title") or "").strip()
                title = market_title if market_title else event_title

                # --- Category classification ---
                category = None
                try:
                    category = config.classify_category(title, event_category_hint)
                except Exception:
                    pass

                if category is None:
                    # Not a soft market or on the stochastic blocklist.
                    continue

                total_soft += 1
                categories_soft[category] = categories_soft.get(category, 0) + 1

                # --- Derive metrics ---
                volume_24h = _parse_fp(market.get("volume_24h_fp"))
                open_interest = _parse_fp(market.get("open_interest_fp"))
                close_time_iso = market.get("close_time", "") or ""
                dtc = _days_to_close(close_time_iso, now_utc)
                # True resolution/settlement date drives the near-term filter.
                resolve_iso = market.get("expected_expiration_time", "") or close_time_iso
                dtr = _days_to_close(resolve_iso, now_utc)

                # Quote for spread and implied probability.
                try:
                    quote = kalshi_client.market_quote(market)
                except Exception:
                    quote = {"yes_bid": None, "yes_ask": None, "last_price": None}

                yes_bid = quote["yes_bid"]
                yes_ask = quote["yes_ask"]

                # spread_cents: (yes_ask - yes_bid) expressed in cents (i.e. × 100)
                if yes_bid is not None and yes_ask is not None:
                    spread_cents = round((yes_ask - yes_bid) * 100)
                else:
                    spread_cents = 999  # effectively infinite → will be filtered

                mip = _market_implied_prob(yes_bid, yes_ask)

                # --- Filters ---
                if volume_24h < min_volume_24h:
                    continue
                if open_interest < min_open_interest:
                    continue
                if spread_cents > max_spread_cents:
                    continue
                # Track only markets that settle within [min_days, max_days] of now,
                # keyed off the true resolution date. The floor drops already-resolved /
                # same-day markets (which can carry stale or negative horizons).
                if dtr < min_days or dtr > max_days:
                    continue

                lscore = _liquidity_score(volume_24h, open_interest)

                candidates.append({
                    "ticker": ticker,
                    "event_ticker": event_ticker,
                    "title": title,
                    "category": category,
                    "close_time": close_time_iso,
                    "days_to_close": round(dtc, 2),
                    "resolve_time": resolve_iso,
                    "days_to_resolve": round(dtr, 2),
                    "volume_24h": round(volume_24h, 2),
                    "open_interest": round(open_interest, 2),
                    "spread_cents": spread_cents,
                    "market_implied_probability": round(mip, 4) if mip is not None else None,
                    "liquidity_score": round(lscore, 2),
                })

            # Break the outer event loop if we've hit the scan limit.
            if args.limit is not None and total_scanned >= args.limit:
                break

    except kalshi_client.KalshiError as exc:
        print(f"fetch_candidates: API unreachable — {exc}")
        sys.exit(0)
    except Exception as exc:
        print(f"fetch_candidates: unexpected error during API scan — {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(0)

    # Sort by liquidity_score descending, then keep only the top-N pool.
    candidates.sort(key=lambda c: c["liquidity_score"], reverse=True)
    total_passing_all = len(candidates)
    if args.top and args.top > 0:
        candidates = candidates[: args.top]

    # --- Persist ---
    try:
        store.save_candidates(candidates)
        candidates_path = config.CANDIDATES_PATH
    except Exception as exc:
        print(f"fetch_candidates: failed to write candidates file — {exc}")
        sys.exit(0)

    # --- Summary ---
    total_passing = total_passing_all
    total_saved = len(candidates)

    print()
    print("=" * 72)
    print(f"  Scanned : {total_scanned:>6d} markets")
    print(f"  Soft    : {total_soft:>6d} (passed category filter)")
    print(f"  Passing : {total_passing:>6d} (passed all liquidity/timing filters)")
    print(f"  Saved   : {total_saved:>6d} (top-N pool persisted for curation)")
    print()
    if categories_soft:
        print("  Soft by category:")
        for cat, n in sorted(categories_soft.items(), key=lambda x: -x[1]):
            print(f"    {cat:<12s}: {n}")
    print()
    print(f"  Written to: {candidates_path}")
    print("=" * 72)

    if not candidates:
        print()
        print("  (No candidates passed all filters. Try --min-volume 0 --max-days 540)")
    else:
        print()
        print(f"  Top {min(10, total_passing)} candidates by liquidity_score:")
        print(f"  {'Ticker':<40}  {'Cat':<10}  {'Vol24h':>8}  {'MIP':>6}  Title")
        print(f"  {'-'*40}  {'-'*10}  {'-'*8}  {'-'*6}  {'-'*30}")
        for c in candidates[:10]:
            mip_str = f"{c['market_implied_probability']:.2f}" if c["market_implied_probability"] is not None else "  N/A"
            print(
                f"  {c['ticker']:<40}  {c['category']:<10}  "
                f"{c['volume_24h']:>8.0f}  {mip_str:>6}  {c['title'][:60]}"
            )
    print()


if __name__ == "__main__":
    main()
