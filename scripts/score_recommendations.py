"""score_recommendations.py — score the tracked trade recommendations against resolution.

Workstream A (PLAN_FOR_OPUS.md): reads data/trade_recommendations.jsonl, looks up each OPEN
rec's settled outcome on Kalshi, and scores TWO P&L columns via lib/recledger:

  * conservative (OFFICIAL) — fill only if the rec-time orderbook snapshot proves the limit was
    marketable with depth, at the REAL ask; otherwise NO-FILL (excluded from deployed capital).
  * optimistic — the legacy assume-filled-at-entry-limit number (upper bound, footnote only).

Rows logged before fill evidence existed are the `legacy` cohort (fills_unverified) — reported
separately as provisional, never part of the verification bar. This is the LIVE accuracy record
of the structural edge; the A4 verification bar reads exclusively from the verified cohort.

Idempotent: resolved rows are left as-is; only still-open recs are re-checked. Rewrites the
ledger atomically, then prints the official scoreboard + verification-bar progress.

Usage: python3 scripts/score_recommendations.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config, recledger
from lib.kalshi_client import KalshiClient, KalshiError


def _fmt_sb(sb: dict) -> list[str]:
    lines = []
    n = sb["n_scored"]
    if n:
        lines.append(f"  win-rate:        {sb['wins']}/{n} = {sb['win_rate']:.3f}")
        lines.append(f"  realized P&L:    ${sb['pnl']:+.2f} on ${sb['deployed']:.2f} deployed"
                     f"  (ROI {sb['roi']:+.4f})")
        lines.append(f"  beat-market:     {sb['beat_market']}/{n} = {sb['beat_market_rate']:.3f}"
                     f"  (Brier cal {sb['brier_cal']:.4f} vs market {sb['brier_market']:.4f})")
    if sb["n_nofill"]:
        lines.append(f"  no-fill:         {sb['n_nofill']} rec(s) never marketable at the limit "
                     f"— excluded from deployed capital")
    if not lines:
        lines.append(f"  (no scored recs in this cohort yet; resolved={sb['n_resolved']})")
    return lines


def main() -> int:
    rows = recledger.load_rows()
    if not rows:
        print("no recommendations logged yet (run recommend_trades.py)")
        return 0

    vpol = recledger.load_verification_policy()
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
            r.update(recledger.score_resolved(r, result,
                                              min_depth=vpol["min_fill_depth_units"]))
            newly += 1

    migrated = recledger.tag_legacy(rows)   # one-time cohort backfill on pre-A1 rows
    recledger.write_rows(rows)

    resolved = [r for r in rows if r.get("status") == "resolved"]
    open_n = len(rows) - len(resolved)
    print(f"recommendations: {len(rows)} total | {len(resolved)} resolved | {open_n} open "
          f"({newly} newly scored this run"
          + (f"; {migrated} row(s) cohort-tagged" if migrated else "") + ")")

    print("\n=== OFFICIAL SCOREBOARD — verified fills, conservative column ===")
    for line in _fmt_sb(recledger.scoreboard(rows, "verified", "conservative")):
        print(line)

    legacy = recledger.scoreboard(rows, "legacy", "optimistic")
    if legacy["n_resolved"]:
        print("\n=== PROVISIONAL — legacy cohort (fills UNVERIFIED, optimistic column) ===")
        for line in _fmt_sb(legacy):
            print(line)
        print("  caveat: logged before fill evidence existed; NEVER counts toward verification.")

    stress = recledger.compute_stress(rows)
    vs = recledger.verification_status(rows, vpol, stress)
    print(f"\n=== VERIFICATION BAR (PLAN_FOR_OPUS §A4) — "
          f"{'PASSED' if vs['verified'] else 'not passed'} ===")
    for name, c in vs["criteria"].items():
        print(f"  [{'x' if c['pass'] else ' '}] {name}: target {c['target']} | current {c['current']}")
    print(f"  {vs['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
