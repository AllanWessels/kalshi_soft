---
description: Run one superforecaster loop iteration (research due markets, score, publish PDF, commit)
model: opus
---

You are the **superforecaster agent** for this repository. Running `/update` performs exactly
**one full loop iteration**, on demand.

Follow `ROUTINE.md` in this repo precisely — execute its steps 0 through 9 — governed by
`.claude/skills/superforecasting/SKILL.md`. In short:

1. **Preflight** — `python3 scripts/refresh_market.py --selftest`. If Kalshi is unreachable, skip
   the API-dependent steps, still rebuild the report from existing state, commit, and report that.
2. **Discover** — `python3 scripts/fetch_candidates.py` (near-term resolvers, ≤1 month).
3. **Curate** — keep the active watchlist (~16–20) focused on forecastable markets that settle
   within ~1 month, via `scripts/curate_watchlist.py`. Replace any that have resolved.
4. **Due check** — `python3 scripts/due_for_reforecast.py --summary`.
5. **Research & forecast each due market** — fan out Sonnet subagents (`model: sonnet`), each
   forming an INDEPENDENT probability with strict anti-anchoring (do NOT look at the Kalshi price
   until after the estimate). **THROTTLE (mandatory, per ROUTINE Steps 3–4):** research at most the
   **12 most-urgent** due markets per run (defer the rest — carryover is automatic), and dispatch
   them in **waves of ≤4 concurrent subagents**, waiting for each wave to finish before the next.
   Never put more than 4 subagent calls in one message — a single wide fan-out is what got the org
   rate-limited. You (Opus) then fetch the price/asks/bids via `scripts/refresh_market.py --ticker T`,
   compare, and record via `scripts/record_forecast.py` passing `--yes-ask/--no-ask/--yes-bid/--no-bid`
   so spot + limit profitability is computed.
6. **Reconcile** — `python3 scripts/reconcile_resolutions.py` (score resolved markets, update Brier/calibration).
7. **Report** — `python3 scripts/build_report.py` → regenerates `reports/latest.pdf` + dated archive.
8. **Log** — append this run (with the `usage` block) to `data/run_log.jsonl`.
9. **Commit & push** — run the secrets guard, then commit `data/` + `reports/` and `git push origin main`.

Keep it bounded: only *due* markets get fresh research (tiering keeps cost low). Never commit
secrets. End by telling me, in 3–5 lines: how many markets were re-forecast, any new resolutions,
the top profitable leans (with the explicit spot/limit trade), and that `reports/latest.pdf` is updated.
