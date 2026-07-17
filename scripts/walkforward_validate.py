"""walkforward_validate.py — month-by-month out-of-sample validation of the calibration map.

Workstream B2 (PLAN_FOR_OPUS.md): a single random split can flatter a decaying edge — Kalshi's
crowd matures, market makers arrive, cells compress. Walk-forward answers the question the live
screen actually asks: "would cells fitted on the PAST have made money in the NEXT month?"

Method (per fold k): fit the map on all rows with close-month < M_k, then simulate the standard
trade rule on rows closing in M_k — take the corrected side when EV after the Kalshi fee clears
MIN_PROFITABLE_EV, fill at the implied price (upper bound: history has no spread), settle at the
real outcome. Aggregate P&L per CELL across folds.

Output: data/history/walkforward.json — {cells: {cell: {n, pnl, staked, roi, positive, folds}}}.
`atlas.tradeable_cell()` gates the live screen on `positive`; the file failing to exist fails
CLOSED (no screen trades). Rerun this after every harvest/refit.

Usage: python3 scripts/walkforward_validate.py [--in PATH] [--min-ev 0.02] [--min-fold-rows 500]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import math
from collections import defaultdict

from lib import config, schemas
from lib.atlas import CalibrationMap, WALKFORWARD_PATH

DEFAULT_IN = config.DATA_DIR / "history" / "markets.jsonl"


def _fee(price: float) -> float:
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


def _simulate(rows, cmap, min_ev, agg):
    """Trade each row per the standard rule; accumulate per-cell P&L into `agg`."""
    n_trades = 0
    for r in rows:
        p = r["implied_yes"]
        info = cmap.calibrate(r.get("category", "?"), p,
                              r.get("open_interest", 0.0) or 0.0, r.get("duration_days"))
        if not info["corrected"]:
            continue
        p_cal = info["calibrated"]
        ev_yes = p_cal - p - _fee(p)
        ev_no = (p - p_cal) - _fee(1 - p)
        ev, side = (ev_yes, "YES") if ev_yes >= ev_no else (ev_no, "NO")
        if ev < min_ev:
            continue
        cost = (p if side == "YES" else 1 - p)
        cost += _fee(cost)
        won = (r["outcome"] == 1) if side == "YES" else (r["outcome"] == 0)
        pnl = (1.0 - cost) if won else -cost
        cell = agg[info["key"]]
        cell["n"] += 1
        cell["pnl"] += pnl
        cell["staked"] += cost
        n_trades += 1
    return n_trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--min-ev", type=float, default=config.MIN_PROFITABLE_EV)
    ap.add_argument("--min-fold-rows", type=int, default=500,
                    help="skip a test month with fewer rows than this")
    args = ap.parse_args()

    rows = _load(args.inp)
    if not rows:
        print("no corpus — run harvest_history.py first", file=sys.stderr)
        return 2
    months = sorted({(r.get("close_time") or "")[:7] for r in rows if r.get("close_time")})
    print(f"loaded {len(rows)} rows across close-months: {months}")

    agg: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0, "staked": 0.0})
    folds = []
    for i, m in enumerate(months):
        train = [r for r in rows if (r.get("close_time") or "")[:7] < m]
        test = [r for r in rows if (r.get("close_time") or "")[:7] == m]
        if not train or len(test) < args.min_fold_rows:
            print(f"  fold {m}: skipped (train={len(train)}, test={len(test)})")
            continue
        cmap = CalibrationMap.fit(train)
        n_trades = _simulate(test, cmap, args.min_ev, agg)
        folds.append({"month": m, "train": len(train), "test": len(test), "trades": n_trades})
        print(f"  fold {m}: train={len(train)} test={len(test)} trades={n_trades}")

    if not folds:
        print("no usable folds — need >=2 close-months of history", file=sys.stderr)
        return 2

    cells = {}
    for key, c in agg.items():
        roi = (c["pnl"] / c["staked"]) if c["staked"] else 0.0
        cells[key] = {"n": c["n"], "pnl": round(c["pnl"], 3), "staked": round(c["staked"], 3),
                      "roi": round(roi, 4), "positive": c["pnl"] > 0}
    WALKFORWARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    WALKFORWARD_PATH.write_text(json.dumps(
        {"generated_at": schemas.utc_now_iso(), "folds": folds,
         "params": {"min_ev": args.min_ev}, "cells": cells}, indent=2))

    pos = {k: v for k, v in cells.items() if v["positive"]}
    tot_pnl = sum(c["pnl"] for c in cells.values())
    tot_staked = sum(c["staked"] for c in cells.values())
    print(f"\n=== WALK-FORWARD VERDICT ({len(folds)} fold(s)) ===")
    print(f"  overall: {sum(c['n'] for c in cells.values())} trades, pnl {tot_pnl:+.2f} on "
          f"{tot_staked:.2f} staked (ROI {tot_pnl / tot_staked if tot_staked else 0:+.3f})")
    print(f"  cells traded: {len(cells)} | POSITIVE (tradeable): {len(pos)}")
    for k, v in sorted(pos.items(), key=lambda kv: -kv[1]["pnl"])[:20]:
        print(f"    {k:<40} n={v['n']:<5} pnl {v['pnl']:+8.2f}  roi {v['roi']:+.3f}")
    neg = sorted(((k, v) for k, v in cells.items() if not v["positive"]),
                 key=lambda kv: kv[1]["pnl"])[:8]
    if neg:
        print("  worst (blocked from screen):")
        for k, v in neg:
            print(f"    {k:<40} n={v['n']:<5} pnl {v['pnl']:+8.2f}  roi {v['roi']:+.3f}")
    print(f"\nwrote {WALKFORWARD_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
