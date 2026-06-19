"""strategies.py — the forecasting EXPERIMENT harness.

We do not assume which forecasting topology is best; we *measure* it. Each forecast
is produced by a registered "strategy arm" (how many independent forecasters, how
their probabilities are aggregated, whether we crowd-adjust toward the market, and
whether an adversarial red-team pass runs). Every forecast is tagged with its
``strategy_id`` so the resolution scoreboard can discover which arm wins on Brier
AND realized profit, per category.

This module is pure + stdlib-only: it defines the arms, the aggregation math, and a
deterministic arm-selector. The orchestrator (ROUTINE) reads an arm's config to
decide how many forecasters to spawn and how to combine them.

Public API
----------
REGISTRY: dict[str, Strategy]
get(strategy_id) -> Optional[Strategy]
aggregate(probs, method) -> float
crowd_adjust(p, market_price, weight) -> float
combine(probs, strategy, market_price=None) -> float
select_strategy(ticker, by_strategy_stats=None, explore_every=4) -> Strategy
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Strategy:
    id: str
    description: str
    n_forecasters: int = 1
    aggregation: str = "mean"          # mean | trimmed_mean | median
    crowd_adjust_weight: float = 0.0   # 0..1 weight pulled toward the market price
    debate_rounds: int = 0             # 0 = no debate (independent estimation)
    redteam: bool = False              # adversarial critic pass before committing
    forecaster_model: str = "opus"     # forecaster model — ALWAYS "opus" (see directive below)


# The seed arms. Ordered so the round-robin selector cycles through them.
# PROJECT DIRECTIVE (model routing, fixed): **forecasting is always Opus** — never the local
# model, never Sonnet. Qwen (local) is used only for RETRIEVAL condensation and ADVERSARIAL
# analysis (the in-loop challenge gate + the blind post-mortem critic), not for forming forecasts.
# The experiment therefore varies forecasting *topology* (how many Opus forecasters, how they are
# aggregated, crowd-adjust, red-team) — NOT which model forecasts. The former local-forecaster
# arms (L0/L1) are retired; historical L* records remain in the scoreboard but are never re-selected.
REGISTRY: dict[str, Strategy] = {
    "S0-single": Strategy(
        "S0-single", "Single Opus forecaster, no aggregation (baseline)",
        n_forecasters=1, aggregation="mean", forecaster_model="opus"),
    "S1-ensemble3": Strategy(
        "S1-ensemble3", "3 independent Opus forecasters -> trimmed mean",
        n_forecasters=3, aggregation="trimmed_mean", forecaster_model="opus"),
    "S2-ensemble3-crowd": Strategy(
        "S2-ensemble3-crowd", "S1 + crowd-adjust 30% toward the market price",
        n_forecasters=3, aggregation="trimmed_mean", crowd_adjust_weight=0.30,
        forecaster_model="opus"),
    "S3-ensemble3-redteam": Strategy(
        "S3-ensemble3-redteam", "S1 + adversarial red-team pass before commit",
        n_forecasters=3, aggregation="trimmed_mean", redteam=True, forecaster_model="opus"),
}

DEFAULT_STRATEGY = "S1-ensemble3"
_ARM_IDS = list(REGISTRY.keys())


def get(strategy_id: str) -> Optional[Strategy]:
    return REGISTRY.get(strategy_id)


# ---------------------------------------------------------------------------
# Aggregation math
# ---------------------------------------------------------------------------

def _clamp(p: float) -> float:
    return max(0.0, min(1.0, p))


def aggregate(probs: list[float], method: str = "mean") -> float:
    """Combine independent forecaster probabilities into one number.

    trimmed_mean drops the single highest and lowest when n>=3 (robust to one
    outlier forecaster — Halawi'24 / Schoenegger'24 both use robust aggregators);
    median is fully robust; mean is the naive baseline.
    """
    xs = [float(p) for p in probs if p is not None]
    if not xs:
        raise ValueError("aggregate() requires at least one probability")
    if len(xs) == 1:
        return _clamp(xs[0])
    if method == "median":
        return _clamp(statistics.median(xs))
    if method == "trimmed_mean" and len(xs) >= 3:
        s = sorted(xs)
        core = s[1:-1]  # drop one min, one max
        return _clamp(sum(core) / len(core))
    return _clamp(sum(xs) / len(xs))


def crowd_adjust(p: float, market_price: Optional[float], weight: float) -> float:
    """Pull the estimate ``weight`` of the way toward the market price.

    Halawi'24: blending an LLM forecast toward the crowd/market reliably shaves
    ~0.01 Brier. weight=0 or no market price => unchanged. We make this an *arm*
    so the scoreboard tests the claim rather than assuming it.
    """
    if market_price is None or weight <= 0.0:
        return _clamp(p)
    return _clamp((1.0 - weight) * p + weight * market_price)


def combine(probs: list[float], strategy: Strategy,
            market_price: Optional[float] = None) -> float:
    """Full arm pipeline: aggregate forecaster probs, then optional crowd-adjust."""
    agg = aggregate(probs, strategy.aggregation)
    return crowd_adjust(agg, market_price, strategy.crowd_adjust_weight)


# ---------------------------------------------------------------------------
# Arm selection (deterministic; epsilon-greedy once we have evidence)
# ---------------------------------------------------------------------------

def _ticker_index(ticker: str, n: int) -> int:
    """Stable, seed-free index in [0, n) from a ticker (reproducible across runs)."""
    h = 0
    for ch in (ticker or ""):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h % n if n else 0


def select_strategy(
    ticker: str,
    by_strategy_stats: Optional[dict[str, dict]] = None,
    explore_every: int = 4,
) -> Strategy:
    """Pick the arm for *ticker*.

    Cold start (no stats): deterministic round-robin by ticker hash, so all arms
    get exercised and the choice is reproducible. With stats, epsilon-greedy:
    exploit the best arm (highest skill_vs_market, then ROI) most of the time, but
    every ``explore_every``-th ticker (by hash) explore another arm. Stats shape:
    ``{strategy_id: {"skill_vs_market": float|None, "roi": float|None, "n": int}}``.
    """
    if not by_strategy_stats:
        return REGISTRY[_ARM_IDS[_ticker_index(ticker, len(_ARM_IDS))]]

    # Rank arms that have any evidence by skill, then ROI.
    rated = {k: v for k, v in by_strategy_stats.items() if k in REGISTRY and v.get("n", 0) > 0}
    if not rated:
        return REGISTRY[_ARM_IDS[_ticker_index(ticker, len(_ARM_IDS))]]

    def _score(v: dict) -> tuple:
        return (v.get("skill_vs_market") or -1e9, v.get("roi") or -1e9)

    best_id = max(rated, key=lambda k: _score(rated[k]))

    # Explore on every Nth ticker (deterministic), else exploit.
    if explore_every > 0 and _ticker_index(ticker, explore_every) == 0:
        explore_id = _ARM_IDS[_ticker_index(ticker + "x", len(_ARM_IDS))]
        return REGISTRY[explore_id]
    return REGISTRY[best_id]


# ---------------------------------------------------------------------------
# Inline self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    errors: list[str] = []

    def check(name, cond):
        if not cond:
            errors.append(name)

    # aggregation
    check("mean", abs(aggregate([0.2, 0.4, 0.6], "mean") - 0.4) < 1e-9)
    check("median", abs(aggregate([0.1, 0.5, 0.9], "median") - 0.5) < 1e-9)
    # trimmed mean drops 0.05 and 0.95 -> mean(0.4,0.5,0.6)=0.5
    check("trimmed", abs(aggregate([0.05, 0.4, 0.5, 0.6, 0.95], "trimmed_mean") - 0.5) < 1e-9)
    check("single", abs(aggregate([0.73], "mean") - 0.73) < 1e-9)
    check("clamped", aggregate([1.2, 1.4], "mean") == 1.0)

    # crowd adjust: p=0.8, market=0.6, w=0.5 -> 0.7
    check("crowd", abs(crowd_adjust(0.8, 0.6, 0.5) - 0.7) < 1e-9)
    check("crowd_nomarket", abs(crowd_adjust(0.8, None, 0.5) - 0.8) < 1e-9)
    check("crowd_zeroweight", abs(crowd_adjust(0.8, 0.6, 0.0) - 0.8) < 1e-9)

    # combine S2 (trimmed + crowd 0.3): probs trimmed-> all 0.80, market 0.50 -> 0.8*0.7+0.5*0.3=0.71
    s2 = REGISTRY["S2-ensemble3-crowd"]
    check("combine_s2", abs(combine([0.80, 0.80, 0.80], s2, market_price=0.50) - 0.71) < 1e-9)

    # selection cold-start is deterministic + returns a registered arm
    a = select_strategy("KXFOO-26")
    b = select_strategy("KXFOO-26")
    check("select_deterministic", a.id == b.id and a.id in REGISTRY)

    # selection with stats exploits the best arm for most tickers
    stats = {
        "S0-single": {"skill_vs_market": -0.02, "roi": -0.1, "n": 5},
        "S1-ensemble3": {"skill_vs_market": 0.05, "roi": 0.2, "n": 5},
        "S2-ensemble3-crowd": {"skill_vs_market": 0.03, "roi": 0.1, "n": 5},
    }
    exploited = [select_strategy(f"KXT{i}", stats).id for i in range(20)]
    check("select_exploits_best", exploited.count("S1-ensemble3") >= 10)

    if errors:
        print("STRATEGIES TEST FAILURES:", ", ".join(errors))
        raise SystemExit(1)
    print("strategies OK")
