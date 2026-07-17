"""recledger.py — single source of truth for the structural-edge trade-recommendation ledger.

Workstream A (PLAN_FOR_OPUS.md): the rec ledger is the live, forward verification record of the
structural/atlas edge. This module owns everything the ledger means:

  * fill evidence   — orderbook snapshot at rec time (best bids, the ask our side must cross,
                      depth at that ask, and whether the entry limit was marketable RIGHT THEN);
  * dual scoring    — `pnl_optimistic` (assume the limit filled — the old, upper-bound number)
                      vs `pnl_conservative` (fill ONLY if evidenced marketable, at the real ask;
                      otherwise NO-FILL, excluded from deployed capital). Conservative is the
                      OFFICIAL number everywhere.
  * cohorts         — rows carrying fill evidence are `verified`; rows logged before evidence
                      existed are `legacy` (fills_unverified) and report only as provisional.
  * kill switches   — per-cell (n>=cell_kill_min_n verified resolved, conservative ROI<0) and a
                      global trailing-ROI halt. Precommitted in data/policy.json
                      (`structural_verification`), enforced by recommend_trades.py.
  * verification bar— the precommitted criteria that must ALL pass before live trading is even
                      discussed with the user (PLAN_FOR_OPUS.md §A4).
  * tail stress     — Monte Carlo of future-basket ROI using each rec's calibrated probability
                      as truth: P(ROI<0), drawdown, break-even win rate. The 13/13-illusion killer.

Orderbook shape (Kalshi GET /markets/{t}/orderbook): {"orderbook_fp": {"yes_dollars": [[price,
qty],...], "no_dollars": [...]}} — resting BIDS per side, price/qty as decimal strings. The ask
for NO = 1 - best YES bid (crossing fills against the YES buyer), and vice versa; depth at that
ask is the opposing best bid's displayed quantity.
"""
from __future__ import annotations

import json
import math
import random
from typing import Any, Optional

from . import config, schemas

LEDGER_PATH = config.DATA_DIR / "trade_recommendations.jsonl"
POLICY_PATH = config.DATA_DIR / "policy.json"

# Fallbacks if data/policy.json lacks the structural_verification block (it is written by
# Workstream A4; these mirror it so code never crashes on an older policy file).
DEFAULT_VERIFICATION = {
    "verified_min_n": 40,
    "verified_min_roi": 0.08,
    "roi_ci_level": 0.90,
    "min_positive_cells": 3,
    "stress_max_p_loss": 0.10,
    "cell_kill_min_n": 15,
    "global_halt_trailing_n": 30,
    "global_halt_roi": -0.10,
    "min_fill_depth_units": 1.0,
}


# ---------------------------------------------------------------------------
# Ledger IO
# ---------------------------------------------------------------------------

def load_rows() -> list[dict]:
    rows: list[dict] = []
    if LEDGER_PATH.exists():
        for line in LEDGER_PATH.open():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def write_rows(rows: list[dict]) -> None:
    """Atomic rewrite of the whole ledger."""
    tmp = LEDGER_PATH.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(LEDGER_PATH)


def load_verification_policy() -> dict:
    try:
        pol = json.loads(POLICY_PATH.read_text())
    except (OSError, ValueError):
        pol = {}
    merged = dict(DEFAULT_VERIFICATION)
    merged.update(pol.get("structural_verification") or {})
    return merged


def fee(price: float) -> float:
    return math.ceil(config.KALSHI_FEE_RATE * price * (1 - price) * 100) / 100.0


# ---------------------------------------------------------------------------
# Fill evidence (orderbook snapshot at rec time)
# ---------------------------------------------------------------------------

def _best_bid(levels: Optional[list]) -> tuple[Optional[float], float]:
    """(best_price, qty_at_best) from a [[price, qty], ...] bid ladder; (None, 0) if empty."""
    best_p, best_q = None, 0.0
    for lvl in levels or []:
        try:
            p, q = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if best_p is None or p > best_p:
            best_p, best_q = p, q
    return best_p, best_q


