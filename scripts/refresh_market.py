"""refresh_market.py — fetch live Kalshi market state and print JSON to stdout.

CLI
---
  --selftest          ping the Kalshi API; print "kalshi OK" or
                      "kalshi UNREACHABLE: <reason>"; exit 0 either way.
  --ticker TICKER     print a single-market JSON object to stdout; exit 0.
                      On fetch error prints {"error": "..."} JSON; exit 1.
  --batch T1,T2,...   print a JSON list, one object per ticker; exit 0.
                      Failed tickers appear as {"ticker":..,"error":..} entries.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
from typing import Any, Optional

from lib import config, kalshi_client, scoring


# ---------------------------------------------------------------------------
# Market object builder
# ---------------------------------------------------------------------------

def _build_market_obj(ticker: str, client: kalshi_client.KalshiClient) -> dict[str, Any]:
    """Fetch a single market and return the standardised dict.

    Raises KalshiError on fetch failure (caller wraps in try/except).
    """
    market = client.get_market(ticker)

    # --- title ---------------------------------------------------------------
    title: Optional[str] = (
        market.get("title")
        or market.get("subtitle")
        or market.get("question")
        or ""
    )

    # --- status / close_time / result ----------------------------------------
    status: Optional[str] = market.get("status")
    close_time: Optional[str] = market.get("close_time") or market.get("expiration_time")
    result: Optional[str] = market.get("result") or ""

    # --- quote ---------------------------------------------------------------
    quote = kalshi_client.market_quote(market)
    yes_bid: Optional[float] = quote["yes_bid"]
    yes_ask: Optional[float] = quote["yes_ask"]
    no_bid: Optional[float] = quote["no_bid"]
    no_ask: Optional[float] = quote["no_ask"]
    last_price: Optional[float] = quote["last_price"]

    # --- fees ----------------------------------------------------------------
    fee_yes: Optional[float] = (
        scoring.kalshi_fee(yes_ask, fee_rate=config.KALSHI_FEE_RATE)
        if yes_ask is not None else None
    )
    fee_no: Optional[float] = (
        scoring.kalshi_fee(no_ask, fee_rate=config.KALSHI_FEE_RATE)
        if no_ask is not None else None
    )

    # --- market-implied probability ------------------------------------------
    market_implied_probability: Optional[float] = scoring.market_implied_from_quote(
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        last_price=last_price,
    )

    market_price_cents: Optional[int] = (
        round(market_implied_probability * 100)
        if market_implied_probability is not None
        else None
    )

    # --- volume / OI ---------------------------------------------------------
    # Kalshi may expose these under various key names; try the known ones.
    # Kalshi market objects carry these as fixed-point strings (*_fp); the bare keys are usually
    # absent, so include the _fp fallbacks or OI/volume come back None (which mis-tiers the
    # history-calibration anchor to "thin"). _fp helper parses the string to float.
    def _fp(*keys):
        for k in keys:
            v = market.get(k)
            if v is not None:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    continue
        return None

    volume_24h: Optional[float] = _fp(
        "volume_24h", "volume24h", "daily_volume", "volume_24h_fp",
    )
    open_interest: Optional[float] = _fp(
        "open_interest", "open_interest_value", "open_interest_fp",
    )

    return {
        "ticker": ticker,
        "title": title,
        "status": status,
        "close_time": close_time,
        "result": result,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last_price": last_price,
        "market_implied_probability": market_implied_probability,
        "market_price_cents": market_price_cents,
        "volume_24h": volume_24h,
        "open_interest": open_interest,
        "fee_yes": fee_yes,
        "fee_no": fee_no,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch live Kalshi market state and print JSON to stdout.",
        add_help=True,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--selftest",
        action="store_true",
        help="Ping the Kalshi API; print plain-text result; exit 0 always.",
    )
    mode.add_argument(
        "--ticker",
        metavar="TICKER",
        help="Single market ticker to fetch; prints JSON object.",
    )
    mode.add_argument(
        "--batch",
        metavar="T1,T2,...",
        help="Comma-separated list of tickers; prints JSON array.",
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # selftest
    # ------------------------------------------------------------------
    if args.selftest:
        try:
            key_id, pem = config.load_secrets()
            client = kalshi_client.KalshiClient(key_id=key_id, private_key_pem=pem)
            ok = client.ping()
            if ok:
                print("kalshi OK")
            else:
                print("kalshi UNREACHABLE: ping returned False")
        except Exception as exc:  # noqa: BLE001
            print(f"kalshi UNREACHABLE: {type(exc).__name__}: {exc}")
        sys.exit(0)

    # ------------------------------------------------------------------
    # Shared client for --ticker / --batch
    # ------------------------------------------------------------------
    key_id, pem = config.load_secrets()
    client = kalshi_client.KalshiClient(key_id=key_id, private_key_pem=pem)

    # ------------------------------------------------------------------
    # --ticker
    # ------------------------------------------------------------------
    if args.ticker:
        try:
            obj = _build_market_obj(args.ticker.strip(), client)
            print(json.dumps(obj))
            sys.exit(0)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": str(exc)}))
            sys.exit(1)

    # ------------------------------------------------------------------
    # --batch
    # ------------------------------------------------------------------
    tickers = [t.strip() for t in args.batch.split(",") if t.strip()]
    results: list[dict] = []
    for ticker in tickers:
        try:
            obj = _build_market_obj(ticker, client)
            results.append(obj)
        except Exception as exc:  # noqa: BLE001
            results.append({"ticker": ticker, "error": str(exc)})
    print(json.dumps(results))
    sys.exit(0)


if __name__ == "__main__":
    main()
