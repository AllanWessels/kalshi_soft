"""learning.py — the LEARNING POLICY: make the FORECASTS better from resolved outcomes.

This is the lever `learn_policy.py` is NOT. That module is the *betting* policy — it tunes WHEN
to act on a forecast (EV/gap/confidence gates); it cannot rescue a forecaster that is worse than
the price. This module closes the outcome -> forecast loop so the forecaster gets *better* over
time. Three mechanisms, every one heavily regularized (shrunk toward a safe prior at low n) so it
earns autonomy only as the resolved record grows — the same anti-overfit discipline we apply to
forecasting itself:

  1. RECALIBRATION (per model). A single logit-temperature `k` per forecaster, fit to minimize
     Brier on that model's resolved (prob, outcome) pairs. k<1 shrinks toward 0.5 (corrects
     overconfidence), k>1 sharpens. Shrunk toward k=1 (identity) by n/(n+K_CAL).

  2. SEGMENT x MODEL TRUST. Per (segment, model) realized skill-vs-market -> ensemble weights,
     shrunk toward equal weight at low n. We learn WHICH model to trust WHERE.

  3. SHRINK-TO-MARKET (the profitability tie-in). Blend the recalibrated ensemble toward the
     market prior by a factor that GROWS with our measured skill in that segment. No measured
     skill => we ARE the market (=> zero edge => the betting gate takes no position, correctly).
     Earned skill => we deviate proportionally and a real edge appears. Position-taking thus
     EMERGES from learning instead of being asserted by a gate. This is what converts "we lose
     to the market" into "we deviate only where we've proven we beat it."

Recomputed at reconcile time, persisted to data/learning_policy.json (auditable + reversible),
and applied at forecast time via LearningPolicy.blend().
"""
from __future__ import annotations

import json
import math
from typing import Optional

from . import config, store, scoring

K_CAL = 25        # recalibration shrink: k pulled toward identity until a model has >> 25 resolved
K_WEIGHT = 8      # per-(segment,model) trust shrink toward equal weight
K_ALPHA = 10      # shrink-to-market: skill must be earned over ~K_ALPHA samples to deviate fully
SKILL_FULL = 0.05 # segment skill (Brier units) at which we fully trust the model over the market
MIN_SEG_N = 3     # never deviate from the market in a segment with fewer than this many resolved
                  # markets — one lucky resolution must not buy a 45% deviation (anti-overfit)
_EPS = 1e-6

LEARNING_PATH = config.DATA_DIR / "learning_policy.json"