def snapshot_fill_evidence(client, ticker: str, side: str, entry_limit: float) -> Optional[dict]:
    """Fetch the live orderbook and return the fill-evidence dict for one rec, or None on error.

    side: "YES" or "NO" — the side the rec buys. The ask we must cross is 1 - best opposing bid;
    depth at that ask is the opposing bid's displayed size (units as reported by Kalshi).
    """
    try:
        ob = (client.get_orderbook(ticker) or {}).get("orderbook_fp") or {}
    except Exception:
        return None
    yes_bid, yes_qty = _best_bid(ob.get("yes_dollars"))
    no_bid, no_qty = _best_bid(ob.get("no_dollars"))
    if side == "NO":
        ask = round(1 - yes_bid, 4) if yes_bid is not None else None
        depth = yes_qty
    else:
        ask = round(1 - no_bid, 4) if no_bid is not None else None
        depth = no_qty
    return {
        "ts": schemas.utc_now_iso(),
        "yes_bid": yes_bid, "yes_bid_qty": yes_qty,
        "no_bid": no_bid, "no_bid_qty": no_qty,
        "ask": ask, "ask_depth": depth,
        # marketable right now: our limit reaches the resting opposing offer
        "fillable_now": bool(ask is not None and entry_limit >= ask - 1e-9),
    }


# ---------------------------------------------------------------------------
# Scoring (dual-column) & cohorts
# ---------------------------------------------------------------------------

def cohort_of(rec: dict) -> str:
    return "verified" if isinstance(rec.get("fill_evidence"), dict) else "legacy"


def score_resolved(rec: dict, result: str, min_depth: float = 1.0) -> dict:
    """Score one resolved rec (result in {'yes','no'}) for a 1-unit position, BOTH columns.

    Optimistic: the historical behavior — assume the limit order filled at entry_limit.
    Conservative (OFFICIAL): fill only if the rec-time snapshot shows the limit was marketable
    (fillable_now) with depth >= min_depth; the fill price is the REAL ask at that moment (a
    marketable limit executes at the resting offer, not at your limit). No evidence, not
    marketable, or too thin => NO-FILL: excluded from deployed capital and P&L.
    """
    outcome_yes = 1 if result == "yes" else 0
    payoff = (1 - outcome_yes) if rec["side"] == "NO" else outcome_yes

    entry = float(rec["entry_limit"])
    cost_opt = round(entry + fee(entry), 4)
    pnl_opt = round(payoff - cost_opt, 4)

    ev = rec.get("fill_evidence") if isinstance(rec.get("fill_evidence"), dict) else None
    filled_cons = bool(ev and ev.get("fillable_now") and ev.get("ask") is not None
                       and float(ev.get("ask_depth") or 0) >= min_depth)
    if filled_cons:
        ask = float(ev["ask"])
        cost_cons = round(ask + fee(ask), 4)
        pnl_cons = round(payoff - cost_cons, 4)
    else:
        cost_cons, pnl_cons = 0.0, 0.0

    bc = (float(rec["calibrated_yes"]) - outcome_yes) ** 2
    bm = (float(rec["market_yes"]) - outcome_yes) ** 2
    return {
        "status": "resolved", "outcome": result, "scored_at": schemas.utc_now_iso(),
        "cohort": cohort_of(rec), "fills_unverified": ev is None,
        # optimistic column (kept under the legacy field names for continuity)
        "realized_pnl": pnl_opt, "cost": cost_opt, "won": pnl_opt > 0,
        "pnl_optimistic": pnl_opt, "cost_optimistic": cost_opt,
        # conservative column — THE official number
        "conservative_filled": filled_cons,
        "pnl_conservative": pnl_cons, "cost_conservative": cost_cons,
        "won_conservative": (pnl_cons > 0) if filled_cons else None,
        "brier_cal": round(bc, 5), "brier_market": round(bm, 5), "beat_market": bc < bm,
    }


