"""atlas.py — shared market-(in)efficiency model learned from settled history.

Two leakage-free artifacts, both keyed on cells = (category, price_band, liquidity_tier):

  * inefficiency atlas (scripts/inefficiency_atlas.py): where is the crowd miscalibrated?
  * market-calibration map (this module): a per-cell correction implied_price -> calibrated P(yes),
    fit by SHRUNK Platt scaling on thousands of past (implied, outcome) pairs.

Why Platt (logit a,b) and not just a bias: the longshot inefficiency the atlas finds is a *bias*
(b shifts the crowd's price) AND often an over/under-confidence (a scales it). Both are learned,
then shrunk toward identity (a=1,b=0) by n/(n+K) so a thin cell can't buy a wild correction.

Used out-of-sample only: the map is fit on PAST settled markets and applied to FUTURE live ones —
legitimate calibration (Ch 14), not contamination. backtest_history.py proves it train/test split.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from . import config

_EPS = 1e-6
K_CELL = 60          # shrink a cell's Platt fit toward identity until it has >> 60 resolved markets
MIN_CELL_N = 40      # below this, no correction at all (track the raw market)

CALIB_PATH = config.DATA_DIR / "history" / "market_calibration.json"

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


def cell_key(category: str, implied: float, liquidity: float) -> str:
    return f"{category}|{price_band(implied)}|{liq_tier(liquidity)}"


def _logit(p: float) -> float:
    p = min(1 - _EPS, max(_EPS, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x < -30:
        return _EPS
    if x > 30:
        return 1 - _EPS
    return 1 / (1 + math.exp(-x))


def _fit_platt(rows) -> Optional[dict]:
    """Grid-search (a,b) minimizing Brier on a cell's (implied, outcome), shrunk toward identity.

    rows: iterable of (implied_yes, outcome). Returns {a,b,n,brier_raw,brier_cal} or None if too few.
    """
    pairs = [(float(p), int(y)) for (p, y) in rows]
    n = len(pairs)
    if n < MIN_CELL_N:
        return None
    brier_raw = sum((p - y) ** 2 for p, y in pairs) / n
    best = (1.0, 0.0, brier_raw)
    a = 0.4
    while a <= 2.0 + 1e-9:
        b = -1.2
        while b <= 1.2 + 1e-9:
            br = sum((_sigmoid(a * _logit(p) + b) - y) ** 2 for p, y in pairs) / n
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
    """Per-cell market->outcome correction. Apply with .calibrate(category, implied, liquidity)."""

    def __init__(self, cells: Optional[dict] = None):
        self.cells = cells or {}

    @classmethod
    def fit(cls, rows) -> "CalibrationMap":
        """rows: iterable of dicts with category, implied_yes, outcome, liquidity."""
        buckets: dict[str, list] = {}
        for r in rows:
            p = r.get("implied_yes")
            y = r.get("outcome")
            if not isinstance(p, (int, float)) or y not in (0, 1):
                continue
            # liquidity axis = open_interest (persists post-settlement; present live too)
            key = cell_key(r.get("category", "?"), p, r.get("open_interest", 0.0) or 0.0)
            buckets.setdefault(key, []).append((p, y))
        cells = {}
        for key, pairs in buckets.items():
            fit = _fit_platt(pairs)
            if fit is not None:
                cells[key] = fit
        return cls(cells)

    def calibrate(self, category: str, implied: float, liquidity: float) -> dict:
        """Return {calibrated, raw, key, corrected}. No cell fit => returns raw (track market)."""
        key = cell_key(category, implied, liquidity or 0.0)
        c = self.cells.get(key)
        if not c:
            return {"calibrated": implied, "raw": implied, "key": key, "corrected": False}
        cal = _sigmoid(c["a"] * _logit(implied) + c["b"])
        return {"calibrated": round(cal, 4), "raw": implied, "key": key, "corrected": True,
                "a": c["a"], "b": c["b"], "n": c["n"]}

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
