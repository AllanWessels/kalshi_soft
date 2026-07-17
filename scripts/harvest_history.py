"""harvest_history.py — Harvest Kalshi's SETTLED soft-market history at scale.

Why
---
The live resolved record is tiny (n~31), far too few to learn *where* the crowd is
beatable with any statistical power. But Kalshi exposes thousands of already-SETTLED
markets in our soft categories, each carrying its pre-settlement price and its binary
outcome. That is a large, **leakage-free** corpus (price-vs-outcome arithmetic; no model
"knows" anything) for two jobs:
  1. Map market (in)efficiency by segment -> decide where to even play (inefficiency_atlas.py).
  2. Train a recalibration map at scale.

We harvest BY SERIES (Politics/Elections/Entertainment/Economics/World/Mentions) rather than
scanning the global settled firehose, which is ~all crypto/sports/MVE junk that classifies out.

Usage
-----
python3 scripts/harvest_history.py [--max-series N] [--categories politics,economy,...]
                                   [--out PATH] [--resume]

Output: JSONL at data/history/markets.jsonl (gitignored). One compact record per settled
binary market with a usable implied price and a yes/no result. Resumable: re-running skips
series already fully harvested (tracked in data/history/_harvested_series.json).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import datetime
import json
import traceback

from lib import config
from lib.kalshi_client import KalshiClient, KalshiError, parse_dollars


# Kalshi series-level category -> our canonical soft category.
# (Market objects carry no category; series do. This is the clean filter.)
KALSHI_CAT_TO_CANON = {
    "Politics": "politics",
    "Elections": "politics",
    "World": "politics",        # foreign elections / policy
    "Economics": "economy",
    "Entertainment": "culture",
    "Mentions": "statements",
}


def _slug(kalshi_category: str) -> str:
    """Canonical slug for a non-soft Kalshi category (B3 all-category harvest)."""
    return (kalshi_category or "other").strip().lower().replace(" & ", "-").replace(" ", "-")

HISTORY_DIR = config.DATA_DIR / "history"
DEFAULT_OUT = HISTORY_DIR / "markets.jsonl"
HARVESTED_SERIES_PATH = HISTORY_DIR / "_harvested_series.json"


def _parse_fp(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _iso_to_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _implied_yes(market: dict):
    """Best estimate of the market-implied YES probability *before* settlement.

    Prefer previous_price (last trade before settlement) over last_price (which may be the
    settlement print). Fall back to the mid of the final yes book. Returns float in (0,1) or None.
    """
    for key in ("previous_price_dollars", "last_price_dollars"):
        p = parse_dollars(market.get(key))
        if p is not None and 0.0 < p < 1.0:
            return p
    yb = parse_dollars(market.get("yes_bid_dollars"))
    ya = parse_dollars(market.get("yes_ask_dollars"))
    if yb is not None and ya is not None and 0.0 < yb <= ya < 1.0:
        return round((yb + ya) / 2.0, 6)
    return None


def _record(market: dict, series: dict, canon: str):
    """Build one compact, leakage-free corpus record, or None if unusable."""
    result = (market.get("result") or "").strip().lower()
    if result not in ("yes", "no"):
        return None  # void/scratch/unsettled -> no label
    implied = _implied_yes(market)
    if implied is None:
        return None  # no usable pre-settlement price -> can't measure the market
    open_dt = _iso_to_dt(market.get("open_time"))
    close_dt = _iso_to_dt(market.get("close_time"))
    duration_days = None
    if open_dt and close_dt:
        duration_days = round((close_dt - open_dt).total_seconds() / 86400.0, 3)
    return {
        "ticker": market.get("ticker"),
        "series_ticker": series.get("ticker"),
        "kalshi_category": series.get("category"),
        "category": canon,
        "title": market.get("title"),
        "tags": series.get("tags") or [],
        "result": result,                       # the label: yes=1 / no=0
        "outcome": 1 if result == "yes" else 0,
        "implied_yes": implied,                  # market-implied P(yes) pre-settlement
        "close_time": market.get("close_time"),
        "open_time": market.get("open_time"),
        "duration_days": duration_days,
        "volume": _parse_fp(market.get("volume_fp")),
        "open_interest": _parse_fp(market.get("open_interest_fp")),
        "liquidity": parse_dollars(market.get("liquidity_dollars")) or 0.0,
    }


def _load_harvested() -> set:
    if HARVESTED_SERIES_PATH.exists():
        try:
            return set(json.loads(HARVESTED_SERIES_PATH.read_text()))
        except (ValueError, OSError):
            return set()
    return set()


def _save_harvested(done: set) -> None:
    HARVESTED_SERIES_PATH.write_text(json.dumps(sorted(done)))


def main() -> int:
    ap = argparse.ArgumentParser(description="Harvest settled Kalshi soft-market history")
    ap.add_argument("--max-series", type=int, default=None, help="Cap series processed this run")
    ap.add_argument("--categories", default=None,
                    help="Comma list of canonical cats to include (default: all soft)")
    ap.add_argument("--all-categories", action="store_true",
                    help="B3 (PLAN_FOR_OPUS): harvest EVERY Kalshi category (sports, financials, "
                         "...) for the STRUCTURAL map only — favorite-longshot bias was first "
                         "documented in sports betting. The LLM-forecast blocklist is untouched; "
                         "walk-forward validation decides which new cells are tradeable.")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--resume", action="store_true",
                    help="Skip series already harvested (default: always resume-safe)")
    args = ap.parse_args()

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    want_cats = None
    if args.categories:
        want_cats = {c.strip().lower() for c in args.categories.split(",") if c.strip()}

    client = KalshiClient(*config.load_secrets())
    if not client.ping():
        print("kalshi UNREACHABLE — aborting", file=sys.stderr)
        return 2

    # Discover series to harvest (soft by default; every category with --all-categories).
    resp = client.get_series()
    all_series = resp.get("series") or []
    soft_series = []
    for s in all_series:
        canon = KALSHI_CAT_TO_CANON.get(s.get("category"))
        if canon is None:
            if not args.all_categories:
                continue
            canon = _slug(s.get("category"))
        if want_cats and canon not in want_cats:
            continue
        soft_series.append((s, canon))
    print(f"series to harvest: {len(soft_series)} "
          f"(of {len(all_series)} total; all_categories={args.all_categories})")

    done = _load_harvested()
    if args.resume:
        soft_series = [(s, c) for (s, c) in soft_series if s.get("ticker") not in done]
        print(f"after resume filter: {len(soft_series)} series remain")
    if args.max_series:
        soft_series = soft_series[: args.max_series]

    n_markets = 0
    n_series_done = 0
    out_f = out_path.open("a", encoding="utf-8")
    try:
        for i, (series, canon) in enumerate(soft_series, 1):
            st = series.get("ticker")
            try:
                for market in client.iter_markets(status="settled", series_ticker=st):
                    rec = _record(market, series, canon)
                    if rec is None:
                        continue
                    out_f.write(json.dumps(rec) + "\n")
                    n_markets += 1
                done.add(st)
                n_series_done += 1
            except KalshiError as exc:
                print(f"  [skip {st}] {exc}", file=sys.stderr)
                continue
            if i % 50 == 0:
                out_f.flush()
                _save_harvested(done)
                print(f"  ...{i}/{len(soft_series)} series | {n_markets} usable markets so far")
    finally:
        out_f.flush()
        out_f.close()
        _save_harvested(done)

    print(f"DONE — {n_series_done} series harvested this run, "
          f"{n_markets} usable settled markets written to {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted — progress saved (resumable)", file=sys.stderr)
        raise SystemExit(130)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
