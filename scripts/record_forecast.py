"""CLI: persist one forecast entry for a market.

Usage (agent):
    python3 scripts/record_forecast.py --ticker TICKER --prob 0.62 [options]

The agent provides judgment fields; store.append_forecast_entry computes
edge, prob_delta_vs_prev, and as_of.  The resulting record.current is
printed as pretty JSON so the agent can see the computed edge.

Fee-aware profitability (optional)
-----------------------------------
Pass --yes-ask and/or --no-ask (prices in dollars, [0,1]) to enable
fee-aware lean/conviction derivation.  When --yes-ask is given:
  * scoring.best_tradable() determines lean, ev, and fee.
  * lean is overridden by the profitability result (authoritative).
  * conviction is derived from ev unless --conviction was explicitly passed.
When --yes-ask is NOT given, legacy manual --lean/--conviction apply.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json

from lib import config, store, schemas, scoring  # noqa: E402 (after sys.path patch)


def _comma_list(value: str) -> list[str]:
    """Split a comma-separated string into a stripped list, ignoring empties."""
    return [item.strip() for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="record_forecast",
        description="Persist one forecast entry for a Kalshi market.",
    )

    # --- required ---
    p.add_argument(
        "--ticker",
        required=True,
        help="Kalshi market ticker, e.g. PRES-2026-DEM",
    )
    p.add_argument(
        "--prob",
        required=True,
        type=float,
        help="My probability estimate, float in [0, 1].",
    )

    # --- judgment fields (defaults deferred to None so we can detect explicit passing) ---
    p.add_argument(
        "--confidence",
        choices=list(schemas.CONFIDENCE_LEVELS),
        default="medium",
        help="Epistemic confidence level (default: medium).",
    )
    p.add_argument(
        "--lean",
        choices=list(schemas.LEANS),
        default=None,
        help="Paper-trade direction (default: NONE; overridden by profitability when --yes-ask is given).",
    )
    p.add_argument(
        "--conviction",
        choices=list(schemas.CONVICTION_LEVELS),
        default=None,
        help="Size of paper lean (default: low; derived from EV when --yes-ask is given and this is not passed).",
    )
    p.add_argument(
        "--trigger",
        choices=list(schemas.TRIGGERS),
        default="scheduled",
        help="Why this forecast was produced (default: scheduled).",
    )

    # --- optional market data ---
    p.add_argument(
        "--market-implied",
        dest="market_implied",
        type=float,
        default=None,
        help="Market-implied probability the agent observed, float in [0, 1].",
    )
    p.add_argument(
        "--market-price-cents",
        dest="market_price_cents",
        type=float,
        default=None,
        help="Raw YES midpoint in cents (for audit; optional).",
    )

    # --- fee-aware profitability ---
    p.add_argument(
        "--yes-ask",
        dest="yes_ask",
        type=float,
        default=None,
        help="Price to buy YES (dollars, [0,1]). Enables fee-aware lean/conviction derivation.",
    )
    p.add_argument(
        "--no-ask",
        dest="no_ask",
        type=float,
        default=None,
        help="Price to buy NO (dollars, [0,1]). Used with --yes-ask for profitability calc.",
    )
    p.add_argument(
        "--yes-bid",
        dest="yes_bid",
        type=float,
        default=None,
        help="Best YES bid (dollars). Used to price a resting LIMIT buy if the lean is YES.",
    )
    p.add_argument(
        "--no-bid",
        dest="no_bid",
        type=float,
        default=None,
        help="Best NO bid (dollars). Used to price a resting LIMIT buy if the lean is NO.",
    )

    # --- narrative fields ---
    p.add_argument(
        "--rationale",
        dest="rationale",
        default="",
        help="1-3 sentence rationale summary.",
    )
    p.add_argument(
        "--drivers",
        default="",
        help="Comma-separated key drivers, e.g. 'incumbency,economy'.",
    )
    p.add_argument(
        "--reference-classes",
        dest="reference_classes",
        default="",
        help="Comma-separated reference classes.",
    )
    p.add_argument(
        "--refs",
        default="",
        help="Comma-separated research references / URLs.",
    )

    # --- record-level metadata (passed through to append_forecast_entry) ---
    p.add_argument(
        "--title",
        default=None,
        help="Human-readable market title (populates ForecastRecord on first write).",
    )
    p.add_argument(
        "--category",
        choices=list(schemas.CATEGORIES),
        default=None,
        help="Market category.",
    )
    p.add_argument(
        "--close-time",
        dest="close_time",
        default=None,
        help="Market close time as ISO-8601 UTC, e.g. 2026-12-01T00:00:00Z.",
    )

    return p


def validate_args(args: argparse.Namespace) -> None:
    """Validate probability ranges and ask prices; exit 2 on error."""
    errors: list[str] = []

    if not (0.0 <= args.prob <= 1.0):
        errors.append(f"--prob {args.prob!r} is outside [0, 1].")

    if args.market_implied is not None and not (0.0 <= args.market_implied <= 1.0):
        errors.append(f"--market-implied {args.market_implied!r} is outside [0, 1].")

    if args.yes_ask is not None and not (0.0 <= args.yes_ask <= 1.0):
        errors.append(f"--yes-ask {args.yes_ask!r} is outside [0, 1].")

    if args.no_ask is not None and not (0.0 <= args.no_ask <= 1.0):
        errors.append(f"--no-ask {args.no_ask!r} is outside [0, 1].")

    if errors:
        for msg in errors:
            print(f"ERROR: {msg}", file=sys.stderr)
        sys.exit(2)


def _derive_conviction_from_ev(ev) -> str:
    """Map net EV per contract to a conviction label."""
    if ev is None:
        return "low"
    if ev >= 0.12:
        return "high"
    if ev >= 0.05:
        return "medium"
    return "low"


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    validate_args(args)

    # Resolve lean, conviction, and optional EV/fee fields.
    # When --yes-ask is provided, profitability is authoritative for lean;
    # conviction is derived from EV unless --conviction was explicitly passed.
    yes_ask_field: float | None = None
    no_ask_field: float | None = None
    fee_per_contract: float | None = None
    ev_per_contract: float | None = None
    limit_price: float | None = None
    ev_limit_per_contract: float | None = None
    lean_note: str | None = None

    if args.yes_ask is not None:
        # Fee-aware path.
        yes_ask_field = args.yes_ask
        no_ask_field = args.no_ask

        side, ev, fee = scoring.best_tradable(
            args.prob,
            args.yes_ask,
            args.no_ask,
            fee_rate=config.KALSHI_FEE_RATE,
            min_ev=config.MIN_PROFITABLE_EV,
        )

        # Warn if user also passed an explicit --lean that differs from computed side.
        if args.lean is not None and args.lean != side:
            print(
                f"NOTE: --lean {args.lean!r} overridden by profitability analysis "
                f"(computed side={side!r}); profitability is authoritative.",
                file=sys.stderr,
            )

        lean = side
        fee_per_contract = fee
        ev_per_contract = ev

        # Limit-order alternative: rest a buy on the lean side at its current best bid.
        if side in ("YES", "NO"):
            limit_price = args.yes_bid if side == "YES" else args.no_bid
            ev_limit_per_contract = scoring.net_ev_at_price(
                args.prob, side, limit_price, fee_rate=config.KALSHI_FEE_RATE
            )

        # Conviction: derive from EV unless the user explicitly passed --conviction.
        if args.conviction is not None:
            conviction = args.conviction
        else:
            conviction = _derive_conviction_from_ev(ev) if side != "NONE" else "low"

        # Confidence gate (has the final say): a positive-EV side is only ACTIONABLE if
        # confidence backs it. EV is computed from my probability as if true, so a low-
        # confidence estimate or a large gap vs a liquid market is more likely model error
        # than edge. When gated, keep ev_per_contract as INDICATIVE but set lean=NONE.
        confidence_for_gate = args.confidence if args.confidence is not None else "medium"
        ok, note = scoring.confidence_gate(
            side, args.prob, args.market_implied, confidence_for_gate,
            max_gap=config.MAX_MARKET_DISAGREEMENT,
        )
        if side != "NONE" and not ok:
            lean = "NONE"
            lean_note = note
            conviction = "low"

    else:
        # Legacy path: use manual --lean / --conviction with documented defaults.
        lean = args.lean if args.lean is not None else "NONE"
        conviction = args.conviction if args.conviction is not None else "low"

    # Build the ForecastEntry from CLI args.
    # Leave as_of / edge / prob_delta_vs_prev unset — store computes them.
    entry = schemas.ForecastEntry(
        my_probability=args.prob,
        my_confidence=args.confidence,
        market_implied_probability=args.market_implied,
        market_price_cents=args.market_price_cents,
        yes_ask=yes_ask_field,
        no_ask=no_ask_field,
        fee_per_contract=fee_per_contract,
        ev_per_contract=ev_per_contract,
        limit_price=limit_price,
        ev_limit_per_contract=(round(ev_limit_per_contract, 4)
                               if ev_limit_per_contract is not None else None),
        lean=lean,
        conviction=conviction,
        lean_note=lean_note,
        trigger=args.trigger,
        rationale_summary=args.rationale,
        key_drivers=_comma_list(args.drivers),
        reference_classes=_comma_list(args.reference_classes),
        research_refs=_comma_list(args.refs),
    )

    # Persist via store; store stamps as_of, computes edge and prob_delta.
    rec = store.append_forecast_entry(
        args.ticker,
        entry,
        title=args.title,
        category=args.category,
        close_time=args.close_time,
    )

    # Print current (denormalized newest entry) as pretty JSON.
    if rec.current is None:
        print("{}", file=sys.stdout)
    else:
        print(json.dumps(rec.current.to_dict(), indent=2, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    main()