def tag_legacy(rows: list[dict]) -> int:
    """Backfill cohort/fills_unverified/pnl_optimistic on rows scored before Workstream A.

    Returns how many rows were modified. Never touches conservative columns (legacy rows have
    no fill evidence — they stay provisional forever; the verification clock starts at A1)."""
    changed = 0
    for r in rows:
        if "cohort" not in r:
            r["cohort"] = cohort_of(r)
            r["fills_unverified"] = r["cohort"] == "legacy"
            changed += 1
        if r.get("status") == "resolved" and "pnl_optimistic" not in r:
            r["pnl_optimistic"] = r.get("realized_pnl")
            r["cost_optimistic"] = r.get("cost")
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# Scoreboards
# ---------------------------------------------------------------------------

def _resolved(rows: list[dict], cohort: Optional[str] = None) -> list[dict]:
    out = [r for r in rows if r.get("status") == "resolved"]
    if cohort:
        out = [r for r in out if (r.get("cohort") or cohort_of(r)) == cohort]
    return out


def scoreboard(rows: list[dict], cohort: str = "verified", basis: str = "conservative") -> dict:
    """Aggregate stats for one cohort×basis. Conservative basis counts only evidenced fills."""
    res = _resolved(rows, cohort)
    if basis == "conservative":
        filled = [r for r in res if r.get("conservative_filled")]
        nofill = len(res) - len(filled)
        pnl = sum(r.get("pnl_conservative") or 0 for r in filled)
        cost = sum(r.get("cost_conservative") or 0 for r in filled)
        wins = sum(1 for r in filled if (r.get("pnl_conservative") or 0) > 0)
        base = filled
    else:
        nofill = 0
        pnl = sum(r.get("pnl_optimistic", r.get("realized_pnl")) or 0 for r in res)
        cost = sum(r.get("cost_optimistic", r.get("cost")) or 0 for r in res)
        wins = sum(1 for r in res if (r.get("pnl_optimistic", r.get("realized_pnl")) or 0) > 0)
        base = res
    n = len(base)
    beat = sum(1 for r in base if r.get("beat_market"))
    bc = sum(r.get("brier_cal") or 0 for r in base) / n if n else None
    bm = sum(r.get("brier_market") or 0 for r in base) / n if n else None
    return {
        "cohort": cohort, "basis": basis, "n_resolved": len(res), "n_scored": n,
        "n_nofill": nofill, "wins": wins,
        "win_rate": (wins / n) if n else None,
        "pnl": round(pnl, 4), "deployed": round(cost, 4),
        "roi": (pnl / cost) if cost else None,
        "beat_market": beat, "beat_market_rate": (beat / n) if n else None,
        "brier_cal": bc, "brier_market": bm,
    }


def per_cell(rows: list[dict], cohort: str = "verified", basis: str = "conservative") -> dict:
    cells: dict[str, list] = {}
    for r in _resolved(rows, cohort):
        cells.setdefault(r.get("cell") or "?", []).append(r)
    out = {}
    for cell, rs in cells.items():
        out[cell] = scoreboard(rs, cohort=cohort, basis=basis)
    return out


def bootstrap_roi_ci(rows: list[dict], level: float = 0.90, trials: int = 2000,
                     seed: int = 7) -> Optional[tuple[float, float]]:
    """Bootstrap CI on conservative ROI over verified filled resolved recs. None if n<5."""
    filled = [r for r in _resolved(rows, "verified") if r.get("conservative_filled")]
    if len(filled) < 5:
        return None
    rng = random.Random(seed)
    rois = []
    for _ in range(trials):
        sample = [filled[rng.randrange(len(filled))] for _ in range(len(filled))]
        cost = sum(r["cost_conservative"] for r in sample)
        if cost <= 0:
            continue
        rois.append(sum(r["pnl_conservative"] for r in sample) / cost)
    if not rois:
        return None
    rois.sort()
    lo_idx = int((1 - level) / 2 * len(rois))
    hi_idx = min(len(rois) - 1, int((1 + level) / 2 * len(rois)))
    return (rois[lo_idx], rois[hi_idx])


