#!/usr/bin/env python3
"""learn_policy.py — the autonomous learning pass (observe & propose & self-apply).

Closes the loop the project is built for: read the system's OWN resolved track record,
find which decision criteria actually predicted profit, and update the learnable policy
(data/policy.json) — with guardrails so it cannot overfit a handful of noisy outcomes
into a worse policy. This is what lets the system learn "when do I take a position" from
its own results instead of waiting for a human to spot the pattern.

Modes:
  (default)  observe & propose -> writes data/policy_proposals.json + prints a summary.
  --apply    apply ONLY proposals that clear EVERY guardrail; everything else stays
             human-gated (printed, not applied) and logged to policy.changelog.

Guardrail philosophy (revised after the URAN post-mortem)
---------------------------------------------------------
The original loop demanded ``MIN_N`` resolved samples *in the rarest tail cell* before a
knob could move. For fade losses that cell (a 20pt+ gap) almost never fills — so the loop
sat frozen at INSUFFICIENT_DATA while it could plainly see, across the whole corpus, that
wide fades lose money. It had the lesson and could not act on it; a real-money 80pt fade
(URAN) was then taken. That is the failure this revision fixes.

The new guardrails act on a CONSISTENT, corpus-wide signal with a small, bounded,
reversible step — and treat the two directions asymmetrically:

  * MIN_N is measured against the TOTAL resolved sample informing the knob (the whole
    ladder), not the rarest band.
  * A directional-CONSISTENCY check (a monotone "wider fades lose" ladder) is the real
    anti-overfit guard — we act on a coherent shape, never one noisy cell.
  * MAX_STEP caps the move per cycle: a nudge, never a jump. If the nudge was wrong, the
    next cycle's data reverses it. Every change is logged to policy.changelog.
  * RISK-REDUCING changes (tightening a gate — failure mode is opportunity cost, not lost
    capital) auto-apply. RISK-INCREASING changes (loosening a gate) NEVER auto-apply; they
    stay HUMAN_GATE. The loop may protect capital on its own; a human signs off before it
    takes on more risk.

Run after reconcile (ROUTINE step 6c).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import store, scoring, policy, config, schemas  # noqa: E402

MIN_N = 20          # TOTAL resolved samples informing a knob before a change is auto-eligible
MAX_STEP = 0.05     # max change to any threshold per learning cycle (nudge, not jump)

# Upper edge (in probability gap) of each counterfactual band — used to translate a
# "profit dies past band X" finding into a concrete threshold target.
_BAND_UPPER = {"0-5pt": 0.05, "5-10pt": 0.10, "10-20pt": 0.20, "20pt+": 0.35}
_BAND_ORDER = ["0-5pt", "5-10pt", "10-20pt", "20pt+"]


def _roi(cell):
    return cell.get("cf_roi") if cell else None


def _n(cell):
    return (cell or {}).get("n", 0) if cell else 0


def _row(knob, current, proposed, n_total, guardrail, rationale, evidence):
    return {"knob": knob, "current": current, "proposed": proposed, "n": n_total,
            "guardrail": guardrail, "rationale": rationale, "evidence": evidence}


def _classify(knob, current, proposed, n_total, consistent, risk_reducing,
              rationale, evidence):
    """Apply the revised guardrail ladder and return a proposal row.

    AUTO_OK requires: a real proposal, a directionally consistent corpus signal, enough
    TOTAL samples, a non-zero bounded step, AND that the change reduces risk. Anything
    that loosens a gate is held at HUMAN_GATE no matter how strong the signal.
    """
    if proposed is None or not consistent or n_total < 1:
        return _row(knob, current, proposed, n_total, "INSUFFICIENT_DATA", rationale, evidence)
    if n_total < MIN_N:
        return _row(knob, current, proposed, n_total, "INSUFFICIENT_DATA", rationale, evidence)
    # Bounded nudge toward the proposed value (a step, never the full jump).
    step = max(-MAX_STEP, min(MAX_STEP, proposed - current))
    nudged = round(current + step, 4)
    if nudged == current:
        return _row(knob, current, nudged, n_total, "INSUFFICIENT_DATA",
                    rationale + " (already at/again the target — no move)", evidence)
    if not risk_reducing:
        return _row(knob, current, nudged, n_total, "HUMAN_GATE",
                    rationale + " [risk-INCREASING — human sign-off required]", evidence)
    return _row(knob, current, nudged, n_total, "AUTO_OK", rationale, evidence)


def _gap_ladder(gap: dict):
    """Inspect the counterfactual-by-gap evidence.

    Returns ``(consistent, n_total, last_profitable_upper, catastrophic_20plus)``:
      * consistent — ROI does not INCREASE as the gap widens across populated bands, the
        tightest band (0-5pt) is non-negative, and the widest populated band is negative.
        That monotone "wider fades lose" shape is the anti-overfit check.
      * n_total — total resolved samples across all bands (the corpus informing the gate).
      * last_profitable_upper — upper gap edge of the widest still-profitable band.
      * catastrophic_20plus — the 20pt+ band exists and lost catastrophically (ROI<=-50%).
    """
    populated = [(b, gap.get(b)) for b in _BAND_ORDER if _n(gap.get(b)) > 0]
    n_total = sum(_n(gap.get(b)) for b in _BAND_ORDER)
    if len(populated) < 2:
        return (False, n_total, None, False)
    rois = [(_roi(c) if _roi(c) is not None else 0.0) for _, c in populated]
    monotone = all(rois[i] >= rois[i + 1] - 1e-9 for i in range(len(rois) - 1))
    tight_ok = rois[0] >= 0
    widest_losing = rois[-1] < 0
    consistent = monotone and tight_ok and widest_losing
    profitable_uppers = [_BAND_UPPER[b] for b, c in populated if (_roi(c) or 0) >= 0]
    last_profitable_upper = max(profitable_uppers) if profitable_uppers else 0.05
    top = gap.get("20pt+")
    catastrophic = _n(top) >= 1 and (_roi(top) or 0) <= -0.50
    return (consistent, n_total, last_profitable_upper, catastrophic)


def analyze(resolutions, pol: policy.Policy) -> list[dict]:
    prof = scoring.compute_calibration(resolutions).profitability or {}
    gap = prof.get("cf_by_gap", {})
    conf = prof.get("cf_by_confidence", {})
    proposals: list[dict] = []

    consistent, gap_n, last_profit_upper, catastrophic = _gap_ladder(gap)
    gap_evidence = {b: gap.get(b) for b in _BAND_ORDER}

    # --- KNOB 1: max_market_disagreement (the fade gate, non-high-conf) -------------------
    # Wider fades lose: tighten the gate toward the widest band that still made money.
    gate_target = last_profit_upper if consistent else None
    proposals.append(_classify(
        "max_market_disagreement", pol.max_market_disagreement, gate_target, gap_n,
        consistent=consistent, risk_reducing=(gate_target is not None
                                              and gate_target < pol.max_market_disagreement),
        rationale=("Counterfactual ROI falls monotonically as the fade widens and is "
                   f"profitable only at/under {round(last_profit_upper*100)}pt -> tighten the "
                   "fade gate toward the band that actually pays."
                   if consistent else
                   "No coherent 'wider fades lose' ladder yet (signal not monotone / tail "
                   "band not negative)."),
        evidence=gap_evidence))

    # --- KNOB 2: hard_gap_ceiling (absolute, applies even at HIGH confidence) -------------
    # This is the knob that closes the carve-out that let URAN through: above the ceiling
    # no lean is taken and the adversarial veto is always binding, regardless of confidence.
    # Bring it down toward where fades become CATASTROPHIC (the 20pt+ band) once that band
    # has lost. Distinct from the fade gate: it still leaves room for genuine high-conf
    # near-certainties at moderate gaps; it only hard-blocks egregious divergences.
    ceiling_target = _BAND_UPPER["10-20pt"] if (consistent and catastrophic) else None
    proposals.append(_classify(
        "hard_gap_ceiling", pol.hard_gap_ceiling, ceiling_target, gap_n,
        consistent=(consistent and catastrophic),
        risk_reducing=(ceiling_target is not None and ceiling_target < pol.hard_gap_ceiling),
        rationale=("The widest fades (20pt+) lost catastrophically (ROI<=-50%) -> lower the "
                   "absolute ceiling so even high-confidence cannot take a fade that wide. "
                   "This is the URAN guard learned from the record."
                   if (consistent and catastrophic) else
                   "No catastrophic wide-fade band yet — the absolute ceiling stays put."),
        evidence={"20pt+": gap.get("20pt+"), "10-20pt": gap.get("10-20pt")}))

    # --- KNOB 3: confidence gating (medium too?) — surfaced, not auto-applied -------------
    # The record shows confidence predicts profit (high>medium>low), but there is no
    # continuous knob to nudge here yet — gating medium is a discrete policy change, so it
    # stays a human-surfaced recommendation rather than an autonomous move.
    med, low, high = conf.get("medium", {}), conf.get("low", {}), conf.get("high", {})
    med_loses = (_roi(med) or 0) < 0 and (_roi(high) or 0) > 0
    conf_n = _n(med) + _n(high) + _n(low)
    proposals.append(_row(
        "low_confidence_never_leans", pol.low_confidence_never_leans,
        True if med_loses else None, conf_n,
        "HUMAN_GATE" if (med_loses and conf_n >= MIN_N) else "INSUFFICIENT_DATA",
        rationale=("medium-confidence trades lose while high-confidence profit -> consider a "
                   "min-confidence gate (no continuous knob exists to auto-tune; human-gated)."
                   if med_loses else "Confidence gating evidence inconclusive."),
        evidence={"high": high, "medium": med, "low": low}))

    # --- KNOB 4: adversarial veto authority (learn to trust the gate) --------------------
    verdicts = [r for r in resolutions if getattr(r, "adversarial_verdict", "")]
    proposals.append(_row(
        "adversarial_veto_binding", pol.adversarial_veto_binding, None, len(verdicts),
        "INSUFFICIENT_DATA",
        rationale=("Veto precision becomes learnable once vetoed/revised positions resolve; "
                   f"{len(verdicts)} resolved position(s) carry a verdict so far."),
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
        print(f"  [{tag}] {p['knob']}: {p['current']} -> {p['proposed']} (n={p['n']}) — {p['rationale']}")
        if args.apply and tag == "AUTO_OK":
            policy.record_change(pol, p["knob"], p["current"], p["proposed"],
                                 reason=p["rationale"], evidence=p["evidence"])
            applied += 1
    if args.apply and applied:
        policy.save(pol)
        print(f"applied {applied} guardrail-cleared change(s); policy now v{pol.version}")
    elif args.apply:
        print("no proposal cleared all guardrails — nothing applied.")
    else:
        print("observe mode — proposals written to data/policy_proposals.json; none applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
