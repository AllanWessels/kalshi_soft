"""score_recommendations.py — score the tracked trade recommendations against resolution.

Reads data/trade_recommendations.jsonl, looks up each OPEN rec's settled outcome on Kalshi, and
records realized P&L (after fee) + whether the history-calibrated probability beat the market's
(Brier). This is the LIVE accuracy record of the structural edge — the thing that confirms or kills
it on real, forward markets (the backtest was OOS-historical; this is OOS-live).

Idempotent: resolved rows are left as-is; only still-open recs are re-checked. Rewrites the ledger
atomically with any newly-resolved rows scored, then prints the running scoreboard.

Usage: python3 scripts/score_recommendations.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import math

from lib import config
from lib.kalshi_client import KalshiClient, KalshiError

LEDGER_PATH = config.DATA_DIR / "trade_recommendations.jsonl"


def _fee(price: float) -> float:
    return math.ceil(config.KALSHI_FEE_RATE * price * (1 - price) * 100) / 100.0


def _load():
    rows = []
    if LEDGER_PATH.exists():
        for line in LEDGER_PATH.open():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def _score(rec, result):
    """result in {'yes','no'}. Returns scored fields for a 1-unit position."""
    outcome_yes = 1 if result == "yes" else 0
    entry = float(rec["entry_limit"])
    fee = _fee(entry)
    if rec["side"] == "NO":
        payoff = 1 if result == "no" else 0
    else:
        payoff = 1 if result == "yes" else 0
    pnl = round(payoff - entry - fee, 4)
    bc = (float(rec["calibrated_yes"]) - outcome_yes) ** 2
    bm = (float(rec["market_yes"]) - outcome_yes) ** 2
    return {"status": "resolved", "outcome": result, "realized_pnl": pnl,
            "won": pnl > 0, "brier_cal": round(bc, 5), "brier_market": round(bm, 5),
            "beat_market": bc < bm, "cost": round(entry + fee, 4)}


def main() -> int:
    rows = _load()
    if not rows:
        print("no recommendations logged yet (run recommend_trades.py)")
        return 0
    client = KalshiClient(*config.load_secrets())
    newly = 0
    for r in rows:
        if r.get("status") == "resolved":
            continue
        try:
            m = client.get_market(r["ticker"])
        except KalshiError:
            continue
        status = (m.get("status") or "").lower()
        result = (m.get("result") or "").strip().lower()
        if status in ("settled", "finalized") and result in ("yes", "no"):
            r.update(_score(r, result))
            newly += 1

    # atomic rewrite
    tmp = LEDGER_PATH.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(LEDGER_PATH)

    resolved = [r for r in rows if r.get("status") == "resolved"]
    open_n = len(rows) - len(resolved)
    print(f"recommendations: {len(rows)} total | {len(resolved)} resolved | {open_n} open "
          f"({newly} newly scored this run)")
    if resolved:
        pnl = sum(r["realized_pnl"] for r in resolved)
        cost = sum(r["cost"] for r in resolved)
        wins = sum(1 for r in resolved if r["won"])
        beat = sum(1 for r in resolved if r["beat_market"])
        bc = sum(r["brier_cal"] for r in resolved) / len(resolved)
        bm = sum(r["brier_market"] for r in resolved) / len(resolved)
        print("\n=== LIVE RECOMMENDATION SCOREBOARD ===")
        print(f"  win-rate:        {wins}/{len(resolved)} = {wins/len(resolved):.3f}")
        print(f"  realized P&L:    ${pnl:+.2f} on ${cost:.2f} deployed  (ROI {pnl/cost if cost else 0:+.4f})")
        print(f"  beat-market:     {beat}/{len(resolved)} = {beat/len(resolved):.3f}  "
              f"(Brier cal {bc:.4f} vs market {bm:.4f})")
        verdict = "EDGE HOLDING" if pnl > 0 and bc <= bm else ("INSUFFICIENT" if len(resolved) < 15 else "EDGE NOT CONFIRMED")
        print(f"  verdict:         {verdict} (n={len(resolved)}; need >=15 for a real read)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
