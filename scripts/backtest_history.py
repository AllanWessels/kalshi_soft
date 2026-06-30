"""backtest_history.py — the honest profitability verdict, out-of-sample, after fees.

The existential question — "is there money in the markets we can reach?" — answered on thousands
of settled markets instead of 31, with strict no-leakage discipline:

  1. Deterministic 70/30 train/test split by ticker hash.
  2. Fit the per-cell market-calibration map on TRAIN only.
  3. On TEST, walk each market. Using the calibrated probability (our only "edge"), take the side
     with positive expected value after the Kalshi fee, at the market price. Settle at the real
     outcome. Sum realized P&L, contracts, ROI.

Assumptions stated plainly: fills at the implied price (no extra spread/slippage modeled beyond
the fee), 1 contract per signal, fee = ceil(0.07*p*(1-p)) rounded up to the cent. This is an
UPPER bound on a structural-edge strategy; if it does not clear fees here, it will not live.

Usage: python3 scripts/backtest_history.py [--in PATH] [--min-ev 0.02] [--by-cell]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import math
from collections import defaultdict

from lib import config
from lib.atlas import CalibrationMap, cell_key

DEFAULT_IN = config.DATA_DIR / "history" / "markets.jsonl"


def _fee(price: float) -> float:
    """Kalshi trading fee per contract: ceil(FEE_RATE * price * (1-price) * 100) cents."""
    return math.ceil(config.KALSHI_FEE_RATE * price * (1 - price) * 100) / 100.0


def _load(path):
    rows = []
    for line in Path(path).open():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r.get("implied_yes"), (int, float)) and r.get("outcome") in (0, 1):
            rows.append(r)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--min-ev", type=float, default=config.MIN_PROFITABLE_EV)
    ap.add_argument("--by-cell", action="store_true", help="break P&L down by cell")
    args = ap.parse_args()

    rows = _load(args.inp)
    if not rows:
        print("no corpus — run harvest_history.py first", file=sys.stderr)
        return 2

    train = [r for r in rows if (hash(r.get("ticker", "")) % 10) < 7]
    test = [r for r in rows if (hash(r.get("ticker", "")) % 10) >= 7]
    cmap = CalibrationMap.fit(train)
    print(f"loaded {len(rows)} | train {len(train)} | test {len(test)} | "
          f"calibrated cells {len(cmap.cells)} | min_ev {args.min_ev}")

    tot_pnl = tot_cost = n_trades = n_win = 0.0
    cell_pnl = defaultdict(lambda: [0.0, 0.0, 0])  # key -> [pnl, cost, n]

    for r in test:
        p = r["implied_yes"]
        out = r["outcome"]
        liq = r.get("open_interest", 0.0)
        p_cal = cmap.calibrate(r.get("category", "?"), p, liq)["calibrated"]
        fee = _fee(p)
        ev_yes = p_cal - p - fee            # buy YES at p
        ev_no = (p - p_cal) - fee           # buy NO at (1-p)
        side = None
        if ev_yes >= args.min_ev and ev_yes >= ev_no:
            side, cost, profit = "yes", p + fee, (out - p - fee)
        elif ev_no >= args.min_ev:
            side, cost, profit = "no", (1 - p) + fee, ((1 - out) - (1 - p) - fee)
        if side is None:
            continue
        tot_pnl += profit
        tot_cost += cost
        n_trades += 1
        n_win += 1 if profit > 0 else 0
        if args.by_cell:
            k = cell_key(r.get("category", "?"), p, liq)
            cell_pnl[k][0] += profit
            cell_pnl[k][1] += cost
            cell_pnl[k][2] += 1

    print("\n=== OUT-OF-SAMPLE BACKTEST (test split, after fees) ===")
    if n_trades == 0:
        print("  NO trades cleared the EV bar — no structural edge survives fees here.")
        return 0
    roi = tot_pnl / tot_cost if tot_cost else 0.0
    print(f"  trades: {int(n_trades)}   win-rate: {n_win/n_trades:.3f}")
    print(f"  P&L: ${tot_pnl:+.2f}   capital deployed: ${tot_cost:.2f}")
    print(f"  ROI after fees: {roi:+.4f}  ({'PROFITABLE' if tot_pnl > 0 else 'LOSS'})")

    if args.by_cell:
        print("\n=== P&L BY CELL (>=10 trades, sorted by ROI) ===")
        ranked = []
        for k, (pnl, cost, n) in cell_pnl.items():
            if n >= 10 and cost > 0:
                ranked.append((k, pnl, cost, n, pnl / cost))
        ranked.sort(key=lambda x: -x[4])
        print(f"  {'cell':<34} {'n':<5} {'pnl':<9} {'roi':<8}")
        for k, pnl, cost, n, r in ranked[:20]:
            print(f"  {k:<34} {n:<5} ${pnl:<+8.2f} {r:+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
