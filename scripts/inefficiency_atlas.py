"""inefficiency_atlas.py — Where is the Kalshi crowd actually beatable?

Reads the harvested settled-history corpus (data/history/markets.jsonl) and computes the
MARKET's own calibration, sliced by segment. This is leakage-free: it only uses each market's
pre-settlement implied price and its realized binary outcome. No model is involved.

The logic that ties this to profit:
  - Where the market is well-calibrated (low ECE, low Brier), NO forecaster beats it -> don't play.
  - Where the market is systematically MIScalibrated in a cell with enough resolved n, a researched
    forecaster has room to win -> concentrate the watchlist there.

We report, per cell (category x price-band x liquidity-tier):
  n, market Brier, market ECE (calibration error), and signed bias (mean outcome - mean implied).
A cell is flagged BEATABLE if it has n >= MIN_N and market ECE >= ECE_FLAG (the crowd is
meaningfully off there). Output: data/history/atlas.json + a ranked human-readable summary.

Usage: python3 scripts/inefficiency_atlas.py [--in PATH] [--min-n N]
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
from collections import defaultdict

from lib import config

DEFAULT_IN = config.DATA_DIR / "history" / "markets.jsonl"
ATLAS_OUT = config.DATA_DIR / "history" / "atlas.json"

MIN_N_DEFAULT = 30        # need enough resolved markets for the cell stat to mean anything
ECE_FLAG = 0.04           # market miscalibration threshold to call a cell "beatable in principle"

# Price bands by distance of the implied price from the extremes — edge tends to live where the
# crowd is near-certain (cheap mistakes) and in the muddy middle. Bucket on raw implied prob.
PRICE_BANDS = [
    (0.00, 0.05, "0-5c"),
    (0.05, 0.15, "5-15c"),
    (0.15, 0.35, "15-35c"),
    (0.35, 0.65, "35-65c"),
    (0.65, 0.85, "65-85c"),
    (0.85, 0.95, "85-95c"),
    (0.95, 1.00, "95-100c"),
]

LIQ_TIERS = [
    (0.0, 500.0, "thin"),
    (500.0, 5000.0, "mid"),
    (5000.0, float("inf"), "deep"),
]


def _price_band(p):
    for lo, hi, name in PRICE_BANDS:
        if lo <= p < hi:
            return name
    return "95-100c"


def _liq_tier(liq):
    for lo, hi, name in LIQ_TIERS:
        if lo <= liq < hi:
            return name
    return "deep"


def _brier(p, outcome):
    return (p - outcome) ** 2


def _agg(rows):
    """Aggregate stats for a list of records."""
    n = len(rows)
    if n == 0:
        return None
    brier = sum(_brier(r["implied_yes"], r["outcome"]) for r in rows) / n
    mean_imp = sum(r["implied_yes"] for r in rows) / n
    mean_out = sum(r["outcome"] for r in rows) / n
    bias = mean_out - mean_imp           # +ve: market UNDER-prices yes; -ve: OVER-prices yes
    # ECE: bin by implied decile, weight |mean_out - mean_imp| per bin.
    bins = defaultdict(list)
    for r in rows:
        bins[min(9, int(r["implied_yes"] * 10))].append(r)
    ece = 0.0
    for b in bins.values():
        bn = len(b)
        bi = sum(x["implied_yes"] for x in b) / bn
        bo = sum(x["outcome"] for x in b) / bn
        ece += (bn / n) * abs(bo - bi)
    return {
        "n": n,
        "market_brier": round(brier, 4),
        "mean_implied": round(mean_imp, 4),
        "mean_outcome": round(mean_out, 4),
        "bias": round(bias, 4),
        "ece": round(ece, 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the market-inefficiency atlas")
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--min-n", type=int, default=MIN_N_DEFAULT)
    args = ap.parse_args()

    path = Path(args.inp)
    if not path.exists():
        print(f"no corpus at {path} — run harvest_history.py first", file=sys.stderr)
        return 2
    rows = []
    for line in path.open():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r.get("implied_yes"), (int, float)) and r.get("outcome") in (0, 1):
            rows.append(r)
    print(f"loaded {len(rows)} usable settled markets")

    overall = _agg(rows)
    print("\n=== AGGREGATE MARKET CALIBRATION (the baseline you must beat) ===")
    print(f"  n={overall['n']}  market_brier={overall['market_brier']}  "
          f"ECE={overall['ece']}  bias={overall['bias']}")

    # Cells: category x price-band x liquidity-tier
    cells = defaultdict(list)
    for r in rows:
        key = (r.get("category", "?"), _price_band(r["implied_yes"]), _liq_tier(r.get("open_interest", 0.0)))
        cells[key].append(r)

    cell_stats = []
    for key, group in cells.items():
        st = _agg(group)
        if st is None or st["n"] < args.min_n:
            continue
        cat, pb, lt = key
        st.update({"category": cat, "price_band": pb, "liquidity": lt})
        st["beatable"] = st["ece"] >= ECE_FLAG
        cell_stats.append(st)

    # Also a coarser category-only view (always reported).
    cat_stats = []
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r.get("category", "?")].append(r)
    for cat, group in by_cat.items():
        st = _agg(group)
        st["category"] = cat
        cat_stats.append(st)
    cat_stats.sort(key=lambda s: -s["ece"])

    print("\n=== BY CATEGORY (sorted by market miscalibration / ECE) ===")
    for s in cat_stats:
        print(f"  {s['category']:<12} n={s['n']:<6} brier={s['market_brier']:<7} "
              f"ECE={s['ece']:<7} bias={s['bias']:+.3f}")

    # Rank beatable cells by an opportunity score: ECE * sqrt(n) (miscalibration with confidence).
    beatable = [s for s in cell_stats if s["beatable"]]
    beatable.sort(key=lambda s: -(s["ece"] * (s["n"] ** 0.5)))
    print(f"\n=== TOP BEATABLE CELLS (market ECE >= {ECE_FLAG}, n >= {args.min_n}) ===")
    print(f"  {'category':<11} {'band':<9} {'liq':<6} {'n':<6} {'brier':<7} {'ECE':<7} {'bias':<7}")
    for s in beatable[:25]:
        print(f"  {s['category']:<11} {s['price_band']:<9} {s['liquidity']:<6} "
              f"{s['n']:<6} {s['market_brier']:<7} {s['ece']:<7} {s['bias']:+.3f}")
    if not beatable:
        print("  (none cleared the bar — the reachable universe may simply be efficient)")

    out = {
        "aggregate": overall,
        "by_category": cat_stats,
        "cells": sorted(cell_stats, key=lambda s: -(s["ece"] * (s["n"] ** 0.5))),
        "params": {"min_n": args.min_n, "ece_flag": ECE_FLAG},
    }
    ATLAS_OUT.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {ATLAS_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
