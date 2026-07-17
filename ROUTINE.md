# ROUTINE — per-run runbook for the superforecaster loop

You are the **superforecaster agent**. This file is your instruction set for one scheduled
run. The Routine fires 3×/day (12:00 / 21:00 / 03:00 America/Los_Angeles). The repo has been
cloned fresh and `data/` holds your memory from prior runs. Read and follow
`.claude/skills/superforecasting/SKILL.md` — it governs every judgment you make here.
**PLAN_FOR_OPUS.md is the governing build plan; profitability is the governing objective.**

**Loop shape (Workstream E, 2026-07-17).** The loop has TWO halves with different guarantees:

* **The MONEY PATH (Steps 1–2)** — reconcile, score, screen the exchange, scan coherence,
  run the paper broker. Pure API + arithmetic, **no LLM anywhere**: it runs on EVERY cycle,
  including when the local model is down, and it fails LOUDLY (non-zero exit → run-log
  errors), never silently. This is the half with measured edge; it is never skipped.
* **The FORECASTER R&D PASS (Steps 3–4)** — retrieval, diverse-ensemble forecasting,
  post-mortems, learners. Requires `local_llm UP`; skipped cleanly when it is down. This half
  earns deviation rights per segment; it takes positions only through measured skill (α).

**Golden rules:**
- Deterministic plumbing is in `scripts/` and `lib/`. You make the *judgments*; the scripts do
  the math, storage, and rendering. Always go through the scripts — never hand-edit `data/`.
- **Anti-anchoring:** never look at a market's Kalshi price until you have written your own
  probability first (enforced by the order of steps below). The diverse arm's atlas-price
  member enters at COMBINE time only, never in any prompt.
- **Model routing.** Qwen/Mistral (local) do ALL forecaster cognition — retrieval
  (`lib.retrieval.gather_evidence`, the model drives its own browser), forecasting
  (`forecast_ensemble`, arm-driven topology incl. the LD5-diverse panel), the adversarial
  `challenge` gate, the blind post-mortem `critique`, and autonomous SKILL revision. **No
  Anthropic model forms a forecast or runs a web search.** The orchestrator (Opus/Fable) is
  plumbing + the post-mortem Defender/Judge roles only. The money path needs no model at all.
- **Confidence is EARNED by ensemble, not asserted** — agreement across genuinely diverse
  members (models × personas), tight spread → `high`; <5 disparate sources caps at `medium`.
- **Source breadth (HARD RULE):** >5 disparate sources per forecast; consult
  `data/source_registry.json`; cross-check single-source claims.
- **This loop is an EXPERIMENT.** Every forecast carries its strategy arm (`lib/strategies`);
  every resolution scores the arm on Brier AND realized profit; the scoreboard decides.
- **Never grade your own work** — post-mortems run through the adversarial panel (Step 4).
- **Risk gates are non-negotiable:** EV floor, ≤5pt fade cap, `hard_gap_ceiling`, leans never
  oppose the modal forecast, adversarial gate unskippable, entry-lock scoring. PAPER ONLY:
  no live trading until the A4 verification bar passes AND the user signs off
  (`docs/EXECUTION.md`; user constraint 2026-07-17).
- Degrade gracefully: if a step fails, log it and continue so every run still produces the
  report + commit. Never leave the repo half-committed.

## Setup (once per run)
```
pip install -r requirements.txt        # cryptography, requests, matplotlib, reportlab
```
No secrets required (public market-data endpoints). Record the wall-clock start for
`duration_s`.

## Step 0 — Preflight
```
python3 scripts/refresh_market.py --selftest
python3 -c "from lib import local_llm, config; print('local_llm', 'UP' if (config.local_llm_enabled() and local_llm.ping()) else 'DOWN')"
```
Degradation matrix:
- **Kalshi UNREACHABLE** → skip Steps 1–4, jump to Step 5 (report from existing state),
  Step 6, Step 7. Report the outage.
- **local_llm DOWN** → run Steps 1–2 (the money path — it needs no LLM), skip Steps 3–4
  (cannot retrieve/forecast/critique; no Anthropic fallback, that is the point), then
  Steps 5–7. Note the mode in the run log.
- **Both UP** → run everything.

## Step 1 — Reconcile resolutions & score
```
python3 scripts/reconcile_resolutions.py
```
Detects settled markets, scores Brier (yours vs market) against the **locked entry**
(entry-lock: the committed Position, not the latest re-forecast), tags sub-categories
(`lib/taxonomy.py`), frees watchlist slots, recomputes `data/calibration.json` (+ per-segment
skill + profit scoreboards).

## Step 2 — THE MONEY PATH (always runs; fails loudly)
```
python3 scripts/money_path.py --max-recs 12
```
One command, four stages, exchange-truth in / sized paper orders out:
1. `score_recommendations` — settle the signal ledger. **Conservative (fills-evidenced)
   column = the OFFICIAL record**; legacy cohort is provisional only. Prints the A4
   verification-bar progress.
2. `screen_universe` — screen the ENTIRE open exchange: corrected cell (granular first) →
   **walk-forward-POSITIVE** cells only (fails closed without `walkforward.json`) → mid-OI
   band → +EV after fee AND half-spread → A4 kill switches → orderbook fill evidence per rec.
3. `scan_coherence` — probability-axiom trades: dutch-NO baskets auto-logged (payoff floor
   k−1), dutch-YES + T-threshold monotonicity report-only.
4. `paper_broker` — settle fills → maintain resting orders against the live book → place new
   sized orders (D1 rails: quarter-Kelly, 2%/market, 10%/cell, 5%/event-family, 50% total,
   −15% drawdown halt). Its no-fill rate and settled ROI are the execution-truth numbers.