# ---------------------------------------------------------------------------
# Kill switches (precommitted; enforced by recommend_trades.py)
# ---------------------------------------------------------------------------

def killed_cells(rows: list[dict], policy: Optional[dict] = None) -> dict:
    """Cells removed from the live screen: n>=cell_kill_min_n verified resolved & ROI<0."""
    pol = policy or load_verification_policy()
    out = {}
    for cell, sb in per_cell(rows, "verified", "conservative").items():
        if sb["n_scored"] >= pol["cell_kill_min_n"] and (sb["roi"] or 0) < 0:
            out[cell] = sb
    return out


def global_halt(rows: list[dict], policy: Optional[dict] = None) -> Optional[dict]:
    """Trailing-N verified conservative ROI below the halt floor => pause recommendations."""
    pol = policy or load_verification_policy()
    filled = sorted(
        (r for r in _resolved(rows, "verified") if r.get("conservative_filled")),
        key=lambda r: r.get("scored_at") or r.get("ts") or "",
    )
    n = pol["global_halt_trailing_n"]
    if len(filled) < n:
        return None
    tail = filled[-n:]
    cost = sum(r["cost_conservative"] for r in tail)
    if cost <= 0:
        return None
    roi = sum(r["pnl_conservative"] for r in tail) / cost
    if roi < pol["global_halt_roi"]:
        return {"trailing_n": n, "trailing_roi": roi, "floor": pol["global_halt_roi"]}
    return None


# ---------------------------------------------------------------------------
# Tail stress (Monte Carlo) — PLAN_FOR_OPUS §A2
# ---------------------------------------------------------------------------

def compute_stress(rows: list[dict], n_future: int = 100, trials: int = 5000,
                   seed: int = 7) -> Optional[dict]:
    """Monte Carlo the basket forward using each rec's calibrated_yes as TRUE hit probability.

    Rec profiles are resampled with replacement from the ledger (all rows with a calibrated
    probability and an entry). Each simulated rec wins with its side's calibrated probability;
    cost uses the conservative fill price when evidenced, else the entry limit. Reports the ROI
    distribution, P(ROI<0), expected max drawdown (per $1 basket unit), and the break-even win
    rate. Deterministic under `seed` so reports are reproducible."""
    profiles = []
    for r in rows:
        try:
            q_yes = float(r["calibrated_yes"])
            ev = r.get("fill_evidence") if isinstance(r.get("fill_evidence"), dict) else None
            if ev and ev.get("fillable_now") and ev.get("ask") is not None:
                price = float(ev["ask"])
            else:
                price = float(r["entry_limit"])
            cost = price + fee(price)
            p_win = (1 - q_yes) if r["side"] == "NO" else q_yes
            profiles.append((max(0.0, min(1.0, p_win)), cost))
        except (KeyError, TypeError, ValueError):
            continue
    if not profiles:
        return None
    rng = random.Random(seed)
    rois, max_dds = [], []
    for _ in range(trials):
        pnl_sum = cost_sum = equity = peak = 0.0
        max_dd = 0.0
        for _ in range(n_future):
            p_win, cost = profiles[rng.randrange(len(profiles))]
            pnl = (1.0 - cost) if rng.random() < p_win else -cost
            pnl_sum += pnl
            cost_sum += cost
            equity += pnl
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        if cost_sum > 0:
            rois.append(pnl_sum / cost_sum)
            max_dds.append(max_dd)
    if not rois:
        return None
    rois_sorted = sorted(rois)

    def _pct(q: float) -> float:
        return rois_sorted[min(len(rois_sorted) - 1, int(q * len(rois_sorted)))]

    mean_cost = sum(c for _, c in profiles) / len(profiles)
    return {
        "n_profiles": len(profiles), "n_future": n_future, "trials": trials, "seed": seed,
        "p_loss": sum(1 for r in rois if r < 0) / len(rois),
        "roi_mean": sum(rois) / len(rois),
        "roi_p5": _pct(0.05), "roi_p50": _pct(0.50), "roi_p95": _pct(0.95),
        "expected_max_drawdown": sum(max_dds) / len(max_dds),
        "breakeven_win_rate": mean_cost,   # win rate w where w*1 - mean_cost = 0
        "mean_cost": mean_cost,
    }


