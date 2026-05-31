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
Secrets `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY` are provided as environment variables (public
market data needs no auth, so the run works even if they're absent). Record the wall-clock start
so you can report `duration_s` at the end.

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
cap of 20 from `candidates.json`, choosing markets that (a) are genuinely soft, (b) are diverse
across the four categories (politics / culture / statements / economy — don't let politics crowd
out everything), and (c) are ones where you expect to be able to form an analyzable edge. To add
a market, you will create its first forecast in Step 5 (the `--title/--category/--close-time`
args seed the record); also reflect membership by editing `data/watchlist.json` **via a script
or `store` helper**, not by guessing fields. Keep active count ≤ 20.

> **First run (bootstrap):** the watchlist is empty. Fill it with your best ~20 (or fewer if you
> set `FIRST_RUN_MAX`). Every market will be "new" and therefore due.

## Step 3 — Determine what's due
```
python3 scripts/due_for_reforecast.py --summary
```
Returns the JSON list of tickers needing a fresh forecast this run (new markets, markets past
their tier cadence, or `--event-driven` overrides). Only these get researched — this is what
keeps 3×/day affordable.

## Step 4 — Research & forecast each due market (FAN OUT to Sonnet)
For the due list, **dispatch one Sonnet subagent per market in parallel** (Opus orchestrates).
Give each worker the market title + ticker + resolution rules and this instruction:

> Follow `.claude/skills/superforecasting/SKILL.md` steps 1–4 and 6. Form an INDEPENDENT
> probability: decompose the question, establish base rates / reference classes, gather ≥3
> independent perspectives via web research, run a pre-mortem. **Do NOT look up the Kalshi market
> price.** Return a structured result: `my_probability` (granular), `my_confidence`
> (low/med/high), `rationale_summary` (1–3 sentences), `key_drivers`, `reference_classes`,
> `research_refs` (URLs).

Then **you (Opus), per returned draft**, execute SKILL step 5 (anti-anchoring):
```
python3 scripts/refresh_market.py --ticker <TICKER>      # NOW look at the price
```
Compare the worker's independent probability to the market implied probability. Decide whether
the market knows something the research missed; adjust only for a stated reason (do not average
toward it). Set `--lean` / `--conviction` per the SKILL thresholds.

## Step 5 — Record each forecast
For every due market:
```
python3 scripts/record_forecast.py --ticker <TICKER> --prob <P> --confidence <low|medium|high> \
  --market-implied <MP> --market-price-cents <C> --lean <YES|NO|NONE> --conviction <low|medium|high> \
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
