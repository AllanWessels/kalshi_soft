"""paper_broker.py — run one paper-broker cycle: place / maintain / settle / report (D2).

The rec ledger proves the SIGNAL; this proves the EXECUTION: resting limit orders simulated
against the live Kalshi book with D1 sizing (quarter-Kelly + exposure caps on a NOTIONAL
bankroll), GTC expiry, and an equity curve with a drawdown halt. The no-fill/expiry rate this
produces is the number that separates the simulation from the assume-filled backtest.

PAPER ONLY — there is deliberately no live-order code anywhere (see docs/EXECUTION.md).

Usage: python3 scripts/paper_broker.py [--max-new N]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

from lib import config
from lib.broker import PaperBroker
from lib.kalshi_client import KalshiClient


def main() -> int:
    ap = argparse.ArgumentParser(description="One paper-broker cycle (place/maintain/settle)")
    ap.add_argument("--max-new", type=int, default=20)
    args = ap.parse_args()

    broker = PaperBroker(KalshiClient(*config.load_secrets()))

    st = broker.settle()
    mt = broker.maintain()
    placed = broker.place_from_recs(max_new=args.max_new)

    if placed and placed[0].get("halted"):
        print(f"PLACEMENT HALTED: {placed[0]['reason']}")
        placed = []
    print(f"settled {st['newly_settled']} | maintained {mt['checked']} "
          f"(fill events {mt['fill_events']}, expired {mt['expired']}) | placed {len(placed)}")
    for o in placed:
        tag = o["status"]
        px = f" @ {o['fill_price']}" if o.get("fill_price") else f" limit {o['limit_price']}"
        print(f"  {o['ticker']:<34} {o['side']:<3} qty={o['qty']:<3} {tag}{px}  "
              f"[{o.get('sizing_note', '')}]"
              + (f" ({o.get('skip_reason')})" if o.get("skip_reason") else ""))

    s = broker.summary()
    eq = s["equity"]
    print("\n=== PAPER BROKER ===")
    print(f"  orders: {s['orders']} {s['by_status']}")
    nfr = s["no_fill_rate_terminal"]
    print(f"  no-fill rate (terminal orders): {'—' if nfr is None else format(nfr, '.0%')}")
    if s["settled_n"]:
        print(f"  settled: {s['settled_n']} | P&L ${s['settled_pnl']:+.2f} on "
              f"${s['settled_cost']:.2f} (ROI {s['settled_roi']:+.2%})")
    print(f"  equity: ${eq['equity']:.2f} / notional ${eq['bankroll_notional']:.0f} "
          f"| deployed ${eq['open_deployed']:.2f} | drawdown {eq['current_drawdown']:.1%}"
          + ("  ** DRAWDOWN HALT ACTIVE **" if eq["halted"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
