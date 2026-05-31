# kalshi_soft — Superforecaster Agent for Kalshi "Soft" Markets

An autonomous forecasting agent that tests a hypothesis: **Opus, equipped with a codified
superforecasting methodology (Tetlock / Good Judgment Project), can produce calibrated
probability forecasts that beat the market on Kalshi markets driven by human behavior** —
politics, culture, public statements, behavioral economics/policy — as opposed to stochastic
markets (crypto/sports/weather) where research can't out-model the price.

The agent runs unattended 3×/day, maintains a focused ~20-market watchlist, re-forecasts on a
tiered cadence, tracks probability/confidence drift and edge-vs-market, scores itself with Brier
scores + calibration curves as markets resolve, and publishes a PDF report each run.

> **Paper only.** The Kalshi API key is **read-only**; the client implements no order endpoints.
> Forecasts are a track record, not executed trades.

## How it works
- **Python = deterministic plumbing** (`lib/`, `scripts/`): Kalshi fetch, state storage,
  Brier/calibration math, PDF rendering, git ops. It never decides a probability.
- **Opus = the forecaster**: each run it follows `.claude/skills/superforecasting/SKILL.md`,
  fanning out **Sonnet** research workers (one per market) and synthesizing the final calls.
- **The repo is the memory**: all state lives in `data/` and is committed each run, so the
  cloud Routine (which clones fresh each time) carries its track record forward.

## Layout
```
lib/        schemas, config, kalshi_client, store, scoring, report, gitops   (deterministic)
scripts/    fetch_candidates, refresh_market, record_forecast,
            due_for_reforecast, reconcile_resolutions, build_report          (CLI entrypoints)
data/       watchlist.json, forecasts/<TICKER>.json, resolutions.json,
            calibration.json, candidates.json, run_log.jsonl                 (committed state)
reports/    latest.pdf + archive/report_YYYY-MM-DD.pdf                       (the deliverable)
ROUTINE.md  the per-run runbook the scheduled agent executes
.claude/skills/superforecasting/SKILL.md   the forecasting methodology
```

## Running it
### Local (manual loop / development)
```bash
pip install -r requirements.txt
cp .env.example .env        # fill in KALSHI_KEY_ID + KALSHI_PRIVATE_KEY (gitignored)
python3 scripts/refresh_market.py --selftest      # check API reachability
python3 scripts/fetch_candidates.py               # discover the soft-market pool
# ...then follow ROUTINE.md steps 2–9 (curate, research, record, reconcile, report, commit)
python3 scripts/build_report.py                   # regenerate reports/latest.pdf
```

### Autonomous (cloud Routine — runs with your laptop off)
Configure a Routine at https://claude.ai/code:
1. Add this repository.
2. Set environment variables `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY` (read-only key).
3. Set network access to **Full** (or Custom + allowlist `external-api.kalshi.com`).
4. Enable **unrestricted branch pushes** (so it can commit to `main`).
5. Schedule **3 runs/day at 12:00 / 21:00 / 03:00 `America/Los_Angeles`**.
6. Point the Routine prompt at `ROUTINE.md`.

## Security
Secrets live only in `.env` (gitignored) locally or in the Routine's environment config — never
in the repo. `gitops.assert_no_secrets_staged()` aborts any commit containing a private key or
`.env`. If a key was ever exposed, rotate it in the Kalshi dashboard.

## Reading the output
Open `reports/latest.pdf`. Each market block names the **title + ticker** (so you can find it on
Kalshi), your probability + confidence, the market's implied probability, the signed edge, your
paper lean, and a drift chart. Once markets resolve, the calibration section shows your Brier
score vs the market's and a reliability curve — the empirical verdict on the hypothesis.
