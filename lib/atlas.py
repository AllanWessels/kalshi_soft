"""atlas.py — shared market-(in)efficiency model learned from settled history.

Two leakage-free artifacts:

  * inefficiency atlas (scripts/inefficiency_atlas.py): where is the crowd miscalibrated?
  * market-calibration map (this module): a per-cell correction implied_price -> calibrated P(yes),
    fit by SHRUNK Platt scaling on thousands of past (implied, outcome) pairs.

Cells come at TWO granularities (Workstream B2, PLAN_FOR_OPUS.md):

  * coarse   — (category | price_band | liq_tier)                  e.g. "culture|5-15c|mid"
  * granular — (category | price_band | fine_liq | duration_band)  e.g. "culture|5-15c|mid2|d0-2"

The granular axes are FINER open-interest tiers inside the tradeable band and the market's
LIFETIME (duration open->close). Duration is what the harvest supports honestly: each history row
carries one near-close price snapshot, so a time-REMAINING axis is not learnable from it — but
duration separates daily mention/chart markets from long-lived election markets, is identical at
train and serve time, and also bounds the train/serve mismatch (short-lived cells' snapshots are
representative of any moment in their life). `calibrate()` prefers a granular cell when it has a
qualified fit, else falls back to coarse, else identity (track the market).

Why Platt (logit a,b) and not just a bias: the longshot inefficiency the atlas finds is a *bias*
(b shifts the crowd's price) AND often an over/under-confidence (a scales it). Both are learned,
then shrunk toward identity (a=1,b=0) by n/(n+K) so a thin cell can't buy a wild correction.

Used out-of-sample only: the map is fit on PAST settled markets and applied to FUTURE live ones —
legitimate calibration (Ch 14), not contamination. Validation is walk-forward by close-month
(scripts/walkforward_validate.py) and ONLY walk-forward-positive cells are tradeable by the live
screen (`tradeable_cell()`); the forecaster anchor may still use any fitted cell.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Optional

from . import config

_EPS = 1e-6
K_CELL = 60          # shrink a cell's Platt fit toward identity until it has >> 60 resolved markets
MIN_CELL_N = 40      # below this, no correction at all (track the raw market)

CALIB_PATH = config.DATA_DIR / "history" / "market_calibration.json"
WALKFORWARD_PATH = config.DATA_DIR / "history" / "walkforward.json"

PRICE_BANDS = (
    (0.00, 0.05, "0-5c"), (0.05, 0.15, "5-15c"), (0.15, 0.35, "15-35c"),
    (0.35, 0.65, "35-65c"), (0.65, 0.85, "65-85c"), (0.85, 0.95, "85-95c"),
    (0.95, 1.0001, "95-100c"),
)
# Liquidity tiers keyed on OPEN INTEREST (contracts). open_interest persists after settlement
# (unlike liquidity_dollars, which is order-book depth = 0 on a settled market) and is present on
# BOTH harvested history and live candidates, so train/serve use the same axis. Backtest showed the
# edge is real at OI ~ mid and vanishes in the most liquid markets (efficient), so the tier matters.
LIQ_TIERS = ((0.0, 500.0, "thin"), (500.0, 5000.0, "mid"), (5000.0, float("inf"), "deep"))
# Finer OI tiers for granular cells: the tradeable mid band split three ways (B2).
FINE_LIQ_TIERS = ((0.0, 500.0, "thin"), (500.0, 1000.0, "mid1"), (1000.0, 2000.0, "mid2"),
                  (2000.0, 5000.0, "mid3"), (5000.0, float("inf"), "deep"))
# Market-lifetime bands (open->close), present on history AND live markets.
DUR_BANDS = ((0.0, 2.0, "d0-2"), (2.0, 14.0, "d2-14"), (14.0, float("inf"), "d14+"))


def price_band(p: float) -> str:
    for lo, hi, name in PRICE_BANDS:
        if lo <= p < hi:
            return name
    return "95-100c"


def liq_tier(liq: float) -> str:
    for lo, hi, name in LIQ_TIERS:
        if lo <= liq < hi:
            return name
    return "deep"


def fine_liq_tier(liq: float) -> str:
    for lo, hi, name in FINE_LIQ_TIERS:
        if lo <= liq < hi:
            return name
    return "deep"


def dur_band(days: Optional[float]) -> Optional[str]:
    if not isinstance(days, (int, float)) or days < 0:
        return None
    for lo, hi, name in DUR_BANDS:
        if lo <= days < hi:
            return name
    return "d14+"


def cell_key(category: str, implied: float, liquidity: float) -> str:
    return f"{category}|{price_band(implied)}|{liq_tier(liquidity)}"


def granular_cell_key(category: str, implied: float, liquidity: float,
                      duration_days: Optional[float]) -> Optional[str]:
    db = dur_band(duration_days)
    if db is None:
        return None
    return f"{category}|{price_band(implied)}|{fine_liq_tier(liquidity)}|{db}"


def stable_hash(s: str) -> int:
    """Deterministic string hash (md5) — python's hash() is randomized per process and must
    never be used for train/test splits (the old fit script's split silently varied per run)."""
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)


