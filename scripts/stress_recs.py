"""stress_recs.py — Monte Carlo tail-risk stress of the structural-edge basket (Workstream A2).

The 13/13-illusion killer: a high-win-rate longshot-fade basket looks perfect right up until the
first tail event (one YES hit costs ~0.7-0.9/contract and erases 3-8 wins). This script simulates
the basket FORWARD — resampling rec profiles from the ledger and letting each simulated rec win
with its side's CALIBRATED probability — and reports what the record cannot yet show:

  * P(ROI < 0) over the next N recs           (is the apparent edge fragile?)
  * ROI distribution (mean / p5 / p50 / p95)
  * expected max drawdown (per 1-unit stakes)
  * break-even win rate vs the basket's mean cost

Deterministic under --seed so report numbers are reproducible. Also embedded in the PDF report
(lib/report.py, Structural Edge section) on every build.

Usage: python3 scripts/stress_recs.py [--n-future 100] [--trials 5000] [--seed 7]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

from lib import recledger


def main() -> int:
    ap = argparse.ArgumentParser(description="Monte Carlo stress of the rec basket")
    ap.add_argument("--n-future", type=int, default=100)
    ap.add_argument("--trials", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rows = recledger.load_rows()
    s = recledger.compute_stress(rows, n_future=args.n_future, trials=args.trials,
                                 seed=args.seed)
    if s is None:
        print("no rec profiles in the ledger yet (run recommend_trades.py first)")
        return 0

    print(f"=== BASKET TAIL STRESS — {s['n_future']} future recs x {s['trials']} trials "
          f"(profiles n={s['n_profiles']}, seed={s['seed']}) ===")
    print(f"  P(ROI < 0):            {s['p_loss']:.1%}")
    print(f"  ROI mean:              {s['roi_mean']:+.3f}")
    print(f"  ROI p5 / p50 / p95:    {s['roi_p5']:+.3f} / {s['roi_p50']:+.3f} / {s['roi_p95']:+.3f}")
    print(f"  expected max drawdown: {s['expected_max_drawdown']:.2f} units")
    print(f"  break-even win rate:   {s['breakeven_win_rate']:.3f} "
          f"(mean cost/contract ${s['mean_cost']:.2f})")
    print("\n  Read: if realized hit rate ever exceeds the calibrated expectation at n>=30, the "
          "map is optimistic — that is a cell kill signal (PLAN_FOR_OPUS §A2/§A4).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
