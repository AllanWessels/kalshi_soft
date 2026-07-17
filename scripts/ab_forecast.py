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
import argparse
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import retrieval, local_llm, strategies, scoring, store, schemas, config, learning, error_memory, taxonomy, atlas

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
    # C2 (2026-07-17): dual pass requires BOTH the config flag and an unresolved shadow target.
    # Default is OFF — both models trail the market badly and the head-to-head question now
    # lives inside the LD5-diverse arm (each family is a member; the scoreboard attributes).
    dual = config.SHADOW_AB_ENABLED and shadow_active()
    print(f"shadow_A/B {'ACTIVE (dual-model)' if dual else 'OFF (C2: single arm-driven pass; diverse arm carries both families)'}")
    due = _due(args.limit)
    resolved = store.load_resolutions().resolved
    lessons = store.load_lessons().lessons
    by_strat = scoring.compute_calibration(resolved).by_strategy
    lp = learning.LearningPolicy()          # learned recalibration + segment trust + shrink-to-market
    cmap = atlas.CalibrationMap.load() if config.market_calibration_enabled() else atlas.CalibrationMap({})
    if cmap.cells:
        print(f"market-calibration anchor ON ({len(cmap.cells)} history-learned cells)")
    cache = {}

    # Phase AB: shared Qwen retrieval + the ASSIGNED ARM's forecast. The arm decides the
    # topology (C3): homogeneous arms run n passes of their model; LD5-diverse runs the
    # 4-member cross-model persona panel (strategies.ensemble_members), grouped by model to
    # limit VRAM swaps. Before forecasting we recall the forecaster's most-similar PAST MISSES
    # and inject them in-context (lib.error_memory) so it stops repeating avoidable errors.
    for i, m in enumerate(due):
        tk, q = m["ticker"], m["title"]
        try:
            arm = strategies.select_strategy(tk, by_strat)
            notes = retrieval.gather_evidence(q, as_of=AS_OF, min_sources=5, max_steps=6)
            em = error_memory.recall_block(q, category=m.get("category", ""), ticker=tk,
                                           k=3, resolutions=resolved, lessons=lessons)
            members = strategies.ensemble_members(arm)
            rq = local_llm.forecast_ensemble(
                q, notes, n=arm.n_forecasters, as_of=AS_OF,
                n_sources=notes.get("n_sources"), error_memory=em,
                model=strategies.resolve_forecaster_model(arm), members=members)
            cache[tk] = {"m": m, "notes": notes, "qwen": rq, "em": em, "arm": arm.id}
            print(f"[F {i+1}/{len(due)}] {tk} arm={arm.id} src={notes.get('n_sources')} "
                  f"p={rq['my_probability']} conf={rq['my_confidence']} n={rq['n']}/{rq['n_requested']} "
                  f"mem={'y' if em else '-'}", flush=True)
        except Exception as e:
            print(f"[F {i+1}/{len(due)}] {tk} ERR {e}", flush=True)

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
            # Phase AB already ran the ASSIGNED arm's own topology (model/members), so its
            # ensemble is the official forecast for every arm. (Pre-C2 the Mistral arm's
            # official pass came from the separate shadow phase.)
            official = c["qwen"]
            try:
                px = json.loads(subprocess.run(["python3", "scripts/refresh_market.py", "--ticker", tk],
                                capture_output=True, text=True, timeout=60).stdout.strip().splitlines()[-1])
            except Exception as e:
                print(f"{tk}: price fetch failed ({e})"); continue
            mi = px.get("market_implied_probability")
            # History-calibrated market PRIOR: correct the raw price by the per-cell, OOS-validated
            # favorite-longshot map (lib/atlas). Identity where no cell qualifies, so safe. We anchor
            # the learning blend to this corrected price but still RECORD the raw mi as market-implied
            # so EV/leans trade against the true price; the (mi_anchor - mi) gap IS the structural edge.
            oi = float(c["m"].get("open_interest") or px.get("open_interest") or 0.0)
            cat = c["m"].get("category", "") or "?"
            cal = cmap.calibrate(cat, mi, oi) if mi is not None else {"calibrated": mi, "corrected": False}
            mi_anchor = cal["calibrated"] if mi is not None else None
            # Arm aggregation first (the arm's own topology: ensemble agg + any crowd-adjust).
            # LD5-diverse gets its 5th member HERE: the atlas-calibrated price joins the prob
            # list at combine time only — the crowd's (bias-corrected) vote enters the panel
            # AFTER every blind LLM pass, so anti-anchoring is preserved by construction.
            member_probs = list(official.get("probs") or [])
            if arm.id == "LD5-diverse" and member_probs and mi_anchor is not None:
                member_probs.append(float(mi_anchor))
            prob_arm = strategies.combine(member_probs, arm, market_price=mi) if member_probs else official["my_probability"]
            # Then the LEARNING POLICY on top: recalibrate this model's prob and shrink toward the
            # market by our MEASURED skill in this segment. During the shadow A/B we feed a SINGLE
            # model (the assigned arm's) so the blind Qwen-vs-Mistral head-to-head stays uncontaminated;
            # the cross-model ensemble weight only engages once we pass >1 family. No measured skill =>
            # alpha 0 => we track the price => no edge => the betting gate takes no position (correctly).
            fam = learning._family(arm.id)
            subcat = taxonomy.classify_subcategory(tk, c["m"]["title"], c["m"].get("category", ""))
            segment = f"{c['m'].get('category','')} / {subcat}" if subcat else (c["m"].get("category", "") or "?")
            blended = lp.blend({fam: prob_arm}, segment, mi_anchor)
            prob = blended["final"] if blended.get("final") is not None else prob_arm
            cal_tag = f" cal={mi:.3f}->{mi_anchor:.3f}[{cal['key']}]" if cal.get("corrected") else ""
            print(f"   {tk} arm={arm.id} prob_arm={prob_arm:.4f} -> final={prob:.4f} (seg='{segment}' alpha={blended.get('alpha')}){cal_tag}", flush=True)
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
                # history market-calibration audit: raw price, corrected anchor, the cell, did it move
                "mkt_cal_anchor": mi_anchor, "mkt_cal_corrected": cal.get("corrected"), "mkt_cal_cell": cal.get("key"),
                "error_memory_used": bool(c.get("em")),
                "qwen_p": qf and qf["my_probability"], "qwen_conf": qf and qf["my_confidence"], "qwen_stdev": qf and qf["stdev"],
                "mistral_p": mf and mf["my_probability"], "mistral_conf": mf and mf["my_confidence"], "mistral_stdev": mf and mf["stdev"]}) + "\n")
    print(f"recorded {recorded} forecasts; shadow rows appended -> {config.AB_SHADOW_PATH.name}")


if __name__ == "__main__":
    raise SystemExit(main())