def _logit(p: float) -> float:
    p = min(1 - _EPS, max(_EPS, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x < -30:
        return _EPS
    if x > 30:
        return 1 - _EPS
    return 1 / (1 + math.exp(-x))


MAX_FIT_SAMPLE = 20_000   # grid-search cap per cell: 2-param Platt needs no more; a 4M-row
                          # exotics cell must not cost 1.7B sigmoid evals (B3 full-universe fit)


def _fit_platt(rows) -> Optional[dict]:
    """Grid-search (a,b) minimizing Brier on a cell's (implied, outcome), shrunk toward identity.

    rows: iterable of (implied_yes, outcome). Returns {a,b,n,brier_raw,brier_cal} or None if too
    few. Cells larger than MAX_FIT_SAMPLE are evenly-strided down for the grid search (plenty
    for 2 parameters); ``n`` still reports the FULL cell size so shrinkage stays honest.
    """
    pairs = [(float(p), int(y)) for (p, y) in rows]
    n = len(pairs)
    if n < MIN_CELL_N:
        return None
    if n > MAX_FIT_SAMPLE:
        stride = n // MAX_FIT_SAMPLE + 1
        pairs = pairs[::stride]
    n_fit = len(pairs)
    brier_raw = sum((p - y) ** 2 for p, y in pairs) / n_fit
    logit_pairs = [(_logit(p), y) for p, y in pairs]   # precompute: the grid reuses these 425x
    best = (1.0, 0.0, brier_raw)
    a = 0.4
    while a <= 2.0 + 1e-9:
        b = -1.2
        while b <= 1.2 + 1e-9:
            br = sum((_sigmoid(a * lp + b) - y) ** 2 for lp, y in logit_pairs) / n_fit
            if br < best[2]:
                best = (a, b, br)
            b += 0.1
        a += 0.1
    a, b, brier_cal = best
    # shrink toward identity (a=1, b=0)
    w = n / (n + K_CELL)
    a_s = 1.0 + (a - 1.0) * w
    b_s = b * w
    return {"a": round(a_s, 4), "b": round(b_s, 4), "n": n,
            "brier_raw": round(brier_raw, 5), "brier_cal": round(brier_cal, 5)}


class CalibrationMap:
    """Per-cell market->outcome correction, at coarse AND granular cell granularity.

    Apply with .calibrate(category, implied, liquidity[, duration_days]) — granular first when a
    qualified granular cell exists, else coarse, else identity."""

    def __init__(self, cells: Optional[dict] = None):
        self.cells = cells or {}

    @classmethod
    def fit(cls, rows) -> "CalibrationMap":
        """rows: iterable of dicts with category, implied_yes, outcome, open_interest,
        duration_days. Fits coarse cells always; granular cells where duration is present
        (both live in one dict — key shape distinguishes them, 3 parts vs 4)."""
        buckets: dict[str, list] = {}
        for r in rows:
            p = r.get("implied_yes")
            y = r.get("outcome")
            if not isinstance(p, (int, float)) or y not in (0, 1):
                continue
            # liquidity axis = open_interest (persists post-settlement; present live too)
            oi = r.get("open_interest", 0.0) or 0.0
            cat = r.get("category", "?")
            buckets.setdefault(cell_key(cat, p, oi), []).append((p, y))
            gkey = granular_cell_key(cat, p, oi, r.get("duration_days"))
            if gkey:
                buckets.setdefault(gkey, []).append((p, y))
        cells = {}
        for key, pairs in buckets.items():
            fit = _fit_platt(pairs)
            if fit is not None:
                cells[key] = fit
        return cls(cells)

    def calibrate(self, category: str, implied: float, liquidity: float,
                  duration_days: Optional[float] = None) -> dict:
        """Return {calibrated, raw, key, corrected, granularity}. Granular cell first (when
        duration known and the cell qualified), else coarse, else raw (track the market)."""
        liquidity = liquidity or 0.0
        gkey = granular_cell_key(category, implied, liquidity, duration_days)
        for key, gran in ((gkey, "granular"), (cell_key(category, implied, liquidity), "coarse")):
            if key is None:
                continue
            c = self.cells.get(key)
            if not c:
                continue
            cal = _sigmoid(c["a"] * _logit(implied) + c["b"])
            return {"calibrated": round(cal, 4), "raw": implied, "key": key, "corrected": True,
                    "granularity": gran, "a": c["a"], "b": c["b"], "n": c["n"]}
        return {"calibrated": implied, "raw": implied,
                "key": cell_key(category, implied, liquidity),
                "corrected": False, "granularity": "none"}

    def save(self, path=CALIB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cells": self.cells, "params": {"K_CELL": K_CELL, "MIN_CELL_N": MIN_CELL_N}}, indent=2))

    @classmethod
    def load(cls, path=CALIB_PATH) -> "CalibrationMap":
        try:
            data = json.loads(path.read_text())
            return cls(data.get("cells", {}))
        except (OSError, ValueError):
            return cls({})


def load_atlas() -> dict:
    """Load the inefficiency atlas (cells flagged beatable). Empty dict if not built yet."""
    try:
        return json.loads((config.DATA_DIR / "history" / "atlas.json").read_text())
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Walk-forward gating (B2): only cells that made money in month-by-month
# out-of-sample validation are tradeable by the live screen.
# ---------------------------------------------------------------------------

def load_walkforward() -> dict:
    """{cell_key: {n, pnl, roi, positive, folds}} from walkforward_validate.py; {} if absent."""
    try:
        data = json.loads(WALKFORWARD_PATH.read_text())
        return data.get("cells", {})
    except (OSError, ValueError):
        return {}


def tradeable_cell(key: str, wf: Optional[dict] = None) -> bool:
    """A cell may take LIVE-SCREEN positions only if walk-forward validation was positive.

    No walk-forward record at all (file missing) fails CLOSED for the screen — run
    walkforward_validate.py after every refit. The forecaster anchor is NOT gated by this
    (calibration can help a forecast even where trading the cell wouldn't clear fees)."""
    wf = load_walkforward() if wf is None else wf
    rec = wf.get(key)
    return bool(rec and rec.get("positive"))
