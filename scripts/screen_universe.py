"""screen_universe.py — screen the ENTIRE open Kalshi exchange for structural-edge trades.

Workstream B1 (PLAN_FOR_OPUS.md): the structural edge is mechanical (price + OI + duration +
category — no LLM, no retrieval), so its screen must not be throttled by the 150-candidate
watchlist funnel that recommend_trades.py inherits. This script walks every open market on the
exchange (~a few API pages at limit=1000), maps each into its atlas cell, and emits recs.

Gate — ALL of (deliberately conservative; every layer earned its place):
  * category maps to a harvested canon category (uncorrected categories can't calibrate)
  * calibration map has a qualified cell correction (granular first, coarse fallback)
  * the MATCHED cell is walk-forward POSITIVE (atlas.tradeable_cell — fails closed if
    walkforward.json is missing; run walkforward_validate.py after every refit)
  * open interest in the tradeable MID band (500-5k) — thin=unfillable, deep=efficient
  * EV positive after the Kalshi fee AND crossing half the bid/ask spread
  * cell not KILLED and no global halt (lib/recledger, Workstream A4)
Every emitted rec carries a rec-time orderbook snapshot (fill evidence, Workstream A1) and lands
in the same ledger with the same per-(ticker, day) idempotency. Still 100% PAPER.

Usage: python3 scripts/screen_universe.py [--max N] [--no-log]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import datetime
import json
import math

from lib import config, schemas, atlas, recledger
from lib.kalshi_client import KalshiClient, KalshiError, market_quote

LEDGER_PATH = config.DATA_DIR / "trade_recommendations.jsonl"

# Kalshi series category -> canonical harvested category (same map as harvest_history.py;
# B3 extends the harvest to more categories, at which point they join this map).
KALSHI_CAT_TO_CANON = {
    "Politics": "politics",
    "Elections": "politics",
    "World": "politics",
    "Economics": "economy",
    "Entertainment": "culture",
    "Mentions": "statements",
}


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


def _duration_days(m: dict):
    try:
        o = datetime.datetime.fromisoformat((m.get("open_time") or "").replace("Z", "+00:00"))
        c = datetime.datetime.fromisoformat((m.get("close_time") or "").replace("Z", "+00:00"))
        return round((c - o).total_seconds() / 86400.0, 3)
    except (ValueError, TypeError):
        return None


def _days_to_resolve(m: dict, now: datetime.datetime):
    """Days until the market's TRUE resolution (expected_expiration_time, fallback
    close_time). None if unparseable. Used to concentrate the paper book on markets that
    SETTLE soon so the A4 verification bar accrues resolved fills in weeks, not months —
    otherwise year-out longshots (award seasons, 2027/2028 politics) freeze the scoreboard."""
    for key in ("expected_expiration_time", "close_time"):
        raw = m.get(key)
        if not raw:
            continue
        try:
            t = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return (t - now).total_seconds() / 86400.0
        except (ValueError, TypeError):
            continue
    return None


def _series_category_map(client: KalshiClient) -> dict:
    """{series_ticker: canon_category} for all series in harvested categories (one API call)."""
    out = {}
    for s in (client.get_series().get("series") or []):
        canon = KALSHI_CAT_TO_CANON.get(s.get("category"))
        if canon:
            out[s.get("ticker")] = canon
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Full-universe structural-edge screen")
    ap.add_argument("--max", type=int, default=12,
                    help="cap on recs logged this cycle (exposure control, not discovery)")
    ap.add_argument("--no-log", action="store_true")
    args = ap.parse_args()

    cm = atlas.CalibrationMap.load()
    if not cm.cells:
        print("no calibration map — run fit_market_calibration.py first", file=sys.stderr)
        return 2
    wf = atlas.load_walkforward()
    if not wf:
        print("no walk-forward record — run walkforward_validate.py first (screen fails CLOSED)",
              file=sys.stderr)
        return 2

    ledger_rows = recledger.load_rows()
    vpol = recledger.load_verification_policy()
    halt = recledger.global_halt(ledger_rows, vpol)
    if halt:
        print(f"GLOBAL HALT: trailing-{halt['trailing_n']} conservative ROI "
              f"{halt['trailing_roi']:+.3f} < floor {halt['floor']:+.2f} — screen paused.")
        return 0
    killed = recledger.killed_cells(ledger_rows, vpol)
    if killed:
        print("killed cells: " + ", ".join(killed))

    client = KalshiClient(*config.load_secrets())
    series_cat = _series_category_map(client)
    print(f"series in harvested categories: {len(series_cat)}")

    today = schemas.utc_now_iso()[:10]
    now = datetime.datetime.now(datetime.timezone.utc)
    seen = _existing_keys()
    scanned = softcat = near_horizon = corrected = wf_pos = in_band = 0
    recs = []
    try:
        for m in client.iter_markets(status="open"):
            scanned += 1
            series = (m.get("event_ticker") or m.get("ticker") or "").split("-")[0]
            cat = series_cat.get(series)
            if not cat:
                continue
            softcat += 1
            # Settlement-horizon gate (2026-07-24, rescope-to-fast): only screen markets that
            # RESOLVE within MAX_DAYS_TO_RESOLVE so the verified scoreboard accrues in weeks.
            dtr = _days_to_resolve(m, now)
            if dtr is None or dtr < config.MIN_DAYS_TO_CLOSE or dtr > config.MAX_DAYS_TO_RESOLVE:
                continue
            near_horizon += 1
            q = market_quote(m)
            yb, ya = q.get("yes_bid"), q.get("yes_ask")
            if not (yb and ya and 0 < yb <= ya < 1):
                continue
            p = round((yb + ya) / 2.0, 4)
            oi = 0.0
            try:
                oi = float(m.get("open_interest_fp") or m.get("open_interest") or 0)
            except (TypeError, ValueError):
                pass
            dur = _duration_days(m)
            info = cm.calibrate(cat, p, oi, dur)
            if not info["corrected"]:
                continue
            corrected += 1
            if not atlas.tradeable_cell(info["key"], wf):
                continue
            wf_pos += 1
            if info["key"] in killed:
                continue
            if atlas.liq_tier(oi) != "mid":       # tradeable band only (fills die outside)
                continue
            in_band += 1
            p_cal = info["calibrated"]
            half_spread = round((ya - yb) / 2.0, 4)
            ev_yes = p_cal - p - _fee(p)
            ev_no = (p - p_cal) - _fee(1 - p)
            ev, side = (ev_yes, "YES") if ev_yes >= ev_no else (ev_no, "NO")
            ev_net = ev - half_spread
            if ev_net < config.MIN_PROFITABLE_EV:
                continue
            entry = (p + half_spread) if side == "YES" else (round(1 - p, 4) + half_spread)
            recs.append({
                "ticker": m.get("ticker"), "category": cat,
                "title": (m.get("title") or "")[:70],
                "side": side, "market_yes": p, "calibrated_yes": round(p_cal, 4),
                "entry_limit": round(min(0.99, entry), 4), "ev_net": round(ev_net, 4),
                "open_interest": round(oi, 1), "spread_cents": round((ya - yb) * 100, 1),
                "duration_days": dur, "cell": info["key"], "granularity": info["granularity"],
                "close_time": m.get("close_time"),
                "expected_expiration_time": m.get("expected_expiration_time"),
                "days_to_resolve": round(dtr, 1), "source": "screen_universe",
            })
    except KalshiError as exc:
        print(f"scan aborted early: {exc} — emitting what was found", file=sys.stderr)

    print(f"scanned {scanned} open markets | soft-category {softcat} | "
          f"resolve<={config.MAX_DAYS_TO_RESOLVE}d {near_horizon} | corrected-cell "
          f"{corrected} | walk-forward-positive {wf_pos} | mid-OI {in_band} | "
          f"+EV after costs {len(recs)}")

    recs.sort(key=lambda r: -r["ev_net"])
    recs = recs[: args.max]

    # Fill evidence (Workstream A1) for every rec we will emit.
    for r in recs:
        r["fill_evidence"] = recledger.snapshot_fill_evidence(
            client, r["ticker"], r["side"], r["entry_limit"])

    print(f"\n=== UNIVERSE STRUCTURAL-EDGE BASKET ({len(recs)} recs) ===")
    print(f"  {'ticker':<34} {'side':<4} {'yes':<5} {'fair':<5} {'entry':<6} {'EVnet':<7} "
          f"{'fill?':<6} cell")
    logged = 0
    with (LEDGER_PATH.open("a") if not args.no_log else open("/dev/null", "a")) as lf:
        for r in recs:
            ev = r.get("fill_evidence") or {}
            fill = "now" if ev.get("fillable_now") else ("rest" if ev else "?")
            print(f"  {r['ticker']:<34} {r['side']:<4} {r['market_yes']:<5.2f} "
                  f"{r['calibrated_yes']:<5.2f} {r['entry_limit']:<6.2f} {r['ev_net']:+.3f} "
                  f"{fill:<6} {r['cell']}")
            if args.no_log or (r["ticker"], today) in seen:
                continue
            row = dict(r)
            row.update({"ts": schemas.utc_now_iso(), "status": "open", "outcome": None,
                        "realized_pnl": None,
                        "cohort": "verified" if r.get("fill_evidence") else "legacy"})
            lf.write(json.dumps(row) + "\n")
            logged += 1
    if not args.no_log:
        print(f"\nlogged {logged} new rec(s) -> {LEDGER_PATH.name}")
    print("\nRisk note: correlated longshot-fade basket — size small & equal, cap total "
          "exposure, treat as ONE thematic position. PAPER ONLY until the A4 bar passes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
