"""scan_coherence.py — harvest crowd INCOHERENCE: probability-axiom violations that clear fees.

Workstream B4 (PLAN_FOR_OPUS.md): the purest wisdom-of-crowds trades — no model, no opinion,
just enforcing arithmetic the crowd violated. Two checks over open events:

  1. DUTCH-NO (auto-logged): in a mutually-exclusive event with k legs, buying NO on every leg
     costs sum(no_ask_i + fee_i) and pays AT LEAST k-1 (exactly one YES if the event is also
     exhaustive; ALL-NO pays k if it is not — the floor holds either way, which is why only the
     NO side is auto-logged). Edge = (k-1) - cost, flagged when it clears MIN_PROFITABLE_EV.
  2. DUTCH-YES (report-only): sum(yes_ask_i + fee_i) < 1 pays exactly 1 ONLY if some leg must
     resolve YES — exhaustiveness is not machine-verifiable from the API, so this is surfaced
     for a human eye, never auto-logged.
  3. Bracket monotonicity (report-only): within one event, "above X" threshold markets must have
     P(above x) non-increasing in x. Violations are surfaced; resolution semantics vary too much
     to trade them mechanically.

Dutch-NO baskets land in the same ledger (one row per LEG, shared arb_group id, cell
"arb|dutch_no") with rec-time fill evidence per leg — scored on resolution like any other rec,
so the arb engine builds its own verified record and is killable by the same A4 switches.

Usage: python3 scripts/scan_coherence.py [--max-baskets N] [--no-log]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import math
import re

from lib import config, schemas, recledger
from lib.kalshi_client import KalshiClient, KalshiError, market_quote

LEDGER_PATH = config.DATA_DIR / "trade_recommendations.jsonl"


def _fee(price: float) -> float:
    return math.ceil(config.KALSHI_FEE_RATE * price * (1 - price) * 100) / 100.0


def _existing_keys() -> set:
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


# Threshold suffixes look like "-T0.0", "-T-0.1" (marker immediately after a hyphen). T means
# "more than X" (confirmed semantics — our own CPI markets). A bare "-2" is an OUTCOME INDEX and
# "-B2.8" is a BETWEEN-bracket (a density, not a CDF — monotonicity does NOT apply), so both are
# excluded; only T-thresholds form a comparable P(>x) ladder.
_THRESH_RE = re.compile(r"-T(-?\d+(?:\.\d+)?)$")


def _monotonicity_flags(event: dict, quotes: dict) -> list[str]:
    """Flag 'above X' bracket incoherence inside one event (report-only)."""
    pts = []
    for m in event.get("markets") or []:
        t = m.get("ticker") or ""
        mm = _THRESH_RE.search(t)
        q = quotes.get(t)
        if not mm or not q:
            continue
        yb, ya = q.get("yes_bid"), q.get("yes_ask")
        if yb and ya and 0 < yb <= ya < 1:
            pts.append((float(mm.group(1)), (yb + ya) / 2, t))
    if len(pts) < 3:
        return []
    pts.sort()
    flags = []
    # P(above x) should not INCREASE with x by more than spread noise (2c tolerance).
    for (x1, p1, t1), (x2, p2, t2) in zip(pts, pts[1:]):
        if p2 > p1 + 0.02:
            flags.append(f"{event.get('event_ticker')}: P(>{x2})={p2:.2f} > P(>{x1})={p1:.2f} "
                         f"({t2} vs {t1})")
    return flags


def main() -> int:
    ap = argparse.ArgumentParser(description="Coherence/arb scan over open events")
    ap.add_argument("--max-baskets", type=int, default=5)
    ap.add_argument("--max-events", type=int, default=None,
                    help="stop after N events (smoke tests; full scan when omitted)")
    ap.add_argument("--no-log", action="store_true")
    args = ap.parse_args()

    client = KalshiClient(*config.load_secrets())
    today = schemas.utc_now_iso()[:10]
    seen = _existing_keys()

    n_events = n_mece = 0
    dutch_no, dutch_yes_reports, mono_flags = [], [], []
    try:
        for ev in client.iter_events(status="open", with_nested_markets=True):
            n_events += 1
            if args.max_events and n_events > args.max_events:
                print(f"(stopped at --max-events {args.max_events})")
                break
            markets = [m for m in (ev.get("markets") or [])
                       if (m.get("status") or "").lower() == "active"]
            if len(markets) < 2:
                continue
            quotes = {m.get("ticker"): market_quote(m) for m in markets}
            mono_flags.extend(_monotonicity_flags(ev, quotes))
            if not ev.get("mutually_exclusive"):
                continue
            n_mece += 1
            legs = []
            ok = True
            for m in markets:
                q = quotes[m.get("ticker")]
                na, ya = q.get("no_ask"), q.get("yes_ask")
                if not (na and ya and 0 < na < 1 and 0 < ya < 1):
                    ok = False   # partial books make the arithmetic fake
                    break
                legs.append((m, na, ya))
            if not ok:
                continue
            k = len(legs)
            cost_no = sum(na + _fee(na) for _, na, _ in legs)
            edge_no = (k - 1) - cost_no
            if edge_no >= config.MIN_PROFITABLE_EV:
                dutch_no.append((ev, legs, edge_no))
            cost_yes = sum(ya + _fee(ya) for _, _, ya in legs)
            edge_yes = 1 - cost_yes
            if edge_yes >= config.MIN_PROFITABLE_EV:
                dutch_yes_reports.append(
                    f"{ev.get('event_ticker')}: sum(yes_ask+fee)={cost_yes:.3f} over {k} legs "
                    f"-> edge {edge_yes:+.3f} IF exhaustive (verify by hand — not auto-logged)")
    except KalshiError as exc:
        print(f"scan aborted early: {exc} — reporting what was found", file=sys.stderr)

    print(f"events scanned: {n_events} | mutually-exclusive with full books: {n_mece} | "
          f"dutch-NO baskets: {len(dutch_no)} | dutch-YES (report-only): "
          f"{len(dutch_yes_reports)} | monotonicity flags: {len(mono_flags)}")

    dutch_no.sort(key=lambda t: -t[2])
    dutch_no = dutch_no[: args.max_baskets]
    logged = 0
    with (LEDGER_PATH.open("a") if not args.no_log else open("/dev/null", "a")) as lf:
        for ev, legs, edge in dutch_no:
            et = ev.get("event_ticker")
            print(f"\nDUTCH-NO {et}: k={len(legs)} guaranteed edge {edge:+.3f}/basket")
            group = f"dutchno-{et}-{today}"
            for m, na, _ in legs:
                ev_snap = recledger.snapshot_fill_evidence(client, m.get("ticker"), "NO", na)
                print(f"  BUY NO {m.get('ticker'):<34} @ {na:.2f} "
                      f"{'(fillable)' if (ev_snap or {}).get('fillable_now') else '(check book)'}")
                if args.no_log or (m.get("ticker"), today) in seen:
                    continue
                lf.write(json.dumps({
                    "ticker": m.get("ticker"), "category": "arb",
                    "title": (m.get("title") or "")[:70], "side": "NO",
                    "market_yes": round(1 - na, 4), "calibrated_yes": round(1 - na, 4),
                    "entry_limit": round(na, 4), "ev_net": round(edge / len(legs), 4),
                    "cell": "arb|dutch_no", "arb_group": group, "arb_k": len(legs),
                    "close_time": m.get("close_time"), "source": "scan_coherence",
                    "fill_evidence": ev_snap, "ts": schemas.utc_now_iso(),
                    "status": "open", "outcome": None, "realized_pnl": None,
                    "cohort": "verified" if ev_snap else "legacy",
                }) + "\n")
                logged += 1
    if dutch_yes_reports:
        print("\nDUTCH-YES candidates (REPORT ONLY — exhaustiveness unverified):")
        for line in dutch_yes_reports[:10]:
            print("  " + line)
    if mono_flags:
        print("\nBracket monotonicity violations (report only):")
        for line in mono_flags[:10]:
            print("  " + line)
    if not args.no_log:
        print(f"\nlogged {logged} dutch-NO leg(s) -> {LEDGER_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