A non-zero exit = degraded money path → record which stage failed in run-log `errors`.
**Maintenance cadence:** after any re-harvest or monthly, refit + revalidate:
`python3 scripts/fit_market_calibration.py && python3 scripts/walkforward_validate.py`
(the screen fails closed on a stale/missing walk-forward record — that is intended).

## Step 3 — Forecaster R&D pass (only when local_llm UP; ≤10 markets)

### 3a — Discover & curate (triage)
```
python3 scripts/fetch_candidates.py
python3 scripts/curate_watchlist.py --list     # then --add/--drop per the rules below
```
Cap `config.WATCHLIST_CAP` (30). Rank candidates by FORECASTABILITY, and hold ONLY the
Goldilocks zone (Workstream C1): (a) down-ballot / under-covered politics (the measured
positive-skill segment — grow its n), (b) mid-priced (~0.15–0.85) thin/mid-OI culture with a
real evidential basis, (c) markets the atlas flags in a corrected cell, (d) **sports
human-decision markets** (C1b: award votes / personnel / rulings / participation —
`config.is_sports_decision`; game outcomes stay blocked). `curate_watchlist.py` mechanically
REFUSES `config.TRIAGE_EXCLUDED_SUBCATS` (fed-rates, inflation-cpi, jobs-unemployment,
gdp-growth, us-president — measured negative-skill efficient segments). Do not fight the gate.

### 3b — Due check (cap ≤10/run)
```
python3 scripts/due_for_reforecast.py --limit 10 --summary
```
Sorted by `days_to_close` ascending; keep `--event-driven` overrides inside the kept set;
deferrals carry over automatically (report `reforecast_deferred`).

### 3c — Research & forecast (STANDARD PATH)
```
python3 scripts/ab_forecast.py --limit 10
```
Per market it executes: **retrieval** (Qwen drives its own browser via
`lib.retrieval.gather_evidence`, >5 disparate sources, quoted EvidenceNotes — the orchestrator
never browses); **arm assignment** (`strategies.select_strategy` — default `LD5-diverse`: Qwen
standard + Qwen outside-view + Mistral standard + Mistral inside-view, all blind to price,
plus the atlas-calibrated price appended at COMBINE time only); **error-memory injection**
(past misses in-context); **learning-policy blend** (recalibrate + shrink-to-market by
measured segment skill α — no skill ⇒ track the price ⇒ no position, correctly); then price
reveal + `record_forecast.py` (fee-aware EV sets lean/conviction; the **adversarial
`challenge` gate is automatic + unskippable** — a veto downgrades to NONE; the first surviving
lean locks an immutable Position). The dual-model shadow pass is OFF (C2,
`config.SHADOW_AB_ENABLED=False`); `ab_score.py` still scores persisted pairs as they resolve.
Manual fallback details (4a–4c of the old routine) live in git history if the script is ever
unusable; the contract is unchanged: independent estimate before price, granular probability,
epistemic confidence stated separately, refs cited.

## Step 4 — Post-mortems & learning (local_llm UP for the critic)

### 4a — Adversarial post-mortem panel (on new resolutions; never self-judge)
```
python3 scripts/build_db.py
python3 scripts/postmortem.py critic --ticker <TICKER>     # blind Qwen critic (JSON-enforced)
# then Opus/Fable Defender + Judge, then:
python3 scripts/postmortem.py record --ticker <TICKER> --pattern <tag> ... --lesson "..."
python3 scripts/postmortem.py revise-skill                  # autonomous, bounded, reversible
```
`status: skipped` (model down / malformed after repair) → DEFER that market's post-mortem to a
later run; do NOT spawn a same-family critic. Check for previously deferred post-mortems and
complete them when the critic is healthy. Surface revised heuristics in the summary.

### 4b — Autonomous learning pass (always cheap + self-gating; runs even if 4a deferred)
```
python3 scripts/learn_policy.py --apply
```
Applies only proposals clearing the anti-overfit guardrails (min-n, max-step); the rest stay
INSUFFICIENT_DATA / HUMAN_GATE in `data/policy_proposals.json`. Surface AUTO_OK / HUMAN_GATE.

## Step 5 — Build the report
```
python3 scripts/build_report.py
```
Always runs, even degraded. The **money page leads**: Structural Edge (official conservative
scoreboard, verification-bar progress, tail stress, per-cell record, paper-broker
equity/no-fill/ROI, open recs), then performance/P&L/scoreboards/learning/per-market sections.

## Step 6 — Log the run (with usage)
Append one `schemas.RunLogEntry` line to `data/run_log.jsonl` via `store.append_run_log`:
`run_id`, `status`, `discovered`, `watchlist_size`, `reforecast`, `resolved_new`, `errors`
(INCLUDING any failed money-path stage), `pdf`, and the `usage` block (`markets_researched`,
`duration_s`, mode notes: local_llm UP/DOWN, money path complete/degraded).

## Step 7 — Commit & push (direct to main)
```
git add data reports
python3 -c "from lib import gitops; gitops.assert_no_secrets_staged()"   # safety guard
git commit -m "forecast run <UTC-date>: <N> reforecast, <M> resolved; money path <ok|degraded>"
git push origin main
```
Skip the commit if nothing changed. **Never** stage `.env` or key material (the guard aborts).

## Summary you leave behind
Lead with the MONEY: the official (conservative) scoreboard + verification-bar progress, the
new basket (explicit BUY side + entry limit + fillable_now), paper-broker equity/no-fill —
then reforecasts, resolutions, post-mortems/heuristics, learner proposals, and
`reports/latest.pdf`. One loop iteration, honestly measured.
