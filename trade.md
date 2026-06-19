# Real-Money Trade Ledger

Hand-maintained record of **actual money** trades placed on Kalshi (distinct from the
system's paper leans in `data/`). The model recommends; you decide and execute; you log it here.

> This repo is **private** (github.com/AllanWessels/kalshi_soft), so this ledger is tracked and
> committed alongside the rest of the project.

## Conventions
- **Side** = YES or NO contract you bought. **Entry $** = price paid per contract (incl. the ask).
- **Edge gap** = |my model prob − market implied|, in percentage points; sign noted (fade = betting against the crowd's lean).
- **Status** = OPEN / WON / LOST / CLOSED-EARLY.
- **P&L** = realized per-contract on resolution: WON → (1.00 − entry − fee); LOST → −(entry).

## Open positions

| Date | Ticker | Market | Side | Entry $ | Contracts | Stake | My prob | Mkt impl | Edge gap | Conf | Status | P&L | Notes |
|------|--------|--------|------|---------|-----------|-------|---------|----------|----------|------|--------|-----|-------|
| 2026-06-19 | KXGOVSDNOMR-26-TDOE | Toby Doeden = SD GOP Gov nominee? | NO | 0.56 | _(fill in)_ | _(fill in)_ | 0.27 | 0.445 | 17.5pt (fade) | medium | OPEN | — | Resolves at SD GOP nominee (Jul 28 runoff: Doeden 31% vs incumbent Rhoden 25%). System EV +0.15/contract; gate confirmed NO. ⚠️ sits in two historically-losing buckets (10–20pt fade + medium conf) — see caveat below. |
| 2026-06-19 | KXTRUMPSAY-26JUN22-URAN | Trump says "Uranium" before Jun 22, 2026? | YES | 0.21 | _(fill in)_ | _(fill in)_ | 0.99 | 0.185 | 80.5pt | high | OPEN | — | System EV **+0.76/contract** — the standout edge. Already said "uranium" at the G7 Jun 18 ("right to enrich uranium"); core issue of the US-Iran deal signed Jun 19. Resolves Jun 22. Qwen gate vetoed it but the veto is advisory for high-confidence (policy A), so the lean held. This is the high-confidence/near-certain profile — the *good* bucket, unlike the SD-gov fade. |

## Closed positions

| Date in | Ticker | Market | Side | Entry $ | Exit/Res | P&L | ROI | Notes |
|---------|--------|--------|------|---------|----------|-----|-----|-------|
| — | — | — | — | — | — | — | — | (none yet) |

## Running tally
- Trades: 2 open, 0 closed
- Realized P&L: $0.00 (no real-money trade has resolved yet)
- Open: SD-gov NO @ 0.56 (resolves at runoff), Trump-"Uranium" YES @ 0.21 (resolves Jun 22)

---
### Caveat on the open SD-gov trade
The system's counterfactual conditioning (n=22, provisional) shows money is made on **high-confidence,
small-gap (≤5pt)** leans and **lost** on **wide fades (10–20pt+)** and **medium/low-confidence** bets.
This SD-gov position is a ~17.5pt fade at medium confidence — i.e. in the buckets that have *not* paid
historically, even though it cleared the EV floor and the adversarial gate. Treat it as a real but
higher-variance bet, not a model exemplar. (Sample is tiny; this is a flag, not a verdict.)
