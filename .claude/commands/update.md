---
description: Run one superforecaster loop iteration (research due markets, score, publish PDF, commit)
model: opus
---

You are the **superforecaster agent** for this repository. Running `/update` performs exactly
**one full loop iteration**, on demand.

**Argument — market cap (optional):** `$ARGUMENTS`
If a positive integer N was passed (e.g. `/update 5`), research at most **N** markets this run,
chosen as the N closing soonest (prioritized by end date). If no argument is given, fall back to
the default throttle of **12**. **Model routing (2026-06-23): Qwen (local) does EVERYTHING —
retrieval, FORECASTING (`forecast_ensemble`, 5 passes), and adversarial gating. No Anthropic model
forms a forecast and NO model subagents are spawned; the orchestrator session is plumbing only.**
There is therefore **no wave rule / no burst throttle** — the cap is wall-clock-bound (forecasting
is free sequential Qwen calls on the home GPU). Pass the chosen N to `due_for_reforecast.py` via
`--limit` (see Step 4); deferred markets carry over automatically. Requires `local_llm UP` — if the
local model is down the loop cannot forecast (no fallback).

Follow `ROUTINE.md` in this repo precisely — execute its steps 0 through 9 — governed by
`.claude/skills/superforecasting/SKILL.md`. In short:

1. **Preflight** — `python3 scripts/refresh_market.py --selftest` + local-LLM healthcheck. If Kalshi
   is unreachable, skip the API-dependent steps, still rebuild the report, commit, and report that.
   Note whether local-LLM is UP (local retrieval + relaxed cap) or DOWN (Opus-agent fallback, default cap).
2. **Discover** — `python3 scripts/fetch_candidates.py` (near-term resolvers, ≤1 month).
3. **Curate** — keep the active watchlist (~16–20) focused on forecastable markets that settle
   within ~1 month, via `scripts/curate_watchlist.py`. Replace any that have resolved.
4. **Due check** — `python3 scripts/due_for_reforecast.py --limit N --summary`, where `N` is the
   market cap from the argument above (default 12, relaxed when local-LLM is UP). The script keeps
   the `N` markets closing soonest and reports how many were deferred.
5. **Research & forecast each due market** — for each due market: (4a) **you (orchestrator)** run the
   web searches/fetches inline, then condense them to **quoted evidence notes** via **Qwen**
   (`lib.local_llm.extract_evidence`; Opus fallback only when local is down) so raw pages never hit the
   forecaster's context — retrieval is Qwen's job, never route forecasting through it; (4b) assign a
   **strategy arm** (`lib.strategies.select_strategy`) that sets how many forecasters to spawn and how
   to combine them; (4c) fan out **Opus** forecasters on the **notes** — **forecasting is always Opus**,
   never the local model, never Sonnet — each forming an INDEPENDENT probability with strict
   anti-anchoring (do NOT look at the Kalshi price until after the estimate). **THROTTLE:** dispatch in **waves of ≤4 concurrent
   subagents**. You (Opus) then fetch price/asks/bids via `scripts/refresh_market.py --ticker T`,
   compare, and record via `scripts/record_forecast.py` passing `--strategy-id` +
   `--yes-ask/--no-ask/--yes-bid/--no-bid`. **The adversarial gate is automatic + unskippable:**
   `record_forecast` runs `lib.local_llm.challenge` on every actionable lean (independent
   cross-family review — Opus alone is not trusted to greenlight its own bet); a **veto** downgrades
   the lean to NONE. The first surviving lean **locks an immutable `Position`** (entry-lock) — that
   committed entry is what performance is scored against, not later re-forecasts.
6. **Reconcile** — `python3 scripts/reconcile_resolutions.py` (score resolved markets against the
   **locked entry**; record Brier **and realized + counterfactual P&L/ROI**; update calibration +
   scoreboards).
   **6b. Adversarial post-mortem** (only on new resolutions) — `scripts/postmortem.py`: blind local
   Critic → Claude Defender → Claude Judge → recorded lesson. Never self-judge; SKILL revision stays
   pattern-gated **and** human-gated (`postmortem.py patterns`).
   **6c. Autonomous learning pass** — `python3 scripts/learn_policy.py --apply`: reads the resolved
   record, proposes nudges to the learnable decision policy (`data/policy.json` — the "when do I take
   a position" knobs), and applies **only** what clears the anti-overfit guardrails (min-n, max-step);
   the rest stay INSUFFICIENT_DATA/HUMAN_GATE in `data/policy_proposals.json`. Surface any AUTO_OK or
   HUMAN_GATE proposals in the summary.
7. **Report** — `python3 scripts/build_report.py` → `reports/latest.pdf` + dated archive (incl.
   Profit & Loss, Strategy Scoreboard, and the **Autonomous Learning** policy/proposals section).
8. **Log** — append this run (with the `usage` block) to `data/run_log.jsonl`.
9. **Commit & push** — run the secrets guard, then commit `data/` + `reports/` and `git push origin main`.

Keep it bounded: only *due* markets get fresh research (tiering keeps cost low). Never commit
secrets. End by telling me, in 3–5 lines: how many markets were re-forecast, any new resolutions,
the top profitable leans (with the explicit spot/limit trade), which strategy arm is leading on the
scoreboard, any policy proposals the learner surfaced, and that `reports/latest.pdf` is updated.
