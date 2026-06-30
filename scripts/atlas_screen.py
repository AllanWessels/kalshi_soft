"""atlas_screen.py — score live candidates by the history-learned edge (wires the atlas into curation).

For each market in data/candidates.json, look up its cell in the inefficiency atlas + market-
calibration map and report:
  * is the cell BEATABLE (market historically miscalibrated there)?
  * the calibrated probability vs the market price, and the post-fee EV of the implied trade.
Ranks candidates by |edge| so the curation step (curate_watchlist.py) concentrates the watchlist on
cells where history says the crowd is wrong — and flags degenerate efficient markets to skip.

This is advisory (prints a ranked screen); the operator/agent still chooses the adds. Leakage-free:
the edge comes only from PAST settled markets applied to today's prices.

Usage: python3 scripts/atlas_screen.py [--top N]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import math

from lib import config, store
from lib.atlas import CalibrationMap, load_atlas, cell_key


def _fee(price: float) -> float:
    return math.ceil(config.KALSHI_FEE_RATE * price * (1 - price) * 100) / 100.0


def _implied(c: dict):
    # candidates.json carries the market-implied probability directly
    v = c.get("market_implied_probability")
    if isinstance(v, (int, float)) and 0 < v < 1:
        return float(v)
    for k in ("yes_ask", "last_price", "yes_bid"):
        v = c.get(k)
        if isinstance(v, (int, float)) and 0 < v < 1:
            return float(v)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    cmap = CalibrationMap.load()
    atlas = load_atlas()
    beatable = set()
    for s in atlas.get("cells", []):
        if s.get("beatable"):
            beatable.add(f"{s['category']}|{s['price_band']}|{s['liquidity']}")
    if not cmap.cells:
        print("no calibration map yet — run fit_market_calibration.py first", file=sys.stderr)
        return 2

    cands = store.load_candidates()
    scored = []
    for c in cands:
        p = _implied(c)
        if p is None:
            continue
        liq = float(c.get("open_interest", 0) or 0)  # OI as the liquidity proxy candidates carry
        cat = c.get("category", "?")
        info = cmap.calibrate(cat, p, liq)
        p_cal = info["calibrated"]
        fee = _fee(p)
        ev_yes = p_cal - p - fee
        ev_no = (p - p_cal) - fee
        ev, side = (ev_yes, "YES") if ev_yes >= ev_no else (ev_no, "NO")
        key = cell_key(cat, p, liq)
        scored.append({
            "ticker": c.get("ticker"), "title": (c.get("title") or "")[:46],
            "cat": cat, "price": p, "p_cal": p_cal, "ev": ev, "side": side,
            "beatable": key in beatable, "corrected": info["corrected"],
        })

    scored.sort(key=lambda s: -s["ev"])
    print(f"screened {len(scored)} candidates against history-learned edge\n")
    print(f"  {'ticker':<30} {'cat':<10} {'px':<5} {'cal':<5} {'side':<4} {'EV':<7} flags")
    shown = 0
    for s in scored:
        if shown >= args.top:
            break
        if not s["corrected"]:
            continue
        flags = ("BEATABLE " if s["beatable"] else "") + (f"EV+{s['ev']:.2f}" if s["ev"] >= config.MIN_PROFITABLE_EV else "")
        print(f"  {s['ticker']:<30} {s['cat']:<10} {s['price']:<5.2f} {s['p_cal']:<5.2f} "
              f"{s['side']:<4} {s['ev']:+.3f} {flags}")
        shown += 1
    actionable = [s for s in scored if s["corrected"] and s["ev"] >= config.MIN_PROFITABLE_EV]
    print(f"\n{len(actionable)} candidates clear the post-fee EV bar in a history-corrected cell.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
