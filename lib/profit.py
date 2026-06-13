"""profit.py — realized profit/loss metrics for resolved paper trades.

Brier measures calibration; it does NOT measure whether a lean made money. This
module closes that gap: given a resolved market and the forecast entry that was
live at resolution (which carries the recorded ``lean`` + the ``yes_ask``/
``no_ask`` we would have paid + the Kalshi fee), it computes realized P&L, ROI,
win, and closing-line value (CLV) per contract, and aggregates them per segment.

Conventions
-----------
* A "trade" exists only when ``lean in {YES, NO}``. A ``NONE`` lean means we took
  no position, so it contributes nothing to P&L (``trade_from_entry`` returns
  ``None``) — it is not a 0-dollar trade, it is *no* trade.
* Prices are dollars in [0,1] (probability terms). Payout is $1/contract on a win.
* Fee is the Kalshi entry fee on the lean side at the price paid; reused from
  ``lib.scoring.kalshi_fee`` so the fee model stays in one place.

Public API
----------
realized_pnl(side, entry_price, outcome, fee) -> float
clv(side, entry_price, final_market_implied) -> Optional[float]
trade_from_entry(entry, outcome) -> Optional[dict]
aggregate_profit(resolutions, key_fn) -> dict[str, dict]
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from lib import schemas, scoring


# ---------------------------------------------------------------------------
# Per-contract realized economics
# ---------------------------------------------------------------------------

def _won(side: str, outcome: int) -> bool:
    return (side == "YES" and outcome == 1) or (side == "NO" and outcome == 0)


def realized_pnl(side: str, entry_price: float, outcome: int, fee: float) -> float:
    """Net dollars per contract for buying *side* at *entry_price*, after *fee*.

    Win  -> payout $1 minus what we paid minus fee = (1 - entry_price - fee)
    Loss -> we lose the stake and the fee            = -(entry_price + fee)
    """
    if _won(side, outcome):
        return 1.0 - entry_price - fee
    return -entry_price - fee


def clv(side: str, entry_price: float, final_market_implied: Optional[float]) -> Optional[float]:
    """Closing-line value per contract: did the market move toward our side after entry?

    ``closing_side_prob - entry_price`` where the closing price of our side is
    ``final_market_implied`` for YES and ``1 - final_market_implied`` for NO.
    Positive CLV = we bought cheaper than the market's final assessment, the
    skill signal that is independent of the binary outcome. None if no closing price.
    """
    if final_market_implied is None:
        return None
    closing_side_prob = final_market_implied if side == "YES" else (1.0 - final_market_implied)
    return closing_side_prob - entry_price


def trade_from_entry(
    entry: "schemas.ForecastEntry",
    outcome: int,
    final_market_implied: Optional[float] = None,
) -> Optional[dict]:
    """Compute the realized-trade record for a resolved market, or ``None`` if the
    forecast took no position (lean NONE) or lacks the entry price needed to score.

    Returns a dict of profit fields ready to copy onto a ``schemas.Resolution``:
    ``entry_side, entry_price, fee_at_entry, realized_pnl, roi, won, clv``.
    """
    side = (entry.lean or "NONE").upper()
    if side not in ("YES", "NO"):
        return None

    entry_price = entry.yes_ask if side == "YES" else entry.no_ask
    if entry_price is None:
        return None

    fee = entry.fee_per_contract
    if fee is None:
        fee = scoring.kalshi_fee(entry_price)

    pnl = realized_pnl(side, entry_price, outcome, fee)
    staked = entry_price + fee
    roi = (pnl / staked) if staked > 0 else None

    return {
        "entry_side": side,
        "entry_price": entry_price,
        "fee_at_entry": fee,
        "realized_pnl": pnl,
        "roi": roi,
        "won": _won(side, outcome),
        "clv": clv(side, entry_price, final_market_implied),
    }


# ---------------------------------------------------------------------------
# Aggregation across resolved markets
# ---------------------------------------------------------------------------

def _mean(xs: list[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def aggregate_profit(
    resolutions: list["schemas.Resolution"],
    key_fn: Callable[["schemas.Resolution"], str],
) -> dict[str, dict]:
    """Group resolved markets that carry a realized trade by ``key_fn(r)`` and
    return per-group profit stats.

    Only resolutions with a non-null ``realized_pnl`` (i.e. an actual YES/NO lean)
    count as trades; NONE-lean resolutions are ignored here. Each value:
    ``{n_trades, total_pnl, total_staked, roi, win_rate, avg_clv, max_drawdown}``.
    ``max_drawdown`` is the largest peak-to-trough dip of cumulative P&L within the
    group, ordered by ``resolved_at``.
    """
    groups: dict[str, list[schemas.Resolution]] = {}
    for r in resolutions:
        if getattr(r, "realized_pnl", None) is None:
            continue
        groups.setdefault(key_fn(r), []).append(r)

    out: dict[str, dict] = {}
    for key, rs in groups.items():
        rs_ordered = sorted(rs, key=lambda r: r.resolved_at or "")
        pnls = [r.realized_pnl for r in rs_ordered]
        staked = [
            (r.entry_price or 0.0) + (r.fee_at_entry or 0.0)
            for r in rs_ordered
        ]
        wins = [1 for r in rs_ordered if r.won]
        clvs = [r.clv for r in rs_ordered if r.clv is not None]

        total_pnl = sum(pnls)
        total_staked = sum(staked)

        # Max drawdown of cumulative P&L (most negative trough below running peak).
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)

        out[key] = {
            "n_trades": len(rs_ordered),
            "total_pnl": total_pnl,
            "total_staked": total_staked,
            "roi": (total_pnl / total_staked) if total_staked > 0 else None,
            "win_rate": (len(wins) / len(rs_ordered)) if rs_ordered else None,
            "avg_clv": _mean(clvs),
            "max_drawdown": max_dd,
        }
    return out


# ---------------------------------------------------------------------------
# Inline self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    errors: list[str] = []

    def check(name, cond):
        if not cond:
            errors.append(name)

    # realized_pnl: YES wins at 0.40, fee 0.02 -> 1-0.40-0.02 = 0.58
    check("yes_win", abs(realized_pnl("YES", 0.40, 1, 0.02) - 0.58) < 1e-9)
    # YES loses at 0.40 -> -(0.40+0.02) = -0.42
    check("yes_loss", abs(realized_pnl("YES", 0.40, 0, 0.02) - (-0.42)) < 1e-9)
    # NO wins (outcome 0) at 0.30 -> 1-0.30-0.02 = 0.68
    check("no_win", abs(realized_pnl("NO", 0.30, 0, 0.02) - 0.68) < 1e-9)
    # NO loses (outcome 1) at 0.30 -> -0.32
    check("no_loss", abs(realized_pnl("NO", 0.30, 1, 0.02) - (-0.32)) < 1e-9)

    # Breakeven win-rate at p=0.5, fee 0.02: win=+0.48, loss=-0.52 -> WR* = 0.52
    win, loss = realized_pnl("YES", 0.5, 1, 0.02), realized_pnl("YES", 0.5, 0, 0.02)
    wr_star = -loss / (win - loss)
    check("breakeven_wr_~0.52", abs(wr_star - 0.52) < 1e-9)

    # CLV: bought YES at 0.45, market closes implied 0.60 -> +0.15
    check("clv_yes", abs(clv("YES", 0.45, 0.60) - 0.15) < 1e-9)
    # CLV NO: bought NO at 0.30, closing prob of NO = 1-0.55 = 0.45 -> +0.15
    check("clv_no", abs(clv("NO", 0.30, 0.55) - 0.15) < 1e-9)
    check("clv_none", clv("YES", 0.45, None) is None)

    # trade_from_entry: NONE lean -> no trade
    e_none = schemas.ForecastEntry(lean="NONE", yes_ask=0.5, no_ask=0.5)
    check("none_no_trade", trade_from_entry(e_none, 1) is None)

    # trade_from_entry: YES lean at yes_ask 0.40, fee given, outcome win
    e_yes = schemas.ForecastEntry(lean="YES", yes_ask=0.40, no_ask=0.62,
                                  fee_per_contract=0.02)
    t = trade_from_entry(e_yes, 1, final_market_implied=0.55)
    check("trade_side", t["entry_side"] == "YES")
    check("trade_price", abs(t["entry_price"] - 0.40) < 1e-9)
    check("trade_pnl", abs(t["realized_pnl"] - 0.58) < 1e-9)
    check("trade_won", t["won"] is True)
    check("trade_clv", abs(t["clv"] - (0.55 - 0.40)) < 1e-9)
    check("trade_roi", abs(t["roi"] - (0.58 / 0.42)) < 1e-9)

    # aggregate_profit over two trades (one win, one loss)
    r1 = schemas.Resolution(ticker="A", category="politics", resolved_at="2026-06-01T00:00:00Z",
                            outcome=1, entry_side="YES", entry_price=0.40,
                            fee_at_entry=0.02, realized_pnl=0.58, roi=1.38, won=True, clv=0.10)
    r2 = schemas.Resolution(ticker="B", category="politics", resolved_at="2026-06-02T00:00:00Z",
                            outcome=0, entry_side="YES", entry_price=0.40,
                            fee_at_entry=0.02, realized_pnl=-0.42, roi=-1.0, won=False, clv=-0.05)
    agg = aggregate_profit([r1, r2], lambda r: r.category)
    g = agg["politics"]
    check("agg_n", g["n_trades"] == 2)
    check("agg_pnl", abs(g["total_pnl"] - 0.16) < 1e-9)
    check("agg_winrate", abs(g["win_rate"] - 0.5) < 1e-9)
    check("agg_drawdown", g["max_drawdown"] <= 0.0)

    if errors:
        print("PROFIT TEST FAILURES:", ", ".join(errors))
        raise SystemExit(1)
    print("profit OK")
