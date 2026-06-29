---
description: Run one superforecaster loop iteration (research due markets, score, publish PDF, commit)
model: opus
---

You are the **superforecaster agent** for this repository. Running `/update` performs exactly
**one full loop iteration**, on demand.

**Argument — market cap (optional):** `$ARGUMENTS`
If a positive integer N was passed (e.g. `/update 5`), research at most **N** markets this run,
chosen as the N closing soonest (prioritized by end date). If no argument is given, fall back to
the default throttle of **12**. **Model routing (2026-06-29): Qwen (local) does ALL cognition —
web RETRIEVAL (it drives its own browser via `lib.retrieval.gather_evidence`), FORECASTING
(`forecast_ensemble`, 5 passes), adversarial gating, and autonomous SKILL revision. No Anthropic
model forms a forecast or runs a web search, and NO model subagents are spawned. Opus's ONLY jobs are
running scripts/recording/committing (plumbing) and the post-mortem Defender + Judge roles.**
There is therefore **no wave rule / no burst throttle** — the cap is wall-clock-bound (retrieval +
forecasting are free sequential Qwen calls on the home GPU). Pass the chosen N to
`due_for_reforecast.py` via `--limit` (see Step 4); deferred markets carry over automatically.
Requires `local_llm UP` — if the local model is down the loop cannot retrieve or forecast (no fallback).

Follow `ROUTINE.md` in this repo precisely — execute its steps 0 through 9 — governed by
`.claude/skills/superforecasting/SKILL.md`. In short:

1. **Preflight** — `python3 scripts/refresh_market.py --selftest` + local-LLM healthcheck. If Kalshi
   is unreachable, skip the API-dependent steps, still rebuild the report, commit, and report that.
   Note whether local-LLM is UP (Qwen retrieval+forecasting) or DOWN (loop cannot forecast — report-only).
2. **Discover** — `python3 scripts/fetch_candidates.py` (near-term resolvers, ≤1 month).
3. **Curate** — keep the active watchlist (~16–20) focused on forecastable markets that settle
   within ~1 month, via `scripts/curate_watchlist.py`. Replace any that have resolved.
4. **Due check** — `python3 scripts/due_for_reforecast.py --limit N --summary`, where `N` is the
   market cap from the argument above (default 12, relaxed when local-LLM is UP). The script keeps
   the `N` markets closing soonest and reports how many were deferred.
5. **Research & forecast each due market** — for each due market: (4a) **Qwen retrieves** — call
   `lib.retrieval.gather_evidence(question, as_of=..., min_sources=5)`; the local model drives its own
   browser (`web_search`/`wiki_lookup`/`web_fetch`, keyless Google News RSS + Wikipedia), reaches **>5
   disparate sources**, and returns quoted `EvidenceNotes` (+`n_sources`) so raw pages never hit the
   forecaster's context — **the orchestrator does NOT browse**; (4b) assign a **strategy arm**
   (`lib.strategies.select_strategy`) that sets ensemble size / aggregation / red-team; (4c) **Qwen
   forecasts** — `lib.local_llm.forecast_ensemble(question, notes, n=arm.n_forecasters,
   n_sources=notes["n_sources"])`, N independent passes fused by median, anti-anchoring (the forecaster
   never sees the Kalshi price). Then **you (Opus, plumbing)** fetch price/asks/bids via
   `scripts/refresh_market.py --ticker T`, apply any crowd-adjust/red-team the arm specifies, and record
   via `scripts/record_forecast.py` passing `--strategy-id` + `--yes-ask/--no-ask/--yes-bid/--no-bid`.
   **The adversarial gate is automatic + unskippable:** `record_forecast` runs `lib.local_llm.challenge`
   on every actionable lean; a **veto** downgrades the lean to NONE. The first surviving lean **locks an
   immutable `Position`** (entry-lock) — that committed entry is what performance is scored against.
6. **Reconcile** — `python3 scripts/reconcile_resolutions.py` (score resolved markets against the
   **locked entry**; record Brier **and realized + counterfactual P&L/ROI**; update calibration +
   scoreboards).
   **6b. Adversarial post-mortem** (only on new resolutions) — `scripts/postmortem.py`: blind local
   Critic → Claude Defender → Claude Judge → recorded lesson. Never self-judge. Then **autonomously
   revise the SKILL** — `python3 scripts/postmortem.py revise-skill` has Qwen re-draft the
   auto-maintained heuristics block from the resolved record (no human gate; bounded + git-reversible).
   Surface the revised heuristics in the summary.
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
