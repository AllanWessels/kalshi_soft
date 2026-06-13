# ROUTINE — per-run runbook for the superforecaster loop

You are the **superforecaster agent**. This file is your instruction set for one scheduled
run. The Routine fires 3×/day (12:00 / 21:00 / 03:00 America/Los_Angeles). The repo has been
cloned fresh and `data/` holds your memory from prior runs. Read and follow
`.claude/skills/superforecasting/SKILL.md` — it governs every judgment you make here.

**Golden rules:**
- Deterministic plumbing is in `scripts/` and `lib/`. You make the *judgments*; the scripts do
  the math, storage, and rendering. Always go through the scripts — never hand-edit `data/`.
- **Anti-anchoring:** never look at a market's Kalshi price until you have written your own
  probability first (enforced by the order of steps below).
- **Parallelize (project directive):** independent research fans out to **Sonnet** subagents,
  one per due market; you (Opus) orchestrate and synthesize. Pass `model: sonnet` explicitly.
- Degrade gracefully: if a step fails, log it and continue to the report + commit steps so every
  run still produces output. Never leave the repo half-committed.

## Setup (once per run)
```
pip install -r requirements.txt        # cryptography, requests, matplotlib, reportlab
```
No secrets are required: the loop uses only Kalshi's public market-data endpoints, which need no
auth (the client signs only if a key happens to be present). Record the wall-clock start so you
can report `duration_s` at the end.

## Step 0 — Preflight
```
python3 scripts/refresh_market.py --selftest
```
If it prints `kalshi UNREACHABLE`, skip the research/resolution steps that need the API, jump to
Step 7 (build report from existing state), Step 8, Step 9, and exit. Otherwise continue.

## Step 1 — Discover
```
python3 scripts/fetch_candidates.py
```
Writes the top-150 soft-market pool to `data/candidates.json`. Read it.