def _logit(p: float) -> float:
    p = min(1 - _EPS, max(_EPS, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x < -30: return _EPS
    if x > 30: return 1 - _EPS
    return 1 / (1 + math.exp(-x))


def _family(strategy_id: str) -> str:
    s = (strategy_id or "").lower()
    if "mistral" in s or s == "lqm5-mistral24":
        return "mistral"
    if s.startswith("s"):
        return "opus"
    if s.startswith("l"):
        return "qwen"
    return "other"


def _pairs(resolutions):
    """(family, segment, prob, outcome, brier_mine, brier_market) for resolved markets."""
    out = []
    for r in resolutions:
        if r.outcome is None or r.final_my_probability is None:
            continue
        seg = f"{r.category} / {r.subcategory}" if r.subcategory else (r.category or "?")
        out.append((_family(r.strategy_id), seg, float(r.final_my_probability), int(r.outcome),
                    r.brier_mine, r.brier_market))
    return out


def _fit_k(pairs_pm) -> tuple[float, int]:
    """1-D search for the logit-temperature minimizing Brier; shrunk toward 1 at low n."""
    pm = [(p, y) for (_, _, p, y, _, _) in pairs_pm]
    n = len(pm)
    if n < 3:
        return 1.0, n
    best_k, best_b = 1.0, 1e9
    k = 0.2
    while k <= 3.0 + 1e-9:
        b = sum((_sigmoid(k * _logit(p)) - y) ** 2 for p, y in pm) / n
        if b < best_b:
            best_b, best_k = b, k
        k += 0.05
    # shrink the fitted temperature toward identity (k=1) until we have lots of data
    w = n / (n + K_CAL)
    return round(1.0 + (best_k - 1.0) * w, 4), n


def _segment_skill(pairs) -> dict:
    """Overall (model-agnostic) skill-vs-market per segment, shrunk toward 0 at low n."""
    seg: dict[str, list] = {}
    for (_, s, _, _, bm, bk) in pairs:
        if bm is None or bk is None:
            continue
        seg.setdefault(s, []).append(bk - bm)  # >0 == we beat the market
    out = {}
    for s, xs in seg.items():
        n = len(xs)
        raw = sum(xs) / n
        out[s] = {"n": n, "skill": round(raw * n / (n + K_ALPHA), 5)}  # shrink toward 0
    return out


def _model_weights(pairs) -> dict:
    """Global per-model skill -> ensemble weight (shrunk toward equal). Models with no record
    get the neutral weight so a new arm (Mistral/Opus) starts equal until it earns/loses trust."""
    fam: dict[str, list] = {}
    for (f, _, _, _, bm, bk) in pairs:
        if bm is None or bk is None or f == "other":
            continue
        fam.setdefault(f, []).append(bk - bm)
    weights = {}
    for f, xs in fam.items():
        n = len(xs)
        skill = (sum(xs) / n) * n / (n + K_WEIGHT)        # shrunk skill
        weights[f] = max(0.05, math.exp(skill / 0.03))    # softmax-ish, floor so none vanish
    return weights


def compute_policy(resolutions=None) -> dict:
    """Recompute the learning policy from the resolved record (call at reconcile time)."""
    if resolutions is None:
        resolutions = store.load_resolutions().resolved
    pairs = _pairs(resolutions)
    families = {f for (f, *_rest) in pairs if f != "other"} | {"qwen", "mistral", "opus"}
    recal = {}
    for f in families:
        recal[f] = {"k": _fit_k([p for p in pairs if p[0] == f])[0],
                    "n": sum(1 for p in pairs if p[0] == f)}
    policy = {
        "n_resolved": len(pairs),
        "recalibration": recal,                 # per-model logit temperature
        "segment_skill": _segment_skill(pairs),  # for shrink-to-market alpha
        "model_weights": _model_weights(pairs),  # ensemble trust
        "params": {"K_CAL": K_CAL, "K_WEIGHT": K_WEIGHT, "K_ALPHA": K_ALPHA, "SKILL_FULL": SKILL_FULL},
        "updated_at": __import__("lib.schemas", fromlist=["utc_now_iso"]).utc_now_iso(),
    }
    return policy


def save(policy: dict) -> None:
    store.write_json_atomic(LEARNING_PATH, policy)


def load() -> dict:
    data = store.read_json(LEARNING_PATH)
    return data if data else compute_policy()


class LearningPolicy:
    """Apply the learned policy at forecast time."""

    def __init__(self, policy: Optional[dict] = None):
        self.p = policy or load()

    def recalibrate(self, model: str, prob: float) -> float:
        k = (self.p.get("recalibration", {}).get(model) or {}).get("k", 1.0)
        return _sigmoid(k * _logit(prob))

    def market_alpha(self, segment: str) -> float:
        """Deviation fraction in [0,1]: 0 => track market (no edge), 1 => trust the ensemble.
        Grows with measured, shrunk skill in the segment."""
        seg = self.p.get("segment_skill", {}).get(segment) or {}
        if seg.get("n", 0) < MIN_SEG_N:   # too few resolved to trust any deviation
            return 0.0
        return max(0.0, min(1.0, seg.get("skill", 0.0) / SKILL_FULL))

    def blend(self, model_probs: dict, segment: str, market_implied: Optional[float]) -> dict:
        """Combine the 3 models' (recalibrated, trust-weighted) forecasts, then shrink toward the
        market by (1 - measured skill). Returns the final probability + the components for audit."""
        weights = self.p.get("model_weights", {})
        cal = {m: self.recalibrate(m, p) for m, p in model_probs.items() if p is not None}
        if not cal:
            return {"final": market_implied, "ensemble": None, "alpha": 0.0, "calibrated": {}}
        wsum = sum(weights.get(m, 1.0) for m in cal) or 1.0
        ensemble = sum(weights.get(m, 1.0) * p for m, p in cal.items()) / wsum
        if market_implied is None:
            return {"final": round(ensemble, 4), "ensemble": round(ensemble, 4), "alpha": 1.0, "calibrated": cal}
        alpha = self.market_alpha(segment)
        final = (1 - alpha) * market_implied + alpha * ensemble
        return {"final": round(final, 4), "ensemble": round(ensemble, 4),
                "alpha": round(alpha, 3), "calibrated": {m: round(v, 4) for m, v in cal.items()}}


# ---------------------------------------------------------------------------
# Inline self-test (synthetic; no live data needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    errs = []
    def chk(name, cond):
        if not cond: errs.append(name)

    # overconfident model (predicts 0.9 but only right half the time) -> k should shrink < 1
    class R:  # minimal resolution stand-in
        def __init__(s, p, y, seg="x / y", sid="LQ5-ensemble5", bm=None, bk=None):
            s.final_my_probability=p; s.outcome=y; s.category="x"; s.subcategory="y"
            s.strategy_id=sid; s.brier_mine=bm; s.brier_market=bk
    over = [R(0.9, 1) for _ in range(10)] + [R(0.9, 0) for _ in range(10)]
    pol = compute_policy(over)
    chk("recal_shrinks_overconfident", pol["recalibration"]["qwen"]["k"] < 1.0)

    lp = LearningPolicy(pol)
    chk("recal_pulls_toward_half", lp.recalibrate("qwen", 0.9) < 0.9)

    # no measured skill => alpha 0 => blend tracks the market exactly (=> no edge => no bet)
    lp2 = LearningPolicy({"recalibration": {}, "segment_skill": {}, "model_weights": {}})
    out = lp2.blend({"qwen": 0.8, "mistral": 0.7, "opus": 0.75}, "x / y", market_implied=0.5)
    chk("no_skill_tracks_market", abs(out["final"] - 0.5) < 1e-9 and out["alpha"] == 0.0)

    # earned skill => alpha 1 => blend trusts the ensemble
    lp3 = LearningPolicy({"recalibration": {}, "model_weights": {},
                          "segment_skill": {"x / y": {"n": 30, "skill": 0.10}}})
    out3 = lp3.blend({"qwen": 0.8}, "x / y", market_implied=0.5)
    chk("earned_skill_deviates", out3["alpha"] == 1.0 and out3["final"] > 0.5)

    if errs:
        print("LEARNING TEST FAILURES:", ", ".join(errs)); raise SystemExit(1)
    print("learning OK")
