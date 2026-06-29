#!/usr/bin/env python3
"""ab_forecast.py — standard dual-model forecasting path (ROUTINE Step 4-5, 2026-06-29).

While the shadow A/B is active (Mistral arm under-resolved — see ab_score.shadow_active),
every due market is forecast by BOTH Qwen3-14B and Mistral-Small-24B on the SAME evidence
(retrieval runs once). The ASSIGNED arm's forecast is recorded officially (with the
adversarial gate + entry-lock); BOTH blind forecasts are persisted to data/ab_shadow.jsonl
so ab_score.py can compute a real Brier head-to-head once these markets resolve. Batched by
model (all Qwen first, one VRAM swap to Mistral) to avoid 14GB<->9GB thrash.

Usage: python3 scripts/ab_forecast.py --limit N [--as-of YYYY-MM-DD]
Opru-plumbing: this script orchestrates the local models + recording; Opus does not browse
or forecast. Idempotent per (ticker, day) via record_forecast.
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import retrieval, local_llm, strategies, scoring, store, schemas, config, learning, error_memory, taxonomy

try:
    from scripts.ab_score import shadow_active
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ab_score import shadow_active


def _due(limit):
    out = subprocess.run(["python3", "scripts/due_for_reforecast.py", "--limit", str(limit)],
                         capture_output=True, text=True).stdout
    return json.loads(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--as-of", default=schemas.utc_now_iso()[:10])
    args = ap.parse_args()
    AS_OF = args.as_of
    dual = shadow_active()
    print(f"shadow_A/B {'ACTIVE (dual-model)' if dual else 'COMPLETE (Qwen-only; Mistral arm resolved enough)'}")
    due = _due(args.limit)
    resolved = store.load_resolutions().resolved
    lessons = store.load_lessons().lessons
    by_strat = scoring.compute_calibration(resolved).by_strategy
    lp = learning.LearningPolicy()          # learned recalibration + segment trust + shrink-to-market
    cache = {}

    # Phase AB: shared Qwen retrieval + Qwen forecast (model resident). Before forecasting each
    # market we recall the forecaster's most-similar PAST MISSES and inject them in-context
    # (lib.error_memory) so it stops repeating avoidable errors — the same block is reused for
    # the Mistral pass below so both models learn from the identical track record.
    for i, m in enumerate(due):
        tk, q = m["ticker"], m["title"]
        try:
            notes = retrieval.gather_evidence(q, as_of=AS_OF, min_sources=5, max_steps=6)
            em = error_memory.recall_block(q, category=m.get("category", ""), ticker=tk,
                                           k=3, resolutions=resolved, lessons=lessons)
            rq = local_llm.forecast_ensemble(q, notes, n=5, as_of=AS_OF,
                                             n_sources=notes.get("n_sources"), error_memory=em)
            cache[tk] = {"m": m, "notes": notes, "qwen": rq, "em": em,
                         "arm": strategies.select_strategy(tk, by_strat).id}
            print(f"[Q {i+1}/{len(due)}] {tk} src={notes.get('n_sources')} qwen_p={rq['my_probability']} conf={rq['my_confidence']} mem={'y' if em else '-'}", flush=True)
        except Exception as e:
            print(f"[Q {i+1}/{len(due)}] {tk} ERR {e}", flush=True)

    # Phase C: Mistral on the SAME notes (one swap) — only while shadow active
    if dual:
        for i, tk in enumerate(list(cache)):
            c = cache[tk]
            try:
                rm = local_llm.forecast_ensemble(c["m"]["title"], c["notes"], n=5, as_of=AS_OF,
                                                 n_sources=c["notes"].get("n_sources"),
                                                 model=config.LOCAL_LLM_MODEL_MISTRAL,
                                                 error_memory=c.get("em", ""))
                c["mistral"] = rm
                print(f"[M {i+1}/{len(cache)}] {tk} mistral_p={rm['my_probability']} conf={rm['my_confidence']} n={rm['n']}/5", flush=True)
            except Exception as e:
                print(f"[M {i+1}/{len(cache)}] {tk} ERR {e}", flush=True)

    # Phase D: reveal price, record assigned arm, persist shadow row
    ts = schemas.utc_now_iso()
    recorded = 0
    with open(config.AB_SHADOW_PATH, "a") as shadowf:
        for tk, c in cache.items():
            arm = strategies.get(c["arm"]) or strategies.get(strategies.DEFAULT_STRATEGY)
            official = c.get("mistral") if arm.id == "LQM5-mistral24" else c["qwen"]
            try:
                px = json.loads(subprocess.run(["python3", "scripts/refresh_market.py", "--ticker", tk],
                                capture_output=True, text=True, timeout=60).stdout.strip().splitlines()[-1])
            except Exception as e:
                print(f"{tk}: price fetch failed ({e})"); continue
            mi = px.get("market_implied_probability")
            # Arm aggregation first (the arm's own topology: ensemble agg + any crowd-adjust).
            prob_arm = strategies.combine(official["probs"], arm, market_price=mi) if official.get("probs") else official["my_probability"]
            # Then the LEARNING POLICY on top: recalibrate this model's prob and shrink toward the
            # market by our MEASURED skill in this segment. During the shadow A/B we feed a SINGLE
            # model (the assigned arm's) so the blind Qwen-vs-Mistral head-to-head stays uncontaminated;
            # the cross-model ensemble weight only engages once we pass >1 family. No measured skill =>
            # alpha 0 => we track the price => no edge => the betting gate takes no position (correctly).
            fam = learning._family(arm.id)
            subcat = taxonomy.classify_subcategory(tk, c["m"]["title"], c["m"].get("category", ""))
            segment = f"{c['m'].get('category','')} / {subcat}" if subcat else (c["m"].get("category", "") or "?")
            blended = lp.blend({fam: prob_arm}, segment, mi)
            prob = blended["final"] if blended.get("final") is not None else prob_arm
            print(f"   {tk} arm={arm.id} prob_arm={prob_arm:.4f} -> final={prob:.4f} (seg='{segment}' alpha={blended.get('alpha')})", flush=True)
            refs = ",".join((c["notes"].get("sources_consulted") or [])[:8])
            cmd = ["python3", "scripts/record_forecast.py", "--ticker", tk, "--prob", f"{prob:.4f}",
                   "--confidence", official["my_confidence"], "--trigger", "scheduled",
                   "--strategy-id", arm.id, "--rationale", (official.get("rationale_summary") or "")[:400],
                   "--drivers", ",".join(official.get("key_drivers") or [])[:300],
                   "--reference-classes", ",".join(official.get("reference_classes") or [])[:300],
                   "--refs", refs, "--title", c["m"]["title"], "--category", c["m"]["category"]]
            if c["m"].get("close_time"): cmd += ["--close-time", c["m"]["close_time"]]
            if mi is not None: cmd += ["--market-implied", f"{mi:.4f}"]
            if px.get("market_price_cents") is not None: cmd += ["--market-price-cents", str(px["market_price_cents"])]
            for k, flag in [("yes_ask","--yes-ask"),("no_ask","--no-ask"),("yes_bid","--yes-bid"),("no_bid","--no-bid")]:
                if px.get(k) is not None: cmd += [flag, f"{px[k]:.4f}"]
            subprocess.run(cmd, capture_output=True, text=True)
            recorded += 1
            qf, mf = c.get("qwen"), c.get("mistral")
            shadowf.write(json.dumps({"ticker": tk, "run_ts": ts, "category": c["m"]["category"],
                "arm": arm.id, "market_implied": mi,
                # learning-policy audit: what the arm said vs what we recorded after recalibrate+shrink
                "segment": segment, "prob_arm": round(prob_arm, 4), "prob_final": round(prob, 4),
                "policy_alpha": blended.get("alpha"), "policy_calibrated": blended.get("calibrated"),
                "error_memory_used": bool(c.get("em")),
                "qwen_p": qf and qf["my_probability"], "qwen_conf": qf and qf["my_confidence"], "qwen_stdev": qf and qf["stdev"],
                "mistral_p": mf and mf["my_probability"], "mistral_conf": mf and mf["my_confidence"], "mistral_stdev": mf and mf["stdev"]}) + "\n")
    print(f"recorded {recorded} forecasts; shadow rows appended -> {config.AB_SHADOW_PATH.name}")


if __name__ == "__main__":
    raise SystemExit(main())