## Step 2 — Curate the watchlist (judgment, cap 20)
Read `data/watchlist.json`. Drop entries already `resolved`/`delisted`. Then top up toward the
cap of 20 from `candidates.json`, ranking candidates **primarily by FORECASTABILITY** (per the
SKILL's selection-priority rule): pick markets with a real evidential basis where research yields
a defensible, well-calibrated number; do NOT add near-random trivia (weekly-chart specifics,
single-broadcast word/mention markets, coin-flips) even when liquid. Within the forecastable set,
favor genuine softness and some category spread, but accuracy beats diversity — it's fine if the
list skews to politics/economy, since that's where forecastable human-behavior volume is. To add
a market, you will create its first forecast in Step 5 (the `--title/--category/--close-time`
args seed the record); also reflect membership by editing `data/watchlist.json` **via a script
or `store` helper**, not by guessing fields. Keep active count ≤ 20. `curate_watchlist.py` is
**self-cleaning**: every run auto-purges dead entries (any non-active status), so the file holds only
`active` markets and a `--drop` removes the entry outright. Resolved-market history is preserved in
`resolutions.json`, not the watchlist — so nothing is lost.

> **First run (bootstrap):** the watchlist is empty. Fill it with your best ~20 (or fewer if you
> set `FIRST_RUN_MAX`). Every market will be "new" and therefore due.

## Step 3 — Determine what's due (then apply the per-run cap)
```
python3 scripts/due_for_reforecast.py --summary
```
Returns the JSON list of tickers needing a fresh forecast this run (new markets, markets past
their tier cadence, or `--event-driven` overrides). The list is **sorted by `days_to_close`
ascending — most urgent first.** Only these get researched — this is what keeps 3×/day affordable.

**Per-run cap (throttle — prevents rate-limit shutdowns):** research **at most the 12 most-urgent
due markets this run.** Take the first 12 of the sorted list; if more than 12 are due, **defer the
rest** — do not research them now. Carryover is automatic: a market you skip stays past its cadence
and reappears (more urgent) on the next run, so nothing is lost. Always keep `--event-driven`
overrides inside the kept set even if it means dropping a less-urgent cadence market. Note in the
Step 8 log how many were deferred (`reforecast_deferred`).

## Step 4 — Research & forecast each due market (FAN OUT to Sonnet, IN WAVES)
For the capped due list (≤12 from Step 3), **dispatch Sonnet subagents in bounded waves of at
most 4 at a time** — never all at once. This is the concurrency throttle that keeps the run under
Anthropic's burst limits (a single 20-wide fan-out is what got the org rate-limited).

> **Wave protocol (MANDATORY):** issue at most **4** `Task`/subagent calls in one message, then
> **wait for all 4 to return** before issuing the next batch of ≤4. For 12 markets that is 3 waves.
> Do **not** start a new wave until the previous wave's agents have all completed. Never put more
> than 4 subagent calls in a single message.

Give each worker the market title + ticker + resolution rules and this instruction:

> Follow `.claude/skills/superforecasting/SKILL.md` steps 1–4 and 6. Form an INDEPENDENT
> probability: decompose the question, establish base rates / reference classes, gather ≥3
> independent perspectives via web research, run a pre-mortem. **Do NOT look up the Kalshi market
> price.** Return a structured result: `my_probability` (granular), `my_confidence`
> (low/med/high), `rationale_summary` (1–3 sentences), `key_drivers`, `reference_classes`,
> `research_refs` (URLs).

Then **you (Opus), per returned draft**, execute SKILL step 5 (anti-anchoring):
```
python3 scripts/refresh_market.py --ticker <TICKER>      # NOW look at the price + asks + fees
```
This returns `yes_ask`, `no_ask`, `fee_yes`, `fee_no`. Compare the worker's independent
probability to the market implied probability; adjust only for a stated reason (do not average
toward it). You do NOT hand-pick the lean — pass `--yes-ask`/`--no-ask` to `record_forecast.py`
and it computes **fee-aware profitability**, setting `lean`/`conviction` from net EV per contract
(lean=NONE unless the best side clears `MIN_PROFITABLE_EV`). Expect mostly NONE on liquid markets.

## Step 5 — Record each forecast
For every due market:
```
python3 scripts/record_forecast.py --ticker <TICKER> --prob <P> --confidence <low|medium|high> \
  --market-implied <MP> --market-price-cents <C> --yes-ask <YA> --no-ask <NA> \
  --trigger <bootstrap|scheduled|near_close|event_driven> \
  --rationale "<1-3 sentences>" --drivers "a,b,c" --reference-classes "x,y" --refs "url1,url2" \
  --title "<market title>" --category <politics|culture|statements|economy> --close-time <ISO8601Z>
```
The script computes edge + drift and is idempotent for same-day/same-trigger re-runs.

## Step 6 — Reconcile resolutions & score
```
python3 scripts/reconcile_resolutions.py
```
Detects markets that resolved on Kalshi, records outcomes + Brier (yours vs market), frees
watchlist slots, and recomputes `data/calibration.json`.

## Step 6b — Post-mortem & learning (only when markets newly resolved this run)
If Step 6 recorded any **new** resolutions, learn from them:
1. Rebuild the analysis DB: `python3 scripts/build_db.py` (SQLite mirror of the JSON; gitignored).
2. For each newly-resolved market, run a **post-mortem** (your judgment): pull its forecast
   trajectory (`python3 -c "from lib import db; print(db.forecast_trajectory('<TICKER>'))"`),
   compare it to the outcome and to the market (`brier_mine` vs `brier_market`). Ask: did I have
   the right side? Was I over/under-confident? Did I update well over time, or chase noise? What
   *reliable* signal did I under/over-weight? Record it: `python3 scripts/record_lesson.py --id
   <resolved_at>-<TICKER> --source resolution --ticker <T> --category <c> --outcome <0|1>
   --final-prob .. --final-market .. --brier-mine .. --brier-market .. --beat-market <true|false>
   --right ".." --wrong ".." --lesson ".." --pattern <short-tag>`.
3. **Update the SKILL only on a PATTERN, never on one outcome.** `record_lesson.py` reports how
   many times a `pattern_tag` has recurred across resolutions; only when it reaches
   `config.SKILL_REVISION_MIN_PATTERN` (3) should you edit `.claude/skills/superforecasting/SKILL.md`
   to encode the correction, then re-run `record_lesson.py ... --applied-to-skill` for those. A
   single resolution is one noisy data point — the same discipline as forecasting (SKILL §4a).

## Step 7 — Build the report
```
python3 scripts/build_report.py
```
Regenerates `reports/latest.pdf` and archives a dated copy. Always run this, even on a degraded
run.

## Step 8 — Log the run (with usage)
Append one line to `data/run_log.jsonl` capturing this run: `run_id` (UTC timestamp), `status`,
`discovered`, `watchlist_size`, `reforecast`, `resolved_new`, `errors`, `pdf`, and a `usage`
block with the cost proxies you can observe — `web_searches`, `web_fetches`, `tool_calls`,
`markets_researched`, `duration_s`, and a best-effort `est_tokens`. (Use the `store.append_run_log`
helper / a `schemas.RunLogEntry`.) These feed the report's Cost & Usage section;
claude.ai/settings/usage is authoritative for actual tokens/$.

## Step 9 — Commit & push (direct to main)
```
git add data reports
python3 -c "from lib import gitops; gitops.assert_no_secrets_staged()"   # safety guard
git commit -m "forecast run <UTC-date>: <N> reforecast, <M> resolved"
git push origin main
```
Skip the commit if nothing changed. **Never** stage `.env` or any key material (the guard will
abort the commit if you try).

## Summary you leave behind
A clean PDF the user reads, a committed `data/` reflecting today's forecasts and drift, and one
`run_log.jsonl` line. That's one loop iteration.
