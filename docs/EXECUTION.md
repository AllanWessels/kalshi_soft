# EXECUTION.md — LiveBroker design spec (Workstream D3 — DESIGN ONLY)

**Status: specification. There is deliberately NO live-order code in this repository.**
Building it is gated on BOTH: (1) the A4 verification bar passing on conservative fills
(`data/policy.json:structural_verification`), and (2) the user explicitly signing off,
provisioning a **trading-authorized** API key, and setting `position_sizing.live_bankroll`
(currently `null`; user constraint 2026-07-17: *no live trading until profitability is
verified, no bankroll yet*). Until both hold, the only broker is `lib/broker.PaperBroker`.

## Contract

`LiveBroker` implements the exact `PaperBroker` surface so the routine swaps brokers without
changing any orchestration:

| method            | PaperBroker (today)                          | LiveBroker (spec)                          |
|-------------------|----------------------------------------------|--------------------------------------------|
| `place_from_recs` | simulate limit orders, D1 sizing             | `POST /portfolio/orders` (limit, GTC+expiry) |
| `maintain`        | fill when observed ask crosses limit         | reconcile real fills via `GET /portfolio/fills`; cancel expired via `DELETE /portfolio/orders/{id}` |
| `settle`          | realize at market resolution                 | reconcile `GET /portfolio/settlements`     |
| `summary`         | equity/no-fill/ROI from simulated state      | same, from exchange-truth state            |

## Kalshi trading API surface (v2, `/trade-api/v2`)

- Auth: same RSA-PSS signing already in `lib/kalshi_client.py`, but the key must have trade
  scope (current key is read-only market data). **Never** commit key material; `lib/gitops`
  guard already blocks it.
- `POST /portfolio/orders` — {ticker, action: buy, side: yes|no, type: limit, count,
  yes_price|no_price (cents), expiration_ts, client_order_id}. `client_order_id` = our
  `order_id` (`po-<ticker>|<date>`) for idempotency — resubmission after a crash must not
  double-place.
- `GET /portfolio/orders` / `GET /portfolio/fills` / `GET /portfolio/settlements` /
  `GET /portfolio/balance` — reconciliation truth. Local state (`data/live_orders.jsonl`,
  same schema as paper) is a CACHE of exchange truth, never the authority.
- `DELETE /portfolio/orders/{order_id}` — expiry/cancel path.

## Reconciliation loop (every routine cycle)

1. Pull open orders + fills + settlements + balance from the exchange.
2. Diff against local state; exchange wins every conflict; log every divergence loudly
   (a divergence is a bug or a partial outage — never silently adopt it).
3. Apply the same D1 rails BEFORE placement: quarter-Kelly on `live_bankroll`, per-market 2%,
   per-cell 10%, per-event-family 5%, total 50%, drawdown halt −15% from equity peak
   (equity = balance + mark-to-market of open positions at bid).
4. A4 kill switches apply identically: killed cell ⇒ cancel its resting orders, no new ones;
   global halt ⇒ cancel all resting, place nothing, alert.

## Additional live-only safeguards (beyond paper)

- **Size floor sanity**: refuse any order where `count × price > per_market_cap` — belt over
  the sizing braces (a sizing bug must fail closed, not fat-finger).
- **Price sanity**: refuse limits > 5c through the current book (a stale-book protection).
- **Rate/burst cap**: ≤ N orders per cycle (start N=10), ≤ 1 order per ticker per day —
  mirrors the ledger's idempotency.
- **Dry-run flag**: `LIVE_BROKER_DRY_RUN=1` logs would-be orders without POSTing — the first
  week of any live rollout runs dry against the real key to validate signing + shapes.
- **Kill file**: presence of `data/HALT_LIVE_TRADING` stops all placement unconditionally
  (a human-reachable big red button that needs no code change or deploy).

## Rollout ladder (each step gated on the previous)

1. Paper broker accrues ≥ 4 weeks / n≥40 verified-fill terminal orders (A4 bar).
2. User sign-off + trading key + `live_bankroll` set (user-owned; never inferred).
3. Dry-run week: real key, `DRY_RUN=1`, diff would-be fills vs paper fills.
4. Live at minimum size (1-contract orders) for ≥ 20 settlements; compare realized ROI vs
   paper ROI — the live-vs-paper gap IS the execution-quality metric.
5. Scale to D1 sizing only if the gap ≤ 3 ROI points; else halt and investigate.
