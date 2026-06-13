"""Pure deterministic forecasting math for the kalshi_soft superforecaster.

All functions are stdlib-only (plus ``lib.schemas``). No third-party imports.
All probabilities are floats in [0, 1]. Prices/quotes from Kalshi are in
dollars (probability terms, 0..1) unless noted.

Public API
----------
brier(prob, outcome) -> float
edge(mine, market) -> Optional[float]
market_implied_from_quote(yes_bid, yes_ask, last_price) -> Optional[float]
compute_calibration(resolutions, n_bins) -> schemas.Calibration
drift_series(record) -> list[tuple]
"""

from __future__ import annotations

from typing import Callable, Optional

from lib import schemas, taxonomy


# ---------------------------------------------------------------------------
# Brier score
# ---------------------------------------------------------------------------

def brier(prob: float, outcome: int) -> float:
    """Return the Brier score ``(prob - outcome) ** 2``.

    Parameters
    ----------
    prob:
        Forecast probability in [0, 1].
    outcome:
        Realised outcome: 1 (YES) or 0 (NO).
    """
    return (prob - outcome) ** 2


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------

def edge(mine: float, market: Optional[float]) -> Optional[float]:
    """Return ``mine - market``, or ``None`` if *market* is ``None``."""
    if market is None:
        return None
    return mine - market


# ---------------------------------------------------------------------------
# Market-implied probability from bid/ask/last
# ---------------------------------------------------------------------------

