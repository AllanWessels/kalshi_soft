"""recommend_trades.py — turn the validated structural edge into an explicit, TRACKED trade basket.

The forecaster loses to the market, so its leans are (correctly) gated to ~zero. But the history-
learned market-calibration edge (lib/atlas) BEAT the market out-of-sample and nets positive after
fees in the mid-liquidity band. This script routes THAT edge into actual position recommendations
every cycle, and — critically — appends each one to a ledger so its real accuracy is scored on
resolution (score_recommendations.py). No more silent zero-trade cycles.

Gate (deliberately conservative; the URAN lesson):
  * cell has a real, shrunk history correction (corrected=True)
  * MID open-interest tier (OI 500-5k) — the band proven +ROI out-of-sample; thin=unfillable,
    deep=efficient (edge dies)
  * EV positive after BOTH the Kalshi fee AND crossing half the bid/ask spread
  * cell not KILLED and no global halt active (lib/recledger kill switches, Workstream A4)

Fill evidence (Workstream A1): every logged rec carries an orderbook snapshot taken at rec time
(best bids, the ask our side must cross, depth, fillable_now) so score_recommendations.py can
score a CONSERVATIVE fills-evidenced P&L column — the official number — alongside the optimistic
assume-filled one.

Ledger: data/trade_recommendations.jsonl (one row per rec, idempotent per ticker+date).

Usage: python3 scripts/recommend_trades.py [--max N] [--no-log]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import math

from lib import config, store, schemas, atlas, recledger
from lib.kalshi_client import KalshiClient

LEDGER_PATH = config.DATA_DIR / "trade_recommendations.jsonl"


def _fee(price: float) -> float:
    return math.ceil(config.KALSHI_FEE_RATE * price * (1 - price) * 100) / 100.0


def _implied(c: dict):
    v = c.get("market_implied_probability")
    if isinstance(v, (int, float)) and 0 < v < 1:
        return float(v)
    return None


def _existing_keys() -> set:
    """(ticker, date) already in the ledger, so re-running a day is idempotent."""
    keys = set()
    if LEDGER_PATH.exists():
        for line in LEDGER_PATH.open():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                keys.add((r.get("ticker"), (r.get("ts") or "")[:10]))
            except ValueError:
                continue
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit + log the structural-edge trade basket")
    ap.add_argument("--max", type=int, default=12)
    ap.add_argument("--no-log", action="store_true", help="print only; do not append to the ledger")
    args = ap.parse_args()

    cm = atlas.CalibrationMap.load()
    if not cm.cells:
        print("no calibration map — run fit_market_calibration.py first", file=sys.stderr)
        return 2

    # Workstream A4 kill switches — precommitted, enforced before any screening.
    ledger_rows = recledger.load_rows()
    vpol = recledger.load_verification_policy()
    halt = recledger.global_halt(ledger_rows, vpol)
    if halt:
        print(f"GLOBAL HALT: trailing-{halt['trailing_n']} conservative ROI "
              f"{halt['trailing_roi']:+.3f} < floor {halt['floor']:+.2f} — no recommendations "
              f"this cycle (investigate per PLAN_FOR_OPUS §A4).")
        return 0
    killed = recledger.killed_cells(ledger_rows, vpol)
    if killed:
        print("killed cells (excluded from screen): " +
              ", ".join(f"{c} (n={s['n_scored']}, roi={s['roi']:+.3f})"
                        for c, s in killed.items()))

    today = schemas.utc_now_iso()[:10]
    seen = _existing_keys()
    recs = []
    for c in store.load_candidates():
        p = _implied(c)
        if p is None:
            continue
        oi = float(c.get("open_interest", 0) or 0)
        if atlas.liq_tier(oi) != "mid":          # tradeable band only (proven +ROI OOS)
            continue
        cat = c.get("category", "?")
        info = cm.calibrate(cat, p, oi)
        if not info["corrected"]:
            continue
        if info["key"] in killed:                  # A4 per-cell kill switch
            continue
        p_cal = info["calibrated"]
        fee = _fee(p)
        half_spread = float(c.get("spread_cents", 0) or 0) / 200.0
        ev_yes = p_cal - p - fee
        ev_no = (p - p_cal) - fee
        ev, side = (ev_yes, "YES") if ev_yes >= ev_no else (ev_no, "NO")
        ev_net = ev - half_spread
        if ev_net < config.MIN_PROFITABLE_EV:
            continue
        # entry limit: cross at most half the spread into the book
        entry = (p + half_spread) if side == "YES" else (round(1 - p, 4) + half_spread)
        recs.append({
            "ticker": c.get("ticker"), "category": cat, "title": (c.get("title") or "")[:70],
            "side": side, "market_yes": round(p, 4), "calibrated_yes": round(p_cal, 4),
            "entry_limit": round(min(0.99, entry), 4), "ev_net": round(ev_net, 4),
            "open_interest": round(oi, 1), "spread_cents": c.get("spread_cents"),
            "cell": info["key"], "resolve_by": c.get("resolve_time") or c.get("close_time"),
        })

    recs.sort(key=lambda r: -r["ev_net"])
    recs = recs[: args.max]

    # Workstream A1: fill evidence — snapshot the live orderbook for every rec we will log,
    # so the conservative (official) scoring column has real marketability data to stand on.
    client = KalshiClient(*config.load_secrets())
    for r in recs:
        r["fill_evidence"] = recledger.snapshot_fill_evidence(
            client, r["ticker"], r["side"], r["entry_limit"])

    print(f"=== STRUCTURAL-EDGE TRADE BASKET ({len(recs)} recs, mid-liquidity, +EV after fee+spread) ===")
    print(f"  {'ticker':<32} {'side':<4} {'yes':<5} {'fair':<5} {'entry':<6} {'EVnet':<7} {'fill?':<6} cell")
    logged = 0
    with (LEDGER_PATH.open("a") if not args.no_log else open("/dev/null", "a")) as lf:
        for r in recs:
            ev = r.get("fill_evidence") or {}
            fill = "now" if ev.get("fillable_now") else ("rest" if ev else "?")
            print(f"  {r['ticker']:<32} {r['side']:<4} {r['market_yes']:<5.2f} "
                  f"{r['calibrated_yes']:<5.2f} {r['entry_limit']:<6.2f} {r['ev_net']:+.3f} "
                  f"{fill:<6} {r['cell']}")
            if args.no_log:
                continue
            if (r["ticker"], today) in seen:
                continue
            row = dict(r); row.update({"ts": schemas.utc_now_iso(), "status": "open",
                                       "outcome": None, "realized_pnl": None,
                                       "cohort": "verified" if r.get("fill_evidence") else "legacy"})
            lf.write(json.dumps(row) + "\n")
            logged += 1
    if not args.no_log:
        print(f"\nlogged {logged} new recommendation(s) -> {LEDGER_PATH.name} "
              f"(scored on resolution by score_recommendations.py)")
    print("\nRisk note: correlated longshot-fade basket — size small & equal per market, cap total "
          "exposure, treat as ONE thematic position. High win-rate, tail risk on the rare YES.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
