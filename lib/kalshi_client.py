"""Read-only Kalshi REST client.

Public API
----------
KalshiError          -- raised on non-2xx responses after retries
KalshiClient         -- main client class (see docstring)
parse_dollars(value) -- parse a *_dollars string/number to float or None
market_quote(market) -- extract yes_bid/yes_ask/last_price floats from a market dict

Run ``python3 -m lib.kalshi_client --selftest`` for a connectivity smoke test.
"""

from __future__ import annotations

import argparse
import base64
import time
import urllib.parse
from typing import Any, Generator, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from lib import config

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class KalshiError(Exception):
    """Raised for non-2xx Kalshi API responses (after retries are exhausted)."""


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def parse_dollars(value: Any) -> Optional[float]:
    """Parse a ``*_dollars`` string or number to a float.

    Returns None for None, empty string, or any unparseable value.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def market_quote(market: dict) -> dict:
    """Extract yes_bid, yes_ask, last_price from a market dict.

    Prefers ``*_dollars`` keys (decimal strings). Falls back to legacy cent-based
    integer fields (``yes_bid``, ``yes_ask``, ``last_price``) divided by 100.

    Returns
    -------
    dict with keys ``yes_bid``, ``yes_ask``, ``no_bid``, ``no_ask``, ``last_price``
    (each float or None). The NO side falls back to the YES book identity
    (``no_ask = 1 - yes_bid``, ``no_bid = 1 - yes_ask``) when explicit NO quotes are
    absent — this is what you'd pay to take the other side.
    """
    def _get(dollars_key: str, cents_key: str) -> Optional[float]:
        if dollars_key in market:
            return parse_dollars(market[dollars_key])
        raw = market.get(cents_key)
        if raw is not None:
            try:
                return float(raw) / 100.0
            except (ValueError, TypeError):
                pass
        return None

    yes_bid = _get("yes_bid_dollars", "yes_bid")
    yes_ask = _get("yes_ask_dollars", "yes_ask")
    no_bid = _get("no_bid_dollars", "no_bid")
    no_ask = _get("no_ask_dollars", "no_ask")
    if no_ask is None and yes_bid is not None:
        no_ask = round(1.0 - yes_bid, 6)
    if no_bid is None and yes_ask is not None:
        no_bid = round(1.0 - yes_ask, 6)

    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last_price": _get("last_price_dollars", "last_price"),
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class KalshiClient:
    """Read-only Kalshi REST client.

    Parameters
    ----------
    key_id:
        Kalshi API key identifier (KALSHI-ACCESS-KEY header). If omitted,
        requests are made without authentication headers (fine for public
        market-data endpoints).
    private_key_pem:
        PEM-encoded RSA private key string used for RSA-PSS request signing.
        Optional; if omitted no signing is performed.
    base_url:
        Primary base URL (default ``config.KALSHI_BASE_URL``).
    fallback_url:
        Secondary base URL tried once on ``ConnectionError`` from the primary
        (default ``config.KALSHI_FALLBACK_URL``).
    """

    def __init__(
        self,
        key_id: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        base_url: str = config.KALSHI_BASE_URL,
        fallback_url: str = config.KALSHI_FALLBACK_URL,
    ) -> None:
        self._key_id = key_id
        self._private_key = None
        if private_key_pem:
            self._private_key = serialization.load_pem_private_key(
                private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem,
                password=None,
            )
        self._base_url = base_url.rstrip("/")
        self._fallback_url = fallback_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        # Rate-limit state
        self._req_times: list[float] = []

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    def _sign_headers(self, method: str, path: str) -> dict[str, str]:
        """Return auth headers for the given request if a private key is loaded."""
        if not self._private_key or not self._key_id:
            return {}
        ts_ms = str(int(time.time() * 1000))
        # path must be the URL path including /trade-api/v2 prefix, query stripped
        parsed_path = urllib.parse.urlparse(self._base_url + path).path
        msg = (ts_ms + method.upper() + parsed_path).encode("utf-8")
        sig = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(sig).decode("ascii")
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        }

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """Block until we are within the allowed request rate."""
        now = time.monotonic()
        window = 1.0  # one second window
        # Purge timestamps older than the window
        self._req_times = [t for t in self._req_times if now - t < window]
        if len(self._req_times) >= config.KALSHI_MAX_REQ_PER_SEC:
            sleep_until = self._req_times[0] + window
            sleep_dur = sleep_until - now
            if sleep_dur > 0:
                time.sleep(sleep_dur)
            # Refresh after sleep
            now = time.monotonic()
            self._req_times = [t for t in self._req_times if now - t < window]
        self._req_times.append(time.monotonic())

    # ------------------------------------------------------------------
    # Core request
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        _use_fallback: bool = False,
    ) -> Any:
        """Execute a signed HTTP request with retries and rate limiting.

        Retries on 429 and 5xx (exponential backoff, up to
        ``config.KALSHI_MAX_RETRIES``). On ``ConnectionError`` from the primary
        base URL, retries once against ``fallback_url``.

        Raises
        ------
        KalshiError
            On non-2xx responses after all retries are exhausted.
        """
        base = self._fallback_url if _use_fallback else self._base_url
        url = base + path
        # Strip empty/None params
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        else:
            params = None

        last_exc: Optional[Exception] = None
        for attempt in range(config.KALSHI_MAX_RETRIES + 1):
            self._throttle()
            headers = self._sign_headers(method, path)
            try:
                resp = self._session.request(
                    method,
                    url,
                    params=params or None,
                    headers=headers,
                    timeout=config.KALSHI_TIMEOUT_S,
                )
            except requests.ConnectionError as exc:
                if not _use_fallback:
                    # Try fallback once (no further retries for connection errors)
                    return self._request(method, path, params=params, _use_fallback=True)
                last_exc = exc
                # Connection errors on fallback are re-raised immediately
                raise KalshiError(f"Connection error (primary and fallback): {exc}") from exc

            if resp.status_code < 300:
                try:
                    return resp.json()
                except ValueError:
                    return {}

            # Retriable codes
            if resp.status_code in (429,) or resp.status_code >= 500:
                if attempt < config.KALSHI_MAX_RETRIES:
                    backoff = 2 ** attempt
                    # Respect Retry-After if present
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            backoff = max(backoff, float(retry_after))
                        except ValueError:
                            pass
                    time.sleep(backoff)
                    continue
                # Exhausted retries
                raise KalshiError(
                    f"Kalshi API error {resp.status_code} after {config.KALSHI_MAX_RETRIES} retries: "
                    f"{resp.text[:200]}"
                )

            # Non-retriable error
            raise KalshiError(
                f"Kalshi API error {resp.status_code}: {resp.text[:200]}"
            )

        # Should not reach here, but just in case
        raise KalshiError(f"Request failed after retries; last error: {last_exc}")

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    def get_markets(
        self,
        status: str = "open",
        cursor: Optional[str] = None,
        limit: int = 1000,
        **params: Any,
    ) -> dict:
        """Fetch one page of markets.

        Parameters
        ----------
        status:
            One of ``unopened|open|paused|closed|settled``.
        cursor:
            Pagination cursor from a previous response.
        limit:
            Max results per page (API max 1000).
        **params:
            Additional query parameters (e.g. ``event_ticker``,
            ``series_ticker``, ``tickers``, ``min_close_ts``, ``max_close_ts``).
        """
        p: dict[str, Any] = {"status": status, "limit": limit}
        if cursor:
            p["cursor"] = cursor
        p.update(params)
        return self._request("GET", "/markets", params=p)

    def iter_markets(self, status: str = "open", **params: Any) -> Generator[dict, None, None]:
        """Yield each market dict across all pages for the given filters."""
        cursor: Optional[str] = None
        while True:
            resp = self.get_markets(status=status, cursor=cursor, **params)
            markets = resp.get("markets") or []
            for m in markets:
                yield m
            cursor = resp.get("cursor") or None
            if not cursor:
                break

    def get_market(self, ticker: str) -> dict:
        """Return the inner ``market`` object for a single ticker."""
        resp = self._request("GET", f"/markets/{ticker}")
        return resp.get("market", resp)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def get_events(
        self,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
        with_nested_markets: bool = False,
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> dict:
        """Fetch one page of events."""
        p: dict[str, Any] = {"limit": limit, "with_nested_markets": with_nested_markets}
        if series_ticker:
            p["series_ticker"] = series_ticker
        if status:
            p["status"] = status
        if cursor:
            p["cursor"] = cursor
        return self._request("GET", "/events", params=p)

    def iter_events(self, **params: Any) -> Generator[dict, None, None]:
        """Yield each event dict across all pages."""
        cursor: Optional[str] = None
        # Extract cursor-incompatible pass-through params
        kw = dict(params)
        kw.pop("cursor", None)
        while True:
            resp = self.get_events(cursor=cursor, **kw)
            events = resp.get("events") or []
            for e in events:
                yield e
            cursor = resp.get("cursor") or None
            if not cursor:
                break

    # ------------------------------------------------------------------
    # Series
    # ------------------------------------------------------------------

    def get_series(self, category: Optional[str] = None) -> dict:
        """Fetch the series list (optionally filtered by category)."""
        p: dict[str, Any] = {}
        if category:
            p["category"] = category
        return self._request("GET", "/series", params=p)

    def iter_series(self, category: Optional[str] = None) -> Generator[dict, None, None]:
        """Yield each series dict from the response."""
        resp = self.get_series(category=category)
        for s in resp.get("series") or []:
            yield s

    def get_series_single(self, series_ticker: str) -> dict:
        """Fetch a single series by ticker."""
        return self._request("GET", f"/series/{series_ticker}")

    # ------------------------------------------------------------------
    # Tags / search
    # ------------------------------------------------------------------

    def get_tags_by_categories(self) -> dict:
        """Fetch the tags-by-categories search endpoint."""
        return self._request("GET", "/search/tags_by_categories")

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------

    def get_orderbook(self, ticker: str) -> dict:
        """Fetch the current orderbook for a market."""
        return self._request("GET", f"/markets/{ticker}/orderbook")

    # ------------------------------------------------------------------
    # Ping
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Return True if the API is reachable, False otherwise (never raises)."""
        try:
            self.get_markets(limit=1)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# CLI selftest
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi client selftest")
    parser.add_argument("--selftest", action="store_true", help="Run connectivity smoke test")
    parser.add_argument("--ticker", default=None, help="Optional ticker to look up")
    args = parser.parse_args()

    if not args.selftest:
        parser.print_help()
        raise SystemExit(0)

    key_id, private_key_pem = config.load_secrets()
    client = KalshiClient(key_id=key_id, private_key_pem=private_key_pem)

    try:
        resp = client.get_markets(limit=1)
        n = len(resp.get("markets") or [])
        print(f"kalshi OK — {n} market(s) returned by get_markets(limit=1)")

        if args.ticker:
            try:
                market = client.get_market(args.ticker)
                quote = market_quote(market)
                # Print quote only; never print key material
                print(
                    f"Ticker: {args.ticker}  "
                    f"yes_bid={quote['yes_bid']}  "
                    f"yes_ask={quote['yes_ask']}  "
                    f"last_price={quote['last_price']}"
                )
            except KalshiError as exc:
                print(f"Could not fetch ticker {args.ticker!r}: {exc}")

    except KalshiError as exc:
        print(f"kalshi UNREACHABLE: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"kalshi UNREACHABLE: {type(exc).__name__}: {exc}")