# ---------------------------------------------------------------------------
# The verification bar — PLAN_FOR_OPUS §A4 (ALL must pass to unlock the live conversation)
# ---------------------------------------------------------------------------

def verification_status(rows: list[dict], policy: Optional[dict] = None,
                        stress: Optional[dict] = None) -> dict:
    pol = policy or load_verification_policy()
    sb = scoreboard(rows, "verified", "conservative")
    ci = bootstrap_roi_ci(rows, level=pol["roi_ci_level"])
    cells = per_cell(rows, "verified", "conservative")
    pos_cells = [c for c, s in cells.items() if (s["roi"] or 0) > 0 and s["n_scored"] > 0]
    filled = [r for r in _resolved(rows, "verified") if r.get("conservative_filled")]
    tail_hits = [r for r in filled if (r.get("pnl_conservative") or 0) < 0]
    tail_ok = (bool(tail_hits) and (sb["roi"] or 0) > 0) or \
              (stress is not None and stress["p_loss"] < pol["stress_max_p_loss"])
    # realized hit-rate consistency: observed losing-rate vs calibrated expectation (needs n)
    if filled:
        exp_loss = sum(
            (float(r["calibrated_yes"]) if r["side"] == "NO" else 1 - float(r["calibrated_yes"]))
            for r in filled) / len(filled)
        obs_loss = len(tail_hits) / len(filled)
        # binomial-ish tolerance: 2 sigma
        sigma = math.sqrt(max(exp_loss * (1 - exp_loss), 1e-6) / len(filled))
        hit_consistent = obs_loss <= exp_loss + 2 * sigma
    else:
        exp_loss = obs_loss = None
        hit_consistent = False
    criteria = {
        "n_verified_fills": {
            "target": pol["verified_min_n"], "current": sb["n_scored"],
            "pass": sb["n_scored"] >= pol["verified_min_n"]},
        "conservative_roi": {
            "target": pol["verified_min_roi"], "current": sb["roi"],
            "pass": (sb["roi"] or 0) >= pol["verified_min_roi"]},
        "roi_ci_excludes_zero": {
            "target": f">{0} at {pol['roi_ci_level']:.0%} CI", "current": ci,
            "pass": bool(ci and ci[0] > 0)},
        "positive_cells": {
            "target": pol["min_positive_cells"], "current": len(pos_cells),
            "pass": len(pos_cells) >= pol["min_positive_cells"]},
        "tail_survival": {
            "target": f"tail hit absorbed w/ ROI>0, or stress P(loss)<{pol['stress_max_p_loss']:.0%}",
            "current": {"tail_hits": len(tail_hits),
                        "stress_p_loss": stress["p_loss"] if stress else None},
            "pass": tail_ok},
        "hit_rate_consistent": {
            "target": "observed loss-rate <= calibrated + 2σ",
            "current": {"expected": exp_loss, "observed": obs_loss},
            "pass": hit_consistent},
    }
    return {
        "verified": all(c["pass"] for c in criteria.values()),
        "criteria": criteria,
        "scoreboard": sb,
        "note": "VERIFIED unlocks the live-trading CONVERSATION with the user only — "
                "never live orders themselves (PLAN_FOR_OPUS §A4; user constraint 2026-07-17).",
    }
