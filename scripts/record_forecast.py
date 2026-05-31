"""CLI: persist one forecast entry for a market.

Usage (agent):
    python3 scripts/record_forecast.py --ticker TICKER --prob 0.62 [options]

The agent provides judgment fields; store.append_forecast_entry computes
edge, prob_delta_vs_prev, and as_of.  The resulting record.current is
printed as pretty JSON so the agent can see the computed edge.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json

from lib import config, store, schemas  # noqa: E402 (after sys.path patch)


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

    # --- judgment fields with defaults ---
    p.add_argument(
        "--confidence",
        choices=list(schemas.CONFIDENCE_LEVELS),
        default="medium",
        help="Epistemic confidence level (default: medium).",
    )
    p.add_argument(
        "--lean",
        choices=list(schemas.LEANS),
        default="NONE",
        help="Paper-trade direction (default: NONE).",
    )
    p.add_argument(
        "--conviction",
        choices=list(schemas.CONVICTION_LEVELS),
        default="low",
        help="Size of paper lean (default: low).",
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
    """Validate probability ranges; exit 2 on error."""
    errors: list[str] = []

    if not (0.0 <= args.prob <= 1.0):
        errors.append(f"--prob {args.prob!r} is outside [0, 1].")

    if args.market_implied is not None and not (0.0 <= args.market_implied <= 1.0):
        errors.append(f"--market-implied {args.market_implied!r} is outside [0, 1].")

    if errors:
        for msg in errors:
            print(f"ERROR: {msg}", file=sys.stderr)
        sys.exit(2)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    validate_args(args)

    # Build the ForecastEntry from CLI args.
    # Leave as_of / edge / prob_delta_vs_prev unset — store computes them.
    entry = schemas.ForecastEntry(
        my_probability=args.prob,
        my_confidence=args.confidence,
        market_implied_probability=args.market_implied,
        market_price_cents=args.market_price_cents,
        lean=args.lean,
        conviction=args.conviction,
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
