"""broker.py — order/position layer for the structural edge. PAPER ONLY (Workstream D2).

The rec ledger (lib/recledger) answers "was the signal right?". This layer answers the question
that actually separates paper from profit: "would ORDERS have filled, at what price, at what
SIZE — and what does the equity curve look like?" It simulates resting limit orders against the
LIVE Kalshi orderbook over time:

  * place  — every open rec becomes ONE paper limit order, sized by the D1 policy
             (quarter-Kelly capped by per-market / per-cell / per-event-family / total-deployed
             fractions of a NOTIONAL bankroll). Marketable-now orders fill immediately at the
             ask (never better than the book), depth-capped; the remainder rests.
  * maintain — each cycle, every resting order is re-checked against the live book. It fills
             (partially, by displayed depth) only when the observed ask crosses its limit —
             fill price = the ASK at observation (what you'd actually pay), never the limit
             when the ask is better. Orders expire GTC after order_expiry_days or at market
             close; expiry is recorded, because the NO-FILL rate is the honesty stat that
             separates this simulation from the assume-filled backtest.
  * settle — filled positions realize P&L at market resolution; the equity curve updates; the
             drawdown halt (D1) blocks new placements when equity falls 15% off its peak.

Fill-model honesty (stated plainly): between-run book snapshots cannot see intra-cycle trades,
so "ask crossed the limit at a check" is the fill trigger. That is NEITHER queue-optimistic
(we never fill just because price touched the level — the displayed offer must be takeable at
or under our limit) NOR fully pessimistic (a real resting order could fill from flow we never
observe). It is the best fill evidence obtainable from public snapshots; the LiveBroker
(docs/EXECUTION.md, D3 — design only) replaces it with real fills.

State: data/paper_orders.jsonl — one row per order, atomically rewritten on transitions
(resting -> filled/partial/expired/settled). The same interface (place/maintain/settle) is the
contract a future LiveBroker implements; NO live-order code exists anywhere by design.
"""
from __future__ import annotations

import datetime
import json
import math
from typing import Any, Optional

from . import config, schemas, recledger

ORDERS_PATH = config.DATA_DIR / "paper_orders.jsonl"

DEFAULT_SIZING = {
    "kelly_fraction": 0.25,
    "per_market_cap_frac": 0.02,
    "per_cell_cap_frac": 0.10,
    "per_event_family_cap_frac": 0.05,
    "total_deployed_cap_frac": 0.50,
    "drawdown_halt_frac": -0.15,
    "order_expiry_days": 5,
    "paper_bankroll_notional": 1000.0,
    "live_bankroll": None,
}


def load_sizing_policy() -> dict:
    try:
        pol = json.loads((config.DATA_DIR / "policy.json").read_text())
    except (OSError, ValueError):
        pol = {}
    merged = dict(DEFAULT_SIZING)
    merged.update(pol.get("position_sizing") or {})
    return merged


def _fee(price: float) -> float:
    return math.ceil(config.KALSHI_FEE_RATE * price * (1 - price) * 100) / 100.0


def _now_dt() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(s: Optional[str]) -> Optional[datetime.datetime]:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def event_family(ticker: str) -> str:
    """Correlation bucket for exposure caps: the event ticker (strip the outcome leg)."""
    t = ticker or ""
    return t.rsplit("-", 1)[0] if "-" in t else t


def kelly_fraction(p_win: float, cost: float) -> float:
    """Binary Kelly: stake fraction f* = (p - c) / (1 - c) for cost c, payout 1. Clamped >=0."""
    if not (0.0 < cost < 1.0):
        return 0.0
    return max(0.0, (p_win - cost) / (1.0 - cost))


# ---------------------------------------------------------------------------
# Order store
# ---------------------------------------------------------------------------

def load_orders() -> list[dict]:
    rows: list[dict] = []
    if ORDERS_PATH.exists():
        for line in ORDERS_PATH.open():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def write_orders(rows: list[dict]) -> None:
    tmp = ORDERS_PATH.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(ORDERS_PATH)