def market_implied_from_quote(
    yes_bid: Optional[float],
    yes_ask: Optional[float],
    last_price: Optional[float] = None,
    **_ignored,  # tolerate extra quote keys (no_bid/no_ask) when called as **market_quote(...)
) -> Optional[float]:
    """Derive a single market-implied probability from quote data.

    Inputs are in dollars (probability terms, 0..1) or ``None``.

    Resolution order:
    1. Midpoint of ``yes_bid`` and ``yes_ask`` when both are present.
    2. Whichever of ``yes_bid`` / ``yes_ask`` is present when exactly one is.
    3. ``last_price`` when bid and ask are both ``None``.
    4. ``None`` when everything is ``None``.

    The result is clamped to ``[0.0, 1.0]``.
    """
    raw: Optional[float]

    if yes_bid is not None and yes_ask is not None:
        raw = (yes_bid + yes_ask) / 2.0
    elif yes_bid is not None:
        raw = yes_bid
    elif yes_ask is not None:
        raw = yes_ask
    elif last_price is not None:
        raw = last_price
    else:
        return None

    return max(0.0, min(1.0, raw))


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def _mean(xs: list[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _category_key(r: schemas.Resolution) -> str:
    return r.category or "uncategorized"


def _segment_key(r: schemas.Resolution) -> str:
    """Group key ``"<category> / <subcategory>"``; classifies on the fly when the
    stored subcategory is absent (e.g. resolutions written before taxonomy existed)."""
    cat = r.category or "uncategorized"
    sub = r.subcategory or taxonomy.classify_subcategory(r.ticker, r.title, cat)
    return f"{cat} / {sub}"


def _strategy_key(r: schemas.Resolution) -> str:
    """Group key by the strategy/arm that produced the forecast (experiment harness).
    Resolutions written before strategy tagging fall into ``"untagged"``."""
    return r.strategy_id or "untagged"


def aggregate_by(
    resolutions: list[schemas.Resolution],
    key_fn: Callable[[schemas.Resolution], str],
) -> dict[str, dict]:
    """Group *resolutions* by ``key_fn(r)`` and return per-group performance stats.

    Each value is ``{n, brier_mine_mean, brier_market_mean, skill_vs_market}``
    where ``skill_vs_market = brier_market_mean - brier_mine_mean`` (positive =
    we beat the market in that group). Market means use only rows that had a
    market-implied probability, so a group's market mean may rest on fewer rows
    than ``n``.
    """
    groups: dict[str, dict] = {}
    for r in resolutions:
        g = groups.setdefault(key_fn(r), {"mine": [], "market": []})
        g["mine"].append(
            r.brier_mine if r.brier_mine is not None
            else brier(r.final_my_probability, r.outcome)
        )
        if r.final_market_implied is not None:
            g["market"].append(
                r.brier_market if r.brier_market is not None
                else brier(r.final_market_implied, r.outcome)
            )

    out: dict[str, dict] = {}
    for key, g in groups.items():
        mine_mean = _mean(g["mine"])
        market_mean = _mean(g["market"])
        skill = (
            market_mean - mine_mean
            if mine_mean is not None and market_mean is not None
            else None
        )
        out[key] = {
            "n": len(g["mine"]),
            "brier_mine_mean": mine_mean,
            "brier_market_mean": market_mean,
            "skill_vs_market": skill,
        }
    return out


def compute_calibration(
    resolutions: list[schemas.Resolution],
    n_bins: int = 10,
) -> schemas.Calibration:
    """Build a ``schemas.Calibration`` from a list of resolved forecasts.

    Parameters
    ----------
    resolutions:
        List of ``schemas.Resolution`` objects (resolved markets).
    n_bins:
        Number of equal-width probability bins over [0, 1]. Default 10.

    Notes
    -----
    * Each bin i covers ``[i/n_bins, (i+1)/n_bins)``, except the last bin
      which also includes the upper bound (1.0).
    * ``brier_mine_mean`` uses ``r.brier_mine`` when available, otherwise
      computes ``brier(r.final_my_probability, r.outcome)``.
    * ``brier_market_mean`` uses ``r.brier_market`` when available (and
      ``r.final_market_implied`` is not None), otherwise computes from
      ``final_market_implied`` when present.
    * ``skill_vs_market = brier_market_mean - brier_mine_mean`` (positive
      means we beat the market).
    """
    # --- bins ---------------------------------------------------------------
    bins: list[schemas.CalibrationBin] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        in_bin = [
            r for r in resolutions
            if (r.final_my_probability >= lo and
                (r.final_my_probability < hi if i < n_bins - 1 else r.final_my_probability <= hi))
        ]
        n = len(in_bin)
        mean_forecast: Optional[float] = (
            sum(r.final_my_probability for r in in_bin) / n if n > 0 else None
        )
        observed_freq: Optional[float] = (
            sum(r.outcome for r in in_bin) / n if n > 0 else None
        )
        bins.append(schemas.CalibrationBin(
            range=[lo, hi],
            n=n,
            mean_forecast=mean_forecast,
            observed_freq=observed_freq,
        ))

    # --- brier mine ---------------------------------------------------------
    mine_scores: list[float] = []
    for r in resolutions:
        if r.brier_mine is not None:
            mine_scores.append(r.brier_mine)
        else:
            mine_scores.append(brier(r.final_my_probability, r.outcome))

    brier_mine_mean: Optional[float] = (
        sum(mine_scores) / len(mine_scores) if mine_scores else None
    )

    # --- brier market -------------------------------------------------------
    market_scores: list[float] = []
    for r in resolutions:
        if r.final_market_implied is None:
            continue
        if r.brier_market is not None:
            market_scores.append(r.brier_market)
        else:
            market_scores.append(brier(r.final_market_implied, r.outcome))

    brier_market_mean: Optional[float] = (
        sum(market_scores) / len(market_scores) if market_scores else None
    )

    # --- skill --------------------------------------------------------------
    skill_vs_market: Optional[float] = None
    if brier_market_mean is not None and brier_mine_mean is not None:
        skill_vs_market = brier_market_mean - brier_mine_mean

    # --- per-segment aggregation (category, category/subcategory, strategy) --
    by_category_out = aggregate_by(resolutions, _category_key)
    by_segment_out = aggregate_by(resolutions, _segment_key)
    by_strategy_out = aggregate_by(resolutions, _strategy_key)

    # --- realized-profit scoreboards (lazy import: profit imports scoring) ---
    from lib import profit  # noqa: E402 — deferred to avoid the profit<->scoring cycle
    profit_by_category_out = profit.aggregate_profit(resolutions, _category_key)
    profit_by_segment_out = profit.aggregate_profit(resolutions, _segment_key)
    profit_by_strategy_out = profit.aggregate_profit(resolutions, _strategy_key)

    return schemas.Calibration(
        updated_at=schemas.utc_now_iso(),
        n_resolved=len(resolutions),
        brier_mine_mean=brier_mine_mean,
        brier_market_mean=brier_market_mean,
        skill_vs_market=skill_vs_market,
        bins=bins,
        by_category=by_category_out,
        by_segment=by_segment_out,
        by_strategy=by_strategy_out,
        profit_by_category=profit_by_category_out,
        profit_by_segment=profit_by_segment_out,
        profit_by_strategy=profit_by_strategy_out,
    )


# ---------------------------------------------------------------------------
# Drift series
# ---------------------------------------------------------------------------

def drift_series(record: schemas.ForecastRecord) -> list[tuple]:
    """Return the forecast history as a list of 3-tuples.

    Each tuple is ``(as_of: str, my_probability: float,
    market_implied_probability: Optional[float])`` in chronological order
    (as stored in ``record.history``).
    """
    return [
        (entry.as_of, entry.my_probability, entry.market_implied_probability)
        for entry in record.history
    ]


# ---------------------------------------------------------------------------
# Profitability (fee-aware)
# ---------------------------------------------------------------------------

import math as _math  # noqa: E402


def kalshi_fee(price: Optional[float], contracts: int = 1, fee_rate: float = 0.07) -> float:
    """Kalshi trading fee in dollars: ``ceil(fee_rate * contracts * price * (1-price))``
    rounded UP to the next cent. ``price`` is in dollars (0..1). Settlement is free,
    so this is the only fee on a round trip. Returns 0.0 for an invalid price.
    """
    if price is None or price <= 0 or price >= 1:
        return 0.0
    raw = fee_rate * contracts * price * (1.0 - price)
    return _math.ceil(raw * 100.0) / 100.0


def net_ev_at_price(
    my_prob: float,
    side: str,
    price: Optional[float],
    fee_rate: float = 0.07,
) -> Optional[float]:
    """Net expected profit per contract (dollars) for buying ``side`` at an explicit
    ``price`` (dollars), after the Kalshi fee at that price. None if price is None.

    win_prob = my_prob (YES) or 1-my_prob (NO);  EV = win_prob - price - fee(price).
    Use the ASK for a spot/taker order, the BID for a resting limit order.
    """
    if price is None:
        return None
    win_prob = my_prob if side == "YES" else (1.0 - my_prob)
    return win_prob - price - kalshi_fee(price, fee_rate=fee_rate)


def expected_net_profit(
    my_prob: float,
    side: str,
    yes_ask: Optional[float],
    no_ask: Optional[float],
    fee_rate: float = 0.07,
) -> Optional[float]:
    """Net expected profit per 1 contract (dollars), after the entry fee, for buying
    ``side`` (\"YES\" or \"NO\") at the ask. None if the needed ask is unavailable.

    YES: pay ``yes_ask``; win $1 with prob ``my_prob``  -> EV = my_prob - yes_ask - fee
    NO:  pay ``no_ask`` ; win $1 with prob ``1-my_prob`` -> EV = (1-my_prob) - no_ask - fee
    """
    if side == "YES":
        if yes_ask is None:
            return None
        return my_prob - yes_ask - kalshi_fee(yes_ask, fee_rate=fee_rate)
    if side == "NO":
        if no_ask is None:
            return None
        return (1.0 - my_prob) - no_ask - kalshi_fee(no_ask, fee_rate=fee_rate)
    return None


def modal_tradable(
    my_prob: float,
    yes_ask: Optional[float],
    no_ask: Optional[float],
    fee_rate: float = 0.07,
    min_ev: float = 0.0,
) -> tuple:
    """Return ``(side, ev, fee)`` for a lean that NEVER opposes your modal forecast.

    A lean may only back the side you consider MORE LIKELY THAN NOT (my_prob vs 0.5),
    and only if buying that side is +EV (>= min_ev). This refuses to recommend betting
    against your own prediction (e.g. lean NO when you think YES is 80% likely): if your
    modal outcome is overpriced, the answer is "no value bet" (NONE), not the opposite side.

    Returns the MODAL side's ev/fee even when it is NONE (negative/insufficient EV), so the
    caller can show it as indicative ("I lean X but it isn't priced as value").
    """
    side = "YES" if my_prob >= 0.5 else "NO"
    price = yes_ask if side == "YES" else no_ask
    ev = net_ev_at_price(my_prob, side, price, fee_rate=fee_rate)
    fee = kalshi_fee(price, fee_rate=fee_rate) if price is not None else None
    if ev is None or ev < min_ev:
        return ("NONE", ev, fee)
    return (side, ev, fee)


def confidence_gate(
    side: str,
    my_prob: float,
    market_implied: Optional[float],
    confidence: str,
    max_gap: float = 0.20,
) -> tuple:
    """Decide whether a positive-EV lean is ACTIONABLE given epistemic confidence.

    Returns ``(ok: bool, note: str)``. EV is computed from ``my_prob`` as if it were
    truth; this gate stops us from dressing up model error as edge:
      - confidence "low"            -> never actionable (probability too shaky to fade the crowd)
      - |my_prob - market| > max_gap and confidence != "high" -> probable model error -> not actionable
    """
    if side == "NONE":
        return (False, "")
    if confidence == "low":
        return (False, "low confidence — estimate too uncertain to fade the market")
    if market_implied is not None:
        gap = abs(my_prob - market_implied)
        if gap > max_gap and confidence != "high":
            return (False,
                    f"{round(gap*100)}pt gap vs a liquid market without high confidence — "
                    f"more likely my error than edge")
    return (True, "")


def best_tradable(
    my_prob: float,
    yes_ask: Optional[float],
    no_ask: Optional[float],
    fee_rate: float = 0.07,
    min_ev: float = 0.0,
) -> tuple:
    """Return ``(side, ev, fee)`` for the side with the higher net EV, or
    ``("NONE", best_ev, fee)`` if neither side clears ``min_ev``.

    ``side`` is "YES"/"NO"/"NONE"; ``ev`` is the net $/contract on that side;
    ``fee`` is the entry fee on that side.
    """
    ev_yes = expected_net_profit(my_prob, "YES", yes_ask, no_ask, fee_rate)
    ev_no = expected_net_profit(my_prob, "NO", yes_ask, no_ask, fee_rate)
    candidates = [(s, ev) for s, ev in (("YES", ev_yes), ("NO", ev_no)) if ev is not None]
    if not candidates:
        return ("NONE", None, None)
    side, ev = max(candidates, key=lambda x: x[1])
    ask = yes_ask if side == "YES" else no_ask
    fee = kalshi_fee(ask, fee_rate=fee_rate)
    if ev is None or ev < min_ev:
        return ("NONE", ev, fee)
    return (side, ev, fee)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # --- brier ---------------------------------------------------------------
    assert brier(1.0, 1) == 0.0, "brier(1, 1) should be 0"
    assert brier(0.0, 0) == 0.0, "brier(0, 0) should be 0"
    assert abs(brier(0.7, 0) - 0.49) < 1e-9, "brier(0.7, 0)"
    assert abs(brier(0.3, 1) - 0.49) < 1e-9, "brier(0.3, 1)"
    assert brier(0.5, 1) == 0.25, "brier(0.5, 1)"

    # --- edge ----------------------------------------------------------------
    assert abs(edge(0.6, 0.5) - 0.1) < 1e-9, "edge positive"
    assert abs(edge(0.4, 0.5) - (-0.1)) < 1e-9, "edge negative"
    assert edge(0.6, None) is None, "edge with None market"

    # --- market_implied_from_quote -------------------------------------------
    # Both bid and ask present -> midpoint
    assert market_implied_from_quote(0.4, 0.6) == 0.5
    # Only bid
    assert market_implied_from_quote(0.45, None) == 0.45
    # Only ask
    assert market_implied_from_quote(None, 0.55) == 0.55
    # Neither -> last_price fallback
    assert market_implied_from_quote(None, None, 0.72) == 0.72
    # Everything None -> None
    assert market_implied_from_quote(None, None) is None
    # Clamping
    assert market_implied_from_quote(1.1, None) == 1.0
    assert market_implied_from_quote(-0.1, None) == 0.0
    assert market_implied_from_quote(0.8, 1.2) == 1.0  # mid = 1.0, clamp to 1.0

    # --- compute_calibration -------------------------------------------------
    r1 = schemas.Resolution(
        ticker="A",
        category="politics",
        outcome=1,
        final_my_probability=0.8,
        final_market_implied=0.75,
        brier_mine=None,
        brier_market=None,
    )
    r2 = schemas.Resolution(
        ticker="B",
        category="politics",
        outcome=0,
        final_my_probability=0.2,
        final_market_implied=0.3,
        brier_mine=None,
        brier_market=None,
    )
    r3 = schemas.Resolution(
        ticker="C",
        category="economy",
        outcome=1,
        final_my_probability=0.6,
        final_market_implied=None,
        brier_mine=0.09,  # pre-computed
        brier_market=None,
    )

    cal = compute_calibration([r1, r2, r3])

    assert cal.n_resolved == 3, f"n_resolved={cal.n_resolved}"
    assert len(cal.bins) == 10, f"bin count={len(cal.bins)}"

    # r1 -> brier_mine = (0.8-1)^2 = 0.04; r2 -> (0.2-0)^2 = 0.04; r3 -> 0.09
    expected_mine_mean = (0.04 + 0.04 + 0.09) / 3
    assert abs(cal.brier_mine_mean - expected_mine_mean) < 1e-9, (
        f"brier_mine_mean={cal.brier_mine_mean} expected={expected_mine_mean}"
    )

    # only r1 and r2 have final_market_implied
    expected_market_mean = (brier(0.75, 1) + brier(0.3, 0)) / 2
    assert abs(cal.brier_market_mean - expected_market_mean) < 1e-9, (
        f"brier_market_mean={cal.brier_market_mean} expected={expected_market_mean}"
    )

    expected_skill = expected_market_mean - expected_mine_mean
    assert abs(cal.skill_vs_market - expected_skill) < 1e-9

    assert "politics" in cal.by_category
    assert cal.by_category["politics"]["n"] == 2
    assert "economy" in cal.by_category
    assert cal.by_category["economy"]["n"] == 1

    # bin containing 0.8 (bin 8: [0.8, 0.9)) should have r1
    bin8 = cal.bins[8]
    assert bin8.n == 1, f"bin8.n={bin8.n}"
    assert bin8.mean_forecast == 0.8
    assert bin8.observed_freq == 1.0

    # bin containing 0.2 (bin 2: [0.2, 0.3)) should have r2
    bin2 = cal.bins[2]
    assert bin2.n == 1, f"bin2.n={bin2.n}"

    # --- drift_series --------------------------------------------------------
    fe1 = schemas.ForecastEntry(as_of="2025-01-01T00:00:00Z", my_probability=0.55,
                                market_implied_probability=0.50)
    fe2 = schemas.ForecastEntry(as_of="2025-01-08T00:00:00Z", my_probability=0.60,
                                market_implied_probability=None)
    rec = schemas.ForecastRecord(ticker="TEST", history=[fe1, fe2])
    ds = drift_series(rec)
    assert len(ds) == 2
    assert ds[0] == ("2025-01-01T00:00:00Z", 0.55, 0.50)
    assert ds[1] == ("2025-01-08T00:00:00Z", 0.60, None)

    # edge case: empty record
    assert drift_series(schemas.ForecastRecord(ticker="EMPTY")) == []

    print("scoring OK")
