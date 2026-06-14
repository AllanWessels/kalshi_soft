#!/usr/bin/env python3
"""learn_policy.py — the autonomous learning pass (observe & propose).

Closes the loop the project is built for: read the system's OWN resolved track record,
find which decision criteria actually predicted profit, and PROPOSE updates to the
learnable policy (data/policy.json) — with guardrails so it cannot overfit a handful of
noisy outcomes into a worse policy. This is what lets the system learn "when do I take a
position" from its own results instead of waiting for a human to spot the pattern.

Modes:
  (default)  observe & propose -> writes data/policy_proposals.json + prints a summary.
             NOTHING is applied. This is the safe mode while n is small.
  --apply    apply ONLY proposals that clear EVERY guardrail; everything else stays
             human-gated (printed, not applied) and logged to policy.changelog.

Guardrails (anti-overfit — the whole ballgame at small n):
  * MIN_N resolved samples in the relevant cell before a knob is auto-eligible.
  * MAX_STEP cap per cycle — nudge a threshold, never jump it.
  * Every applied change is appended to policy.changelog (auditable + reversible).

Run after reconcile (ROUTINE step 6c). At today's n it will mostly say
INSUFFICIENT_DATA — by design; it earns authority as the record grows.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import store, scoring, policy, config, schemas  # noqa: E402

MIN_N = 20          # resolved samples in a cell before a knob change is auto-eligible
MAX_STEP = 0.05     # max change to any threshold per learning cycle (nudge, not jump)


def _roi(cell: dict):
    return cell.get("cf_roi") if cell else None


def _proposal(knob, current, proposed, n, rationale, evidence):
    """Classify a proposed change against the guardrails."""
    if proposed is None or n < 1:
        guardrail = "INSUFFICIENT_DATA"
    elif n < MIN_N:
        guardrail = "INSUFFICIENT_DATA"      # directionally real, not yet trustworthy
    elif abs(proposed - current) > MAX_STEP:
        guardrail = "HUMAN_GATE"             # change too large to auto-apply
        proposed = round(current + MAX_STEP * (1 if proposed > current else -1), 4)
    else:
        guardrail = "AUTO_OK"
    return {"knob": knob, "current": current, "proposed": proposed, "n": n,
            "guardrail": guardrail, "rationale": rationale, "evidence": evidence}


def analyze(resolutions, pol: policy.Policy) -> list[dict]:
    prof = scoring.compute_calibration(resolutions).profitability or {}
    gap = prof.get("cf_by_gap", {})
    conf = prof.get("cf_by_confidence", {})
    proposals: list[dict] = []

    # --- KNOB 1: max_market_disagreement (the gap gate) ---------------------------------
    # If wide-gap counterfactual trades lose money, tighten the gate toward the smallest
    # band that is still profitable. The edge-gap finding: big fades lose.
    losing_bands = [b for b in ("10-20pt", "20pt+") if (_roi(gap.get(b)) or 0) < 0]
    band_n = sum((gap.get(b, {}).get("n", 0)) for b in ("10-20pt", "20pt+"))
    proposed_gate = 0.10 if losing_bands else None   # profit survived only <10pt
    proposals.append(_proposal(
        "max_market_disagreement", pol.max_market_disagreement, proposed_gate, band_n,
        rationale=(f"Counterfactual ROI negative in {', '.join(losing_bands)} -> wide fades "
                   f"lose; tighten the fade gate." if losing_bands else
                   "No clear loss in wide-gap bands yet."),
        evidence={b: gap.get(b) for b in ("0-5pt", "5-10pt", "10-20pt", "20pt+")}))

    # --- KNOB 2: gate medium confidence too? --------------------------------------------
    med, low, high = conf.get("medium", {}), conf.get("low", {}), conf.get("high", {})
    med_loses = (_roi(med) or 0) < 0 and (_roi(high) or 0) > 0
    proposals.append(_proposal(
        "low_confidence_never_leans", pol.low_confidence_never_leans,
        True if med_loses else None, (med.get("n", 0) + high.get("n", 0)),
        rationale=("medium-confidence trades lose while high-confidence profit -> consider "
                   "gating medium too (currently only low is gated)." if med_loses else
                   "Confidence gating evidence inconclusive."),
        evidence={"high": high, "medium": med, "low": low}))

    # --- KNOB 3: adversarial veto authority (learn to trust the gate) -------------------
    # Among RESOLVED positions that carried an adversarial verdict, did vetoes/revises
    # actually avoid losses? The gate is new -> expect 0 resolved verdicts for a while.
    verdicts = [r for r in resolutions if getattr(r, "adversarial_verdict", "")]
    proposals.append(_proposal(
        "adversarial_veto_binding", pol.adversarial_veto_binding, None, len(verdicts),
        rationale=("No resolved positions carry an adversarial verdict yet — the gate is "
                   "new. Its veto precision becomes learnable once vetoed/revised positions "
                   "resolve."),
        evidence={"resolved_with_verdict": len(verdicts)}))

    return proposals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="apply only AUTO_OK proposals; everything else stays human-gated")
    args = ap.parse_args()

    pol = policy.load()
    resolutions = store.load_resolutions().resolved
    proposals = analyze(resolutions, pol)

    out = {
        "generated_at": schemas.utc_now_iso(),
        "n_resolved": len(resolutions),
        "guardrails": {"min_n": MIN_N, "max_step": MAX_STEP},
        "mode": "apply" if args.apply else "observe",
        "proposals": proposals,
    }
    store.write_json_atomic(config.DATA_DIR / "policy_proposals.json", out)

    applied = 0
    print(f"learn_policy: {len(resolutions)} resolved | guardrails min_n={MIN_N} max_step={MAX_STEP}")
    for p in proposals:
        tag = p["guardrail"]
        line = f"  [{tag}] {p['knob']}: {p['current']} -> {p['proposed']} (n={p['n']}) — {p['rationale']}"
        print(line)
        if args.apply and tag == "AUTO_OK":
            policy.record_change(pol, p["knob"], p["current"], p["proposed"],
                                 reason=p["rationale"], evidence=p["evidence"])
            applied += 1
    if args.apply and applied:
        policy.save(pol)
        print(f"applied {applied} guardrail-cleared change(s); policy now v{pol.version}")
    elif args.apply:
        print("no proposal cleared all guardrails — nothing applied (correct at small n).")
    else:
        print("observe mode — proposals written to data/policy_proposals.json; none applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
