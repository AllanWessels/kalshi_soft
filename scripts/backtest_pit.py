"""backtest_pit.py — point-in-time (as-of) backtest of the LLM super-team.

Honest backtest of a retrieval forecaster on SETTLED markets, per the chronological
method: reconstruct the information timeline from timestamped sources, forecast at
checkpoints using ONLY sources published <= that checkpoint, and score vs the known
outcome. The market price is NEVER shown to the forecaster; it is compared only after.

Two leakage channels, handled explicitly:
  1. RETRIEVAL leakage ("the web knows"): closed HARD here — GNews is queried per
     checkpoint with Google's `before:<date>` operator AND every item is re-filtered
     on its parsed pubDate <= checkpoint. Undated items are DROPPED (can't prove <=T).
     Wikipedia/live pages are excluded (today's copy reflects the outcome).
  2. PARAMETRIC leakage ("the weights know"): MEASURED, not assumed — a memory-only
     probe (ensemble with EMPTY evidence) runs per event. If it already sits near the
     truth with high confidence, the weights are leaking -> flag/drop that event.

This is a PILOT harness (few events, small ensemble) whose job is to prove the timeline
is honestly reconstructable and to measure the parametric-leakage rate before scaling.

Usage: python3 scripts/backtest_pit.py [--checkpoints 14,7,3,1] [--personas standard,outside,inside]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import local_llm, retrieval, scoring, config  # noqa: E402

OUT_DIR = config.DATA_DIR / "backtest_pit"

# Pilot event set — all settled June 2026 (after the local model's training cutoff, so
# parametric leakage is expected LOW; the memory-only probe verifies per event).
PILOT_EVENTS = [
    {"ticker": "KXWATSONRNC", "outcome": 0, "implied_yes": 0.66,
     "close_time": "2026-06-29T14:21:54Z",
     "question": "Will SCOTUS bar counting mail ballots received after Election Day?",
     "query": "Supreme Court mail ballots received after election day ruling"},
    {"ticker": "KXPERSONPUBLIC-26JUL01-JROB", "outcome": 1, "implied_yes": 0.18,
     "close_time": "2026-06-29T19:12:48Z",
     "question": "Will John Roberts be seen in public before Jul 1, 2026?",
     "query": "Chief Justice John Roberts public appearance June 2026"},
    {"ticker": "KXLABORANNOUNCE-26-AUG01", "outcome": 1, "implied_yes": 0.42,
     "close_time": "2026-06-30T03:21:45Z",
     "question": ("Will Donald Trump issue any official announcement (e.g., Truth Social "
                  "post) on naming his nominee for Labor Secretary before Aug 1, 2026?"),
     "query": "Trump nominee Labor Secretary announcement"},
]

PERSONAS = ["standard", "outside", "inside"]


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _parse_pubdate(s: str):
    """RSS pubDate -> aware datetime, or None if unparseable."""
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def capture_sources_asof(query: str, checkpoint: datetime, max_results: int = 20) -> list[dict]:
    """GNews with `before:<checkpoint>`, then HARD-filter parsed pubDate <= checkpoint.
    Undated items are dropped. Returns dated, sorted (oldest-first) source rows."""
    before = checkpoint.strftime("%Y-%m-%d")
    q = f"{query} before:{before}"
    rows = retrieval._google_news(q, max_results=max_results)
    kept = []
    for r in rows:
        pd = _parse_pubdate(r.get("date", ""))
        if pd is None or pd > checkpoint:
            continue
        r = dict(r)
        r["_pub"] = pd
        kept.append(r)
    # dedup by (source,title)
    seen, uniq = set(), []
    for r in sorted(kept, key=lambda x: x["_pub"]):
        k = (r.get("source", ""), r.get("title", "")[:80])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def ledger_text(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        d = r["_pub"].strftime("%Y-%m-%d")
        lines.append(f"[{d}] SOURCE: {r.get('source','?')} — {r.get('title','')}\n  {r.get('snippet','')}")
    return "\n".join(lines)


def _distinct_sources(rows: list[dict]) -> int:
    return len({(r.get("source") or "").strip().lower() for r in rows if r.get("source")})


def ensemble_forecast(question: str, notes: dict, as_of: str, personas: list[str]) -> dict:
    """Run the price-blind super-team (one pass per persona). Returns mean prob, spread,
    spread-derived confidence, and per-persona probs. Failed passes are skipped."""
    probs, per = [], {}
    for p in personas:
        try:
            out = local_llm.forecast(question, notes, as_of=as_of, persona=p, temperature=0.0)
            probs.append(out["my_probability"])
            per[p] = round(out["my_probability"], 4)
        except local_llm.LocalLLMError as e:
            per[p] = f"ERR:{str(e)[:40]}"
    if not probs:
        return {"prob": None, "spread": None, "confidence": None, "per_persona": per, "n": 0}
    mean = sum(probs) / len(probs)
    spread = max(probs) - min(probs)
    conf = "high" if spread < 0.10 else ("medium" if spread < 0.25 else "low")
    return {"prob": round(mean, 4), "spread": round(spread, 4), "confidence": conf,
            "per_persona": per, "n": len(probs)}


def run_event(ev: dict, checkpoint_days: list[int], personas: list[str]) -> dict:
    close = _parse_iso(ev["close_time"])
    q = ev["question"]
    result = {"ticker": ev["ticker"], "question": q, "outcome": ev["outcome"],
              "implied_yes": ev["implied_yes"], "close_time": ev["close_time"],
              "checkpoints": []}

    for days in sorted(checkpoint_days, reverse=True):
        T = close - timedelta(days=days)
        rows = capture_sources_asof(ev["query"], T)
        nsrc = _distinct_sources(rows)
        cp = {"label": f"T-{days}d", "as_of": T.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "n_sources_le_T": nsrc, "n_items": len(rows)}
        if rows:
            raw = ledger_text(rows)
            try:
                notes = local_llm.extract_evidence(q, raw, as_of=cp["as_of"])
            except local_llm.LocalLLMError:
                notes = {"question": q, "as_of": cp["as_of"], "facts": []}
        else:
            notes = {"question": q, "as_of": cp["as_of"], "facts": []}
        fc = ensemble_forecast(q, notes, cp["as_of"], personas)
        cp.update(fc)
        if fc["prob"] is not None:
            cp["brier"] = round(scoring.brier(fc["prob"], ev["outcome"]), 4)
        result["checkpoints"].append(cp)
        print(f"  {ev['ticker']:28} {cp['label']:6} src={nsrc:2} "
              f"prob={fc['prob']} spread={fc['spread']} conf={fc['confidence']} "
              f"brier={cp.get('brier')}")

    # PARAMETRIC-LEAKAGE PROBE: ensemble with NO evidence, as of resolution.
    empty = {"question": q, "as_of": ev["close_time"], "facts": []}
    probe = ensemble_forecast(q, empty, ev["close_time"], personas)
    probe["brier"] = (round(scoring.brier(probe["prob"], ev["outcome"]), 4)
                      if probe["prob"] is not None else None)
    # Heuristic flag: memory-only already confident & correct-side -> weights likely leak.
    leak = (probe["prob"] is not None
            and probe["confidence"] == "high"
            and abs(probe["prob"] - ev["outcome"]) < 0.25)
    probe["leak_flag"] = bool(leak)
    result["memory_only_probe"] = probe
    result["brier_market"] = round(scoring.brier(ev["implied_yes"], ev["outcome"]), 4)
    print(f"  {ev['ticker']:28} MEM-ONLY prob={probe['prob']} conf={probe['confidence']} "
          f"brier={probe['brier']} LEAK={probe['leak_flag']}  | market_brier={result['brier_market']}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoints", default="14,7,3,1")
    ap.add_argument("--personas", default=",".join(PERSONAS))
    args = ap.parse_args()
    cps = [int(x) for x in args.checkpoints.split(",") if x.strip()]
    personas = [x.strip() for x in args.personas.split(",") if x.strip()]

    if not (config.local_llm_enabled() and local_llm.ping()):
        print("local_llm DOWN — cannot run as-of forecasts", file=sys.stderr)
        return 2

    print(f"PIT backtest: {len(PILOT_EVENTS)} events, checkpoints={cps}, personas={personas}\n")
    results = [run_event(ev, cps, personas) for ev in PILOT_EVENTS]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "pilot_results.json"
    out.write_text(json.dumps(results, indent=2))

    # ---- summary ----
    print("\n=== SUMMARY ===")
    all_pairs, mkt_pairs = [], []
    leak_events = 0
    for r in results:
        final = next((c for c in reversed(r["checkpoints"]) if c.get("prob") is not None), None)
        fb = final["brier"] if final else None
        print(f"{r['ticker']:28} out={r['outcome']} final_prob={final['prob'] if final else None} "
              f"final_brier={fb} market_brier={r['brier_market']} "
              f"mem_leak={r['memory_only_probe']['leak_flag']}")
        for c in r["checkpoints"]:
            if c.get("brier") is not None:
                all_pairs.append(c["brier"])
        mkt_pairs.append(r["brier_market"])
        leak_events += int(r["memory_only_probe"]["leak_flag"])
    if all_pairs:
        print(f"\nmean ensemble Brier over all checkpoints: {sum(all_pairs)/len(all_pairs):.4f} (n={len(all_pairs)})")
    print(f"mean market Brier (event-level): {sum(mkt_pairs)/len(mkt_pairs):.4f}")
    print(f"parametric-leakage flagged events: {leak_events}/{len(results)}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
