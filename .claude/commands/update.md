---
description: Run one superforecaster loop iteration (research due markets, score, publish PDF, commit)
model: opus
---

You are the **superforecaster agent** for this repository. Running `/update` performs exactly
**one full loop iteration**, on demand.

**Argument — market cap (optional):** `$ARGUMENTS`
If a positive integer N was passed (e.g. `/update 5`), research at most **N** markets this run,
chosen as the N closing soonest (prioritized by end date). If no argument is given, fall back to
the default throttle of **12** — but when the **local-LLM healthcheck is UP** (Step 0), the cap may
relax toward the full due list (e.g. 20), since the throttle existed to limit *frontier* fan-out and
local retrieval removes that pressure (ROUTINE Step 3). Either way, pass the chosen number to
`due_for_reforecast.py` via `--limit` (see Step 4) so the truncation is deterministic; the deferred
markets carry over to the next run automatically. N is still subject to the wave rule below (≤4
concurrent subagents).

Follow `ROUTINE.md` in this repo precisely — execute its steps 0 through 9 — governed by
`.claude/skills/superforecasting/SKILL.md`. In short:

1. **Preflight** — `python3 scripts/refresh_market.py --selftest` + local-LLM healthcheck. If Kalshi
   is unreachable, skip the API-dependent steps, still rebuild the report, commit, and report that.
   Note whether local-LLM is UP (local retrieval + relaxed cap) or DOWN (Sonnet fallback, default cap).
2. **Discover** — `python3 scripts/fetch_candidates.py` (near-term resolvers, ≤1 month).
3. **Curate** — keep the active watchlist (~16–20) focused on forecastable markets that settle
   within ~1 month, via `scripts/curate_watchlist.py`. Replace any that have resolved.
4. **Due check** — `python3 scripts/due_for_reforecast.py --limit N --summary`, where `N` is the
   market cap from the argument above (default 12, relaxed when local-LLM is UP). The script keeps
   the `N` markets closing soonest and reports how many were deferred.
5. **Research & forecast each due market** — for each due market: (4a) gather web evidence and
   condense it to **quoted evidence notes** via the LOCAL model (`lib.local_llm.extract_evidence`;
   Sonnet-agent fallback when down) so raw pages never hit Claude context; (4b) assign a **strategy
   arm** (`lib.strategies.select_strategy`) that sets how many forecasters to spawn and how to
   combine them; (4c) fan out Sonnet forecasters (`model: sonnet`) on the **notes**, each forming an
   INDEPENDENT probability with strict anti-anchoring (do NOT look at the Kalshi price until after
   the estimate). **THROTTLE (mandatory):** dispatch in **waves of ≤4 concurrent subagents**, waiting
   for each wave to finish before the next; never put more than 4 subagent calls in one message — a
   single wide fan-out is what got the org rate-limited. You (Opus) then fetch price/asks/bids via
   `scripts/refresh_market.py --ticker T`, compare, and record via `scripts/record_forecast.py`
   passing `--strategy-id` + `--yes-ask/--no-ask/--yes-bid/--no-bid` so the arm is tagged and spot +
   limit profitability is computed.
6. **Reconcile** — `python3 scripts/reconcile_resolutions.py` (score resolved markets; record Brier
   **and realized P&L/ROI**; update calibration + scoreboards).
   **6b. Adversarial post-mortem** (only on new resolutions) — `scripts/postmortem.py`: blind local
   Critic → Claude Defender → Claude Judge → recorded lesson. Never self-judge; SKILL revision stays
   pattern-gated **and** human-gated (`postmortem.py patterns`).
7. **Report** — `python3 scripts/build_report.py` → `reports/latest.pdf` + dated archive (now incl.
   Profit & Loss and the Strategy Scoreboard).
8. **Log** — append this run (with the `usage` block) to `data/run_log.jsonl`.
9. **Commit & push** — run the secrets guard, then commit `data/` + `reports/` and `git push origin main`.

Keep it bounded: only *due* markets get fresh research (tiering keeps cost low). Never commit
secrets. End by telling me, in 3–5 lines: how many markets were re-forecast, any new resolutions,
the top profitable leans (with the explicit spot/limit trade), which strategy arm is leading on the
scoreboard, and that `reports/latest.pdf` is updated.