# ---------------------------------------------------------------------------
# Book reading (same semantics as recledger.snapshot_fill_evidence)
# ---------------------------------------------------------------------------

def _best_ask(client, ticker: str, side: str) -> tuple[Optional[float], float]:
    """(ask, depth_units) for buying `side` right now — from the opposing best bid."""
    try:
        ob = (client.get_orderbook(ticker) or {}).get("orderbook_fp") or {}
    except Exception:
        return None, 0.0
    levels = ob.get("yes_dollars") if side == "NO" else ob.get("no_dollars")
    best_p, best_q = None, 0.0
    for lvl in levels or []:
        try:
            pr, q = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if best_p is None or pr > best_p:
            best_p, best_q = pr, q
    if best_p is None:
        return None, 0.0
    return round(1 - best_p, 4), best_q


# ---------------------------------------------------------------------------
# Equity / exposure accounting
# ---------------------------------------------------------------------------

def equity_stats(orders: list[dict], sizing: Optional[dict] = None) -> dict:
    """Equity curve + exposure from the order rows (notional-bankroll accounting)."""
    sz = sizing or load_sizing_policy()
    bankroll = float(sz["paper_bankroll_notional"])
    realized = sum(o.get("realized_pnl") or 0.0 for o in orders if o.get("status") == "settled")
    # Deployed = filled cost + fees, PLUS resting reservations (limit x qty) — a resting order
    # commits capital the moment it is placed, or the total-deployed cap would leak.
    deployed = 0.0
    for o in orders:
        if o.get("status") in ("filled", "partial"):
            deployed += (o.get("fill_price") or 0.0) * (o.get("filled_qty") or 0) \
                        + (o.get("fee_paid") or 0.0)
        elif o.get("status") == "resting":
            deployed += float(o.get("limit_price") or 0.0) * (o.get("qty") or 0)
    # equity peak from settled sequence (by settle time)
    settled = sorted((o for o in orders if o.get("status") == "settled"),
                     key=lambda o: o.get("settled_at") or "")
    eq, peak, max_dd = bankroll, bankroll, 0.0
    for o in settled:
        eq += o.get("realized_pnl") or 0.0
        peak = max(peak, eq)
        max_dd = min(max_dd, (eq - peak) / peak if peak else 0.0)
    cur_dd = (eq - peak) / peak if peak else 0.0
    return {"bankroll_notional": bankroll, "equity": round(eq, 2),
            "realized_pnl": round(realized, 2), "open_deployed": round(deployed, 2),
            "peak": round(peak, 2), "current_drawdown": round(cur_dd, 4),
            "max_drawdown": round(max_dd, 4),
            "halted": cur_dd <= float(sz["drawdown_halt_frac"])}


def _exposure_by(orders: list[dict], key_fn) -> dict:
    out: dict[str, float] = {}
    for o in orders:
        if o.get("status") in ("filled", "partial", "resting"):
            # count resting orders' committed cost too — capital is reserved, not free
            qty = o.get("filled_qty") or 0
            cost = (o.get("fill_price") or 0.0) * qty + (o.get("fee_paid") or 0.0)
            if o.get("status") == "resting":
                cost = o["limit_price"] * o["qty"]
            k = key_fn(o)
            out[k] = out.get(k, 0.0) + cost
    return out


# ---------------------------------------------------------------------------
# The broker
# ---------------------------------------------------------------------------

