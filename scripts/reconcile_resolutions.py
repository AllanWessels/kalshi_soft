"""reconcile_resolutions.py — Detect resolved Kalshi markets, score them, and update state.

Usage
-----
  python3 scripts/reconcile_resolutions.py [--dry-run]

Flags
-----
  --dry-run   Print what WOULD change but do NOT write any files.

Logic
-----
1. Load watchlist + resolutions. Compute already_resolved tickers (skip these).
2. Candidate tickers = active-watchlist tickers UNION tickers-with-forecast-records
   minus already_resolved.
3. For each candidate, call get_market(ticker). A market is RESOLVED when:
     status in {"settled","finalized","determined"} AND result in {"yes","no"}.
4. For each resolved market:
   - outcome = 1 (yes) or 0 (no).
   - Load forecast record; find last history entry before resolution → final probs.
   - Compute Brier scores.
   - Build Resolution object; append to resolutions.resolved.
   - Mark watchlist entry status="resolved".
5. Save watchlist + resolutions (unless --dry-run).
6. Recompute calibration and save (unless --dry-run).
7. Print human summary.

Idempotent: re-running skips already_resolved tickers.
Robust: per-ticker API errors are caught; total API failure exits 0 without writes.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import datetime as dt

from lib import config, store, scoring, schemas, kalshi_client, taxonomy, profit

# Statuses that indicate a Kalshi market has resolved.
RESOLVED_STATUSES = {"settled", "finalized", "determined"}
# Result values that map to a binary outcome.
BINARY_RESULTS = {"yes", "no"}


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _days_between(start_iso: str, end_iso: str):
    """Days from *start_iso* to *end_iso* (holding period), or None if unparseable."""
    try:
        return round((_parse_iso(end_iso) - _parse_iso(start_iso)).total_seconds() / 86400.0, 2)
    except Exception:
        return None


def _resolved_at_from_market(market: dict) -> str:
    """Extract settlement timestamp from the market dict, falling back to utc now."""
    # Kalshi may provide settlement_date, close_time, expiration_time, etc.
    for key in ("settlement_date", "close_time", "expiration_time"):
        val = market.get(key)
        if val:
            return str(val)
    return _utc_now_iso()


def _find_final_entry(
    history: list[schemas.ForecastEntry],
    resolved_at_str: str,
) -> schemas.ForecastEntry | None:
    """Return the last history entry whose as_of is strictly before resolved_at.

    Falls back to the last entry overall if none is strictly before (e.g. same
    timestamp), because any forecast is still our best-available estimate.
    """
    try:
        resolved_at = _parse_iso(resolved_at_str)
    except Exception:
        # If we can't parse the resolution time, just use the last entry.
        return history[-1] if history else None

    # Walk backwards; return the first entry whose as_of < resolved_at.
    for entry in reversed(history):
        if not entry.as_of:
            continue
        try:
            entry_time = _parse_iso(entry.as_of)
        except Exception:
            continue
        if entry_time < resolved_at:
            return entry

    # No entry strictly before resolution — use the last entry as best effort.
    return history[-1] if history else None


def reconcile(dry_run: bool = False) -> None:
    # ------------------------------------------------------------------
    # 1. Build API client — catch total connection failure immediately.
    # ------------------------------------------------------------------
    key_id, private_key_pem = config.load_secrets()
    try:
        client = kalshi_client.KalshiClient(key_id, private_key_pem)
    except Exception as exc:
        print("reconcile: API unreachable", file=sys.stderr)
        print(f"  (could not construct KalshiClient: {exc})", file=sys.stderr)
        return

    # Quick connectivity probe — fail fast without touching state.
    try:
        reachable = client.ping()
    except Exception:
        reachable = False

    if not reachable:
        print("reconcile: API unreachable")
        return

    # ------------------------------------------------------------------
    # 2. Load existing state.
    # ------------------------------------------------------------------
    watchlist = store.load_watchlist()
    resolutions = store.load_resolutions()
    already_resolved: set[str] = resolutions.tickers()

    # Active watchlist tickers.
    active_tickers: set[str] = {e.ticker for e in watchlist.active()}

    # Tickers that have a forecast record (may include non-watchlist tickers).
    forecast_tickers: set[str] = {rec.ticker for rec in store.iter_forecasts()}

    # Candidate set: union minus already resolved.
    candidates: set[str] = (active_tickers | forecast_tickers) - already_resolved

    # ------------------------------------------------------------------
    # 3. Check each candidate via the API.
    # ------------------------------------------------------------------
    checked = 0
    newly_resolved: list[dict] = []   # accumulate info for the summary line

    for ticker in sorted(candidates):
        checked += 1
        try:
            market = client.get_market(ticker)
        except Exception as exc:
            print(f"reconcile: error fetching {ticker}: {exc}", file=sys.stderr)
            continue

        status = (market.get("status") or "").lower().strip()
        result = (market.get("result") or "").lower().strip()

        # Only handle binary yes/no resolutions.
        if status not in RESOLVED_STATUSES or result not in BINARY_RESULTS:
            continue

        outcome = 1 if result == "yes" else 0
        resolved_at = _resolved_at_from_market(market)

        # Metadata from market dict (fall back gracefully).
        title = market.get("title") or market.get("subtitle") or ticker
        category = market.get("category") or ""

        # Try to find matching watchlist entry for richer metadata.
        wl_entry: schemas.WatchlistEntry | None = next(
            (e for e in watchlist.markets if e.ticker == ticker), None
        )
        if wl_entry:
            if not title or title == ticker:
                title = wl_entry.title or title
            if not category:
                category = wl_entry.category

        # ------------------------------------------------------------------
        # Load forecast record to get final probabilities and scoring inputs.
        # ------------------------------------------------------------------
        forecast_rec = store.load_forecast(ticker)

        if forecast_rec is None or not forecast_rec.history:
            # No forecast history — we can't score, but we still mark resolved.
            if wl_entry and wl_entry.status != "resolved":
                if not dry_run:
                    wl_entry.status = "resolved"
                newly_resolved.append({
                    "ticker": ticker,
                    "outcome": outcome,
                    "brier_mine": None,
                    "brier_market": None,
                    "scored": False,
                })
            # Skip adding a Resolution (nothing to score).
            continue

        history = forecast_rec.history
        final_entry = _find_final_entry(history, resolved_at)

        if final_entry is None:
            # Shouldn't happen given the guard above, but be safe.
            continue

        final_my_probability = final_entry.my_probability
        final_as_of = final_entry.as_of
        final_market_implied = final_entry.market_implied_probability
        first_forecast_prob = history[0].my_probability
        num_forecasts = len(history)

        # COMMIT ANCHOR (entry-lock, option A): score against a FROZEN commitment, not the
        # drifting final forecast. Commit = the locked Position if one was taken, else the
        # first forecast (shadow entry). Later re-forecasts are belief-drift diagnostics only.
        pos = getattr(forecast_rec, "position", None)
        positioned = bool(getattr(pos, "entered", False))
        if positioned:
            commit_prob = pos.entry_probability
            commit_market = pos.entry_market_implied
            commit_as_of = pos.entry_as_of
            commit_verdict = pos.adversarial_verdict or ""
        else:
            commit_prob = first_forecast_prob
            commit_market = history[0].market_implied_probability
            commit_as_of = history[0].as_of
            commit_verdict = ""

        brier_mine = scoring.brier(commit_prob, outcome)
        brier_market: float | None = (
            scoring.brier(commit_market, outcome) if commit_market is not None else None
        )
        held_days = _days_between(commit_as_of, resolved_at)

        # Use forecast record metadata where market dict is thin.
        if not title or title == ticker:
            title = forecast_rec.title or title
        if not category:
            category = forecast_rec.category

        subcategory = taxonomy.classify_subcategory(ticker, title, category)

        # Realized paper-trade economics. PREFER the locked Position (the real entry);
        # fall back to the final lean only for legacy records that predate entry-locking.
        if positioned:
            _fee = scoring.kalshi_fee(pos.entry_price)
            _staked = pos.entry_price + _fee
            _pnl = profit.realized_pnl(pos.entry_side, pos.entry_price, outcome, _fee)
            trade = {
                "entry_side": pos.entry_side, "entry_price": pos.entry_price,
                "fee_at_entry": _fee, "realized_pnl": _pnl,
                "roi": (_pnl / _staked) if _staked > 0 else None,
                "won": profit._won(pos.entry_side, outcome),
                "clv": profit.clv(pos.entry_side, pos.entry_price, final_market_implied),
            }
        else:
            trade = profit.trade_from_entry(final_entry, outcome, final_market_implied)
        # Counterfactual modal-side trade anchored at the FIRST forecast (every market,
        # taken or not) — "if I'd traded my first committed view." Feeds the learner.
        cf = profit.counterfactual_from_entry(history[0], outcome,
                                              history[0].market_implied_probability)

        resolution = schemas.Resolution(
            ticker=ticker,
            title=title,
            category=category,
            subcategory=subcategory,
            resolved_at=resolved_at,
            outcome=outcome,
            final_my_probability=final_my_probability,
            final_as_of=final_as_of,
            final_market_implied=final_market_implied,
            brier_mine=brier_mine,
            brier_market=brier_market,
            num_forecasts=num_forecasts,
            first_forecast_prob=first_forecast_prob,
            strategy_id=getattr(final_entry, "strategy_id", "") or "",
            was_taken=positioned,
            my_confidence=(pos.entry_confidence if positioned else history[0].my_confidence) or "",
            commit_probability=commit_prob,
            commit_as_of=commit_as_of,
            held_days=held_days,
            adversarial_verdict=commit_verdict,
            **(trade or {}),
            **(cf or {}),
        )

        if not dry_run:
            resolutions.resolved.append(resolution)
            if wl_entry:
                wl_entry.status = "resolved"

        newly_resolved.append({
            "ticker": ticker,
            "outcome": outcome,
            "brier_mine": brier_mine,
            "brier_market": brier_market,
            "scored": True,
        })

    # ------------------------------------------------------------------
    # 4. Persist state (skip on --dry-run).
    # ------------------------------------------------------------------
    if not dry_run and newly_resolved:
        store.save_watchlist(watchlist)
        store.save_resolutions(resolutions)
        calibration = scoring.compute_calibration(resolutions.resolved)
        store.save_calibration(calibration)

    # ------------------------------------------------------------------
    # 5. Human summary.
    # ------------------------------------------------------------------
    mode_tag = " [DRY RUN — nothing written]" if dry_run else ""
    print(f"{checked} checked, {len(newly_resolved)} newly resolved{mode_tag}")

    for item in newly_resolved:
        outcome_str = "YES" if item["outcome"] == 1 else "NO"
        if item["scored"]:
            bm_str = f"{item['brier_market']:.4f}" if item["brier_market"] is not None else "n/a"
            print(
                f"  {item['ticker']} -> {outcome_str}  "
                f"brier_mine={item['brier_mine']:.4f}  brier_market={bm_str}"
            )
        else:
            print(f"  {item['ticker']} -> {outcome_str}  (no forecast record — not scored)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Detect resolved Kalshi markets, record outcomes + Brier scores, "
            "free watchlist slots, and recompute calibration. Idempotent."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what WOULD change but do NOT write any files.",
    )
    args = parser.parse_args()
    reconcile(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
