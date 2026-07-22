"""backtest_pit.py — point-in-time (as-of) backtest of the LLM super-team.

Honest backtest of a retrieval forecaster on SETTLED markets, per the chronological
method: reconstruct the information timeline from timestamped sources, forecast at
checkpoints using ONLY sources published <= that checkpoint, and score vs the known
outcome. The market price is NEVER shown to the forecaster; it is compared only after.

Two leakage channels, handled explicitly:
  1. RETRIEVAL leakage ("the web knows"): closed HARD — GNews is queried per checkpoint
     with Google's `before:<date>` operator AND every item is re-filtered on its parsed
     pubDate <= checkpoint. Undated items are DROPPED. Wikipedia/live pages excluded.
  2. PARAMETRIC leakage ("the weights know"): MEASURED, not assumed — a memory-only probe
     (empty evidence) runs per event, and is flagged as leakage ONLY when it is confident
     & correct AND the base rate is genuinely uncertain (a lopsided base rate explains a
     correct memory-only forecast without any memorized outcome).

Super-team construction (the fixes the 3-event pilot demanded):
  FIX 1 (real diversity): 2 model families (Qwen3 + Mistral) x 3 personas x temp>0.
         The pilot's single-model personas collapsed to identical numbers -> no diversity
         -> manufactured confidence. Model-major batching avoids VRAM reload thrash.
  FIX 2 (anti-overreaction): base-rate-anchored shrinkage. final = w*evidence + (1-w)*base,
         w grows with the number of distinct sources, so thin/vivid evidence cannot swing
         the estimate far from the reference-class base rate.
  FIX 3 (honest leak probe): see channel 2 above.

All three fixes live at the AGGREGATION layer — production forecasting code is untouched
until the backtest shows which calibration to port.

Usage: python3 scripts/backtest_pit.py [--events data/backtest_pit/events_40.json]
                                        [--checkpoints 14,7,3,1] [--temp 0.4] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import local_llm, retrieval, scoring, config  # noqa: E402

OUT_DIR = config.DATA_DIR / "backtest_pit"
PERSONAS = ["standard", "outside", "inside"]
MODELS = [config.LOCAL_LLM_MODEL, config.LOCAL_LLM_MODEL_MISTRAL]  # Qwen3, Mistral — 2 families


# --------------------------------------------------------------------------- helpers
def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _parse_pubdate(s: str):
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (TypeError, ValueError):
        return None


def capture_sources_asof(query: str, checkpoint: datetime, max_results: int = 20) -> list[dict]:
    """GNews `before:<checkpoint>` + HARD pubDate<=checkpoint filter. Undated dropped."""
    q = f"{query} before:{checkpoint.strftime('%Y-%m-%d')}"
    kept = []
    for r in retrieval._google_news(q, max_results=max_results):
        pd = _parse_pubdate(r.get("date", ""))
        if pd is None or pd > checkpoint:
            continue
        r = dict(r); r["_pub"] = pd
        kept.append(r)
    seen, uniq = set(), []
    for r in sorted(kept, key=lambda x: x["_pub"]):
        k = (r.get("source", ""), r.get("title", "")[:80])
        if k not in seen:
            seen.add(k); uniq.append(r)
    return uniq


def _distinct_sources(rows: list[dict]) -> int:
    return len({(r.get("source") or "").strip().lower() for r in rows if r.get("source")})


def ledger_text(rows: list[dict]) -> str:
    return "\n".join(
        f"[{r['_pub'].strftime('%Y-%m-%d')}] SOURCE: {r.get('source','?')} — {r.get('title','')}\n"
        f"  {r.get('snippet','')}" for r in rows)


def _fc(model, question, notes, as_of, persona, temp):
    """One forecast pass; returns float prob or None on failure."""
    try:
        return local_llm.forecast(question, notes, as_of=as_of, persona=persona,
                                  temperature=temp, model=model)["my_probability"]
    except local_llm.LocalLLMError:
        return None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# --------------------------------------------------------------------------- phases
def phase_retrieval(events, checkpoints):
    """No LLM. -> ledgers[ticker][days] = rows, meta[ticker][days] = n_sources."""
    ledgers, nsrc = {}, {}
    for ev in events:
        close = _parse_iso(ev["close_time"])
        ledgers[ev["ticker"]], nsrc[ev["ticker"]] = {}, {}
        for days in checkpoints:
            T = close - timedelta(days=days)
            rows = capture_sources_asof(ev["query"] or ev["question"], T)
            ledgers[ev["ticker"]][days] = rows
            nsrc[ev["ticker"]][days] = _distinct_sources(rows)
        print(f"  [retrieval] {ev['ticker']:30} " +
              " ".join(f"T-{d}={nsrc[ev['ticker']][d]}" for d in checkpoints))
    return ledgers, nsrc


def phase_extract(events, checkpoints, ledgers):
    """Condense each ledger into evidence notes ONCE (default model). -> notes[ticker][days]."""
    notes = {}
    for ev in events:
        close = _parse_iso(ev["close_time"]); notes[ev["ticker"]] = {}
        for days in checkpoints:
            rows = ledgers[ev["ticker"]][days]
            as_of = (close - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if rows:
                try:
                    n = local_llm.extract_evidence(ev["question"], ledger_text(rows), as_of=as_of)
                except local_llm.LocalLLMError:
                    n = {"question": ev["question"], "as_of": as_of, "facts": []}
            else:
                n = {"question": ev["question"], "as_of": as_of, "facts": []}
            notes[ev["ticker"]][days] = n
        print(f"  [extract]  {ev['ticker']:30} done")
    return notes


def phase_forecast(events, checkpoints, notes, temp):
    """MODEL-MAJOR forecasting (each model resident for its whole batch).
    -> members[ticker][days] = [probs], base[ticker]=[per-model base rate],
       mem[ticker]=[per-model memory-only]."""
    members = {ev["ticker"]: {d: [] for d in checkpoints} for ev in events}
    base = {ev["ticker"]: [] for ev in events}
    mem = {ev["ticker"]: [] for ev in events}
    for model in MODELS:
        print(f"  [forecast] loading model {model} ...")
        for ev in events:
            close = _parse_iso(ev["close_time"]); q = ev["question"]
            empty = {"question": q, "as_of": ev["close_time"], "facts": []}
            base[ev["ticker"]].append(_fc(model, q, empty, ev["close_time"], "outside", 0.0))
            mem[ev["ticker"]].append(_fc(model, q, empty, ev["close_time"], "standard", 0.0))
            for days in checkpoints:
                as_of = (close - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                n = notes[ev["ticker"]][days]
                for persona in PERSONAS:
                    members[ev["ticker"]][days].append(_fc(model, q, n, as_of, persona, temp))
        print(f"  [forecast] {model} batch complete")
    return members, base, mem


def aggregate(events, checkpoints, members, base, mem, nsrc):
    """No LLM. Apply the three fixes and score."""
    results, all_final, all_raw, all_out, mkt = [], [], [], [], []
    leak_events = 0
    for ev in events:
        t, o = ev["ticker"], ev["outcome"]
        base_rate = _mean(base[t]); mem_only = _mean(mem[t])
        r = {"ticker": t, "question": ev["question"], "outcome": o,
             "implied_yes": ev["implied_yes"], "base_rate": _round(base_rate),
             "memory_only": _round(mem_only), "checkpoints": []}
        for days in checkpoints:
            ms = [m for m in members[t][days] if m is not None]
            ev_mean = _mean(ms)
            if ev_mean is None or base_rate is None:
                r["checkpoints"].append({"label": f"T-{days}d", "n_members": 0}); continue
            spread = (max(ms) - min(ms)) if len(ms) > 1 else 0.0
            n = nsrc[t][days]
            w = min(0.85, max(0.30, n / (n + 5)))                 # FIX 2: shrink to base rate
            final = w * ev_mean + (1 - w) * base_rate
            cp = {"label": f"T-{days}d", "n_members": len(ms), "n_sources": n,
                  "ev_mean": _round(ev_mean), "spread": _round(spread), "w": round(w, 2),
                  "final": _round(final), "brier_final": _round(scoring.brier(final, o)),
                  "brier_raw": _round(scoring.brier(ev_mean, o))}
            r["checkpoints"].append(cp)
            all_final.append((final, o)); all_raw.append((ev_mean, o))
        # FIX 3: leak only if memory-only confident-correct AND base rate genuinely uncertain
        leak = (mem_only is not None and abs(mem_only - o) < 0.25
                and 0.35 < (base_rate or 0.5) < 0.65)
        r["leak_flag"] = bool(leak); leak_events += int(leak)
        # updating skill: earliest vs latest checkpoint brier
        briers = [c["brier_final"] for c in r["checkpoints"] if c.get("brier_final") is not None]
        r["updating_delta"] = _round(briers[0] - briers[-1]) if len(briers) >= 2 else None
        fin = next((c for c in reversed(r["checkpoints"]) if c.get("final") is not None), None)
        r["final_brier"] = fin["brier_final"] if fin else None
        r["brier_market"] = _round(scoring.brier(ev["implied_yes"], o))
        all_out.append(o); mkt.append((ev["implied_yes"], o))
        results.append(r)
    return results, all_final, all_raw, mkt, leak_events


def _round(x, n=4):
    return round(x, n) if isinstance(x, (int, float)) else x


def reliability(pairs, bins=5):
    """Coarse reliability curve: per prob-bin, (mean_pred, empirical_freq, n)."""
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, o) for p, o in pairs if (lo <= p < hi or (b == bins - 1 and p == 1.0))]
        if sel:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(sel),
                        "mean_pred": round(sum(p for p, _ in sel) / len(sel), 3),
                        "emp_freq": round(sum(o for _, o in sel) / len(sel), 3)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(OUT_DIR / "events_40.json"))
    ap.add_argument("--checkpoints", default="14,7,3,1")
    ap.add_argument("--temp", type=float, default=0.4)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    cps = [int(x) for x in args.checkpoints.split(",") if x.strip()]
    events = json.loads(Path(args.events).read_text())
    if args.limit:
        events = events[:args.limit]

    if not (config.local_llm_enabled() and local_llm.ping()):
        print("local_llm DOWN", file=sys.stderr); return 2

    print(f"PIT backtest: {len(events)} events, checkpoints={cps}, models={MODELS}, "
          f"personas={PERSONAS}, temp={args.temp}\n")
    print("PHASE 1 — retrieval (date-bounded, leakage-free)")
    ledgers, nsrc = phase_retrieval(events, cps)
    print("\nPHASE 2 — evidence extraction")
    notes = phase_extract(events, cps, ledgers)
    print("\nPHASE 3 — forecasting (model-major)")
    members, base, mem = phase_forecast(events, cps, notes, args.temp)
    print("\nPHASE 4 — aggregate + score")
    results, all_final, all_raw, mkt, leak_events = aggregate(
        events, cps, members, base, mem, nsrc)

    def mb(pairs):
        return sum(scoring.brier(p, o) for p, o in pairs) / len(pairs) if pairs else None
    # market brier over ONLY the checkpoints we scored (fair denominator: per-event, but
    # report event-level market brier vs per-checkpoint ensemble)
    summary = {
        "n_events": len(results),
        "n_checkpoint_forecasts": len(all_final),
        "brier_final_blended": _round(mb(all_final)),   # with FIX 2
        "brier_raw_ensemble": _round(mb(all_raw)),      # without FIX 2 (diagnostic)
        "brier_market_eventlevel": _round(mb(mkt)),
        "mean_member_spread": _round(_mean(
            [c["spread"] for r in results for c in r["checkpoints"] if c.get("spread") is not None])),
        "leak_flagged_events": leak_events,
        "mean_updating_delta": _round(_mean(
            [r["updating_delta"] for r in results if r["updating_delta"] is not None])),
        "reliability_blended": reliability(all_final),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "scaled_results.json").write_text(json.dumps(
        {"summary": summary, "events": results}, indent=2))

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        if k != "reliability_blended":
            print(f"  {k}: {v}")
    print("  reliability (blended):")
    for b in summary["reliability_blended"]:
        print(f"    {b['bin']}: pred~{b['mean_pred']} emp={b['emp_freq']} (n={b['n']})")
    print(f"\nwrote {OUT_DIR/'scaled_results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