class PaperBroker:
    """Paper order lifecycle against the live book. The LiveBroker contract (D3, design
    only) implements this same surface: place_from_recs / maintain / settle / summary."""

    def __init__(self, client, sizing: Optional[dict] = None):
        self.client = client
        self.sizing = sizing or load_sizing_policy()
        self.orders = load_orders()

    # -- placement ---------------------------------------------------------

    def _size_order(self, rec: dict, cost: float) -> tuple[int, str]:
        """Contracts to buy under quarter-Kelly + exposure caps. Returns (qty, note)."""
        sz = self.sizing
        bankroll = float(sz["paper_bankroll_notional"])
        p_win = (1 - float(rec["calibrated_yes"])) if rec["side"] == "NO" \
            else float(rec["calibrated_yes"])
        f_star = kelly_fraction(p_win, cost)
        alloc = min(float(sz["kelly_fraction"]) * f_star,
                    float(sz["per_market_cap_frac"])) * bankroll
        # remaining headroom under each exposure cap
        eq = equity_stats(self.orders, sz)
        total_cap = float(sz["total_deployed_cap_frac"]) * bankroll
        headroom = [("total", total_cap - eq["open_deployed"])]
        cell_exp = _exposure_by(self.orders, lambda o: o.get("cell") or "?")
        headroom.append(("cell", float(sz["per_cell_cap_frac"]) * bankroll
                         - cell_exp.get(rec.get("cell") or "?", 0.0)))
        fam_exp = _exposure_by(self.orders, lambda o: event_family(o.get("ticker") or ""))
        headroom.append(("event-family", float(sz["per_event_family_cap_frac"]) * bankroll
                         - fam_exp.get(event_family(rec.get("ticker") or ""), 0.0)))
        binding = min(headroom, key=lambda kv: kv[1])
        alloc = min(alloc, max(0.0, binding[1]))
        qty = int(alloc // max(cost, 1e-6))
        note = (f"kelly f*={f_star:.3f} alloc=${alloc:.2f}"
                + (f" (bound: {binding[0]})" if binding[1] < alloc + cost else ""))
        return qty, note

    def place_from_recs(self, max_new: int = 20) -> list[dict]:
        """Turn open ledger recs without an order into paper limit orders (one per rec)."""
        eq = equity_stats(self.orders, self.sizing)
        if eq["halted"]:
            return [{"halted": True, "reason":
                     f"drawdown {eq['current_drawdown']:.1%} <= halt "
                     f"{self.sizing['drawdown_halt_frac']:.0%} — no new paper orders"}]
        have = {o.get("rec_key") for o in self.orders}
        placed = []
        for rec in recledger.load_rows():
            if rec.get("status") == "resolved" or len(placed) >= max_new:
                continue
            rec_key = f"{rec.get('ticker')}|{(rec.get('ts') or '')[:10]}"
            if rec_key in have:
                continue
            limit = float(rec["entry_limit"])
            fee = _fee(limit)
            qty, note = self._size_order(rec, limit + fee)
            order = {
                "order_id": f"po-{rec_key}", "rec_key": rec_key,
                "ticker": rec.get("ticker"), "side": rec.get("side"),
                "cell": rec.get("cell"), "limit_price": limit, "qty": qty,
                "placed_at": schemas.utc_now_iso(),
                "expires_at": self._expiry_iso(rec),
                "status": "resting", "filled_qty": 0, "fill_price": None,
                "fee_paid": 0.0, "fills": [], "sizing_note": note,
                "close_time": rec.get("close_time"),
            }
            if qty < 1:
                order.update({"status": "skipped", "skip_reason":
                              "caps/Kelly allocate <1 contract"})
            else:
                # marketable-now? fill immediately at the ask (never better than the book)
                ask, depth = _best_ask(self.client, order["ticker"], order["side"])
                if ask is not None and ask <= limit + 1e-9:
                    self._fill(order, ask, min(qty, max(1, int(depth))))
            self.orders.append(order)
            placed.append(order)
        write_orders(self.orders)
        return placed

    def _expiry_iso(self, rec: dict) -> str:
        days = float(self.sizing["order_expiry_days"])
        exp = _now_dt() + datetime.timedelta(days=days)
        close = _parse_iso(rec.get("close_time"))
        if close is not None:
            exp = min(exp, close)
        return exp.strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- lifecycle ---------------------------------------------------------

    def _fill(self, order: dict, price: float, qty: int) -> None:
        qty = max(0, min(qty, order["qty"] - order["filled_qty"]))
        if qty <= 0:
            return
        fee = _fee(price) * qty
        order["fills"].append({"at": schemas.utc_now_iso(), "price": price, "qty": qty})
        total = order["filled_qty"] + qty
        prev = (order["fill_price"] or 0.0) * order["filled_qty"]
        order["fill_price"] = round((prev + price * qty) / total, 4)   # avg fill
        order["filled_qty"] = total
        order["fee_paid"] = round(order["fee_paid"] + fee, 4)
        order["status"] = "filled" if total >= order["qty"] else "partial"

    def maintain(self) -> dict:
        """Re-check every resting/partial order against the live book; expire stale ones."""
        now = _now_dt()
        checked = filled = expired = 0
        for o in self.orders:
            if o.get("status") not in ("resting", "partial"):
                continue
            checked += 1
            exp = _parse_iso(o.get("expires_at"))
            if exp is not None and now >= exp:
                o["status"] = "expired" if o["filled_qty"] == 0 else "partial_expired"
                expired += 1
                continue
            ask, depth = _best_ask(self.client, o["ticker"], o["side"])
            if ask is not None and ask <= o["limit_price"] + 1e-9 and depth >= 1:
                self._fill(o, ask, min(o["qty"] - o["filled_qty"], max(1, int(depth))))
                filled += 1
        write_orders(self.orders)
        return {"checked": checked, "fill_events": filled, "expired": expired}

    def settle(self) -> dict:
        """Realize P&L on filled orders whose markets have settled."""
        settled = 0
        for o in self.orders:
            if o.get("status") not in ("filled", "partial", "partial_expired"):
                continue
            if o.get("filled_qty", 0) <= 0:
                continue
            try:
                m = self.client.get_market(o["ticker"])
            except Exception:
                continue
            status = (m.get("status") or "").lower()
            result = (m.get("result") or "").strip().lower()
            if status not in ("settled", "finalized") or result not in ("yes", "no"):
                continue
            won = (result == "no") if o["side"] == "NO" else (result == "yes")
            payoff = o["filled_qty"] * (1.0 if won else 0.0)
            cost = o["fill_price"] * o["filled_qty"] + o["fee_paid"]
            o.update({"status": "settled", "outcome": result, "won": won,
                      "realized_pnl": round(payoff - cost, 4),
                      "settled_at": schemas.utc_now_iso()})
            settled += 1
        write_orders(self.orders)
        return {"newly_settled": settled}

    # -- reporting ---------------------------------------------------------

    def summary(self) -> dict:
        by_status: dict[str, int] = {}
        for o in self.orders:
            by_status[o.get("status") or "?"] = by_status.get(o.get("status") or "?", 0) + 1
        settled = [o for o in self.orders if o.get("status") == "settled"]
        placed_fillable = [o for o in self.orders
                           if o.get("status") not in ("skipped",)]
        n_terminal = sum(1 for o in placed_fillable
                         if o.get("status") in ("settled", "expired", "partial_expired"))
        n_expired_unfilled = by_status.get("expired", 0)
        cost = sum(o["fill_price"] * o["filled_qty"] + o["fee_paid"] for o in settled)
        pnl = sum(o.get("realized_pnl") or 0.0 for o in settled)
        return {
            "orders": len(self.orders), "by_status": by_status,
            "no_fill_rate_terminal": round(n_expired_unfilled / n_terminal, 3) if n_terminal else None,
            "settled_n": len(settled),
            "settled_pnl": round(pnl, 2), "settled_cost": round(cost, 2),
            "settled_roi": round(pnl / cost, 4) if cost else None,
            "equity": equity_stats(self.orders, self.sizing),
        }
