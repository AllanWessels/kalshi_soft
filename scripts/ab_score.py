#!/usr/bin/env python3
"""ab_score.py — resolve the Qwen-vs-Mistral shadow A/B (user directive 2026-06-29).

The shadow log (data/ab_shadow.jsonl) holds BOTH models' blind forecasts for every due
market (shared evidence, recorded before the price was seen). This script joins those rows
with resolved outcomes and reports a real, outcome-grounded head-to-head: Brier per model,
win rate, and how each did vs the market. Until a model has SHADOW_AB_TARGET_RESOLUTIONS
scored markets the result is provisional (printed as such). Read-only; safe to run anytime.

`shadow_active()` is the gate the loop checks: True while the Mistral arm still needs data.
"""
from __future__ import annotations
import json
import statistics
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import store, config


def _brier(p, outcome):
    return (p - outcome) ** 2


def _load_shadow():
    rows = []
    if config.AB_SHADOW_PATH.exists():
        for line in config.AB_SHADOW_PATH.read_text().splitlines():
            line = line.strip()
            if line:
                try: rows.append(json.loads(line))
                except json.JSONDecodeError: pass
    # keep the FIRST (entry-lock) shadow forecast per ticker — that's the committed one
    first = {}
    for r in rows:
        first.setdefault(r["ticker"], r)
    return first


def score():
    shadow = _load_shadow()
    outcomes = {r.ticker: r.outcome for r in store.load_resolutions().resolved if r.outcome is not None}
    joined = [(tk, s, outcomes[tk]) for tk, s in shadow.items() if tk in outcomes]
    out = {"shadow_markets": len(shadow), "resolved_scored": len(joined),
           "target": config.SHADOW_AB_TARGET_RESOLUTIONS}
    if not joined:
        out["status"] = "no resolved shadow markets yet"
        return out
    qb = [_brier(s["qwen_p"], o) for _, s, o in joined if s.get("qwen_p") is not None]
    mb = [_brier(s["mistral_p"], o) for _, s, o in joined if s.get("mistral_p") is not None]
    kb = [_brier(s["market_implied"], o) for _, s, o in joined if s.get("market_implied") is not None]
    qwin = sum(1 for _, s, o in joined if s.get("qwen_p") is not None and s.get("mistral_p") is not None
               and _brier(s["qwen_p"], o) < _brier(s["mistral_p"], o))
    mwin = sum(1 for _, s, o in joined if s.get("qwen_p") is not None and s.get("mistral_p") is not None
               and _brier(s["mistral_p"], o) < _brier(s["qwen_p"], o))
    out.update({
        "qwen_brier": round(statistics.mean(qb), 4) if qb else None,
        "mistral_brier": round(statistics.mean(mb), 4) if mb else None,
        "market_brier": round(statistics.mean(kb), 4) if kb else None,
        "qwen_wins": qwin, "mistral_wins": mwin,
        "qwen_skill_vs_market": round(statistics.mean(kb) - statistics.mean(qb), 4) if qb and kb else None,
        "mistral_skill_vs_market": round(statistics.mean(kb) - statistics.mean(mb), 4) if mb and kb else None,
        "provisional": len(joined) < config.SHADOW_AB_TARGET_RESOLUTIONS,
        "leader": ("qwen" if qb and mb and statistics.mean(qb) < statistics.mean(mb)
                   else "mistral" if qb and mb else None),
    })
    return out


def shadow_active() -> bool:
    """True while the loop should keep dual-forecasting (Mistral arm under-resolved)."""
    s = score()
    return s.get("resolved_scored", 0) < config.SHADOW_AB_TARGET_RESOLUTIONS


if __name__ == "__main__":
    print(json.dumps(score(), indent=2))
