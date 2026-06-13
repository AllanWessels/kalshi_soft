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
  Brier/calibration math, profit/P&L math, PDF rendering, git ops. It never decides a probability.
- **Opus = the forecaster/orchestrator**: each run it follows `.claude/skills/superforecasting/SKILL.md`,
  fanning out **Sonnet** research workers (one per market) and synthesizing the final calls.
- **A local open-weight model = free retrieval + adversarial critic** (`lib/local_llm.py`): condenses
  raw web pages into compact *quoted* evidence notes (so raw pages never enter Claude context) and
  acts as the blind, different-family Critic in the post-mortem panel. Degrades to a Sonnet agent
  when the local endpoint is down.
- **The loop is an experiment**: every forecast is produced by a registered **strategy arm**
  (`lib/strategies.py`) and scored at resolution on **both Brier skill and realized profit**, so the
  scoreboard *discovers* which forecasting topology wins rather than assuming one.
- **Learning is adversarial, not self-graded**: resolved markets go through a Critic → Defender →
  Judge panel (`scripts/postmortem.py`); SKILL edits are pattern-gated **and** human-gated.
- **The repo is the memory**: all state lives in `data/` and is committed each run, so the
  cloud Routine (which clones fresh each time) carries its track record forward.

## Layout
```
lib/        schemas, config, taxonomy, kalshi_client, store, scoring, gitops,
            profit, strategies, local_llm, report                            (deterministic + LLM client)
scripts/    fetch_candidates, refresh_market, record_forecast, due_for_reforecast,
            reconcile_resolutions, build_report, postmortem, record_lesson   (CLI entrypoints)
data/       watchlist.json, forecasts/<TICKER>.json, resolutions.json,
            calibration.json, candidates.json, lessons.json, run_log.jsonl   (committed state)
reports/    latest.pdf + archive/report_YYYY-MM-DD.pdf                       (the deliverable)
ROUTINE.md  the per-run runbook the scheduled agent executes
.claude/skills/superforecasting/SKILL.md   the forecasting methodology
```

### The `/update` pipeline (per run)
```
0 preflight + local-LLM healthcheck   1 discover/curate/due
2 RETRIEVE (local Qwen → quoted evidence notes; Sonnet fallback if down)
3 FORECAST (assign strategy arm → N Sonnet forecasters on the notes → combine; anti-anchoring)
4 RECORD (--strategy-id + entry prices)   5 RECONCILE (Brier + realized P&L/ROI)
6 ADVERSARIAL POST-MORTEM (blind local Critic → Claude Defender → Judge → lesson)
7 REPORT (Performance, Profit & Loss, Strategy Scoreboard) + commit
```

## Running it
### Local (manual loop / development)
```bash
pip install -r requirements.txt
python3 scripts/refresh_market.py --selftest      # check Kalshi reachability (no key needed)
python3 scripts/fetch_candidates.py               # discover the soft-market pool
# ...then follow ROUTINE.md steps 2–9 (curate, research, record, reconcile, report, commit)
python3 scripts/build_report.py                   # regenerate reports/latest.pdf
```
**No secrets required** — the loop uses only Kalshi's public market-data endpoints (see Security).

### On-demand from your phone (recommended) — the `/update` command
Open **Claude Code on the web** (claude.ai/code) on your phone, start a session on this repo,
and type **`/update`** — it runs one full loop and pushes an updated `reports/latest.pdf`, which
you can open in the GitHub mobile app. One-time setup in the session's cloud environment:
1. Add this repository.
2. Network access **Full** (or Custom + allowlist `external-api.kalshi.com`).
3. Enable **unrestricted branch pushes** (so `/update` can commit to `main`).

No Kalshi key needs to be set in the cloud environment (public data only). For hands-free
automation you can later point a scheduled Routine at the same `ROUTINE.md`, but it isn't required.

### Local open-weight model (optional but recommended) — retrieval + adversarial critic
`/update` runs on the local RTX 5080 box (directly or via phone Remote Control — Claude Code web
sandboxes are GPU-less), so the open-weight model is reachable at `localhost` with no tunnel. It is
**operator-installed, not auto-installed**:
```bash
# one-command start (Ollama, works on Blackwell w/ CUDA 12.8):
ollama pull qwen3:14b-instruct-q4_K_M
ollama serve            # serves an OpenAI-compatible API on http://localhost:11434/v1
```
Config lives in `lib/config.py` and is env-overridable:
`LOCAL_LLM_BASE_URL` (default `http://localhost:11434/v1`), `LOCAL_LLM_MODEL`,
`LOCAL_LLM_ENABLED=0` to force the Sonnet-fallback path. Verify with:
```bash
python3 -c "from lib import local_llm; print('UP' if local_llm.ping() else 'DOWN')"
python3 -m lib.local_llm        # offline self-test (JSON parsing + fallback, no server needed)
```
When the endpoint is **down or disabled**, the pipeline still works end-to-end via Sonnet retrieval
and critic agents — the local model is a cost optimization, not a hard dependency. Migrate to
vLLM-from-source only if concurrency demands it (Blackwell needs torch cu128,
`VLLM_FLASH_ATTN_VERSION=2`, `TORCH_CUDA_ARCH_LIST=12.0`).

## Security
The loop is **read-only and needs no credentials** — it calls only Kalshi's public market-data
endpoints (verified: a no-header request to `/trade-api/v2/markets` returns 200). The client
implements no order/portfolio endpoints, so it cannot trade. Any optional Kalshi key (for future
account features) lives only in a gitignored local `.env`, never in the repo or cloud environment,
and `gitops.assert_no_secrets_staged()` aborts any commit containing a private key or `.env`.

## Reading the output
Open `reports/latest.pdf`. Each market block names the **title + ticker** (so you can find it on
Kalshi), your probability + confidence, the market's implied probability, the signed edge, your
paper lean, and a drift chart. Once markets resolve, the calibration section shows your Brier
score vs the market's and a reliability curve — the empirical verdict on the hypothesis.

The **Performance Over Time** section is the trend view: cumulative Brier (yours vs market) and
running skill as the resolved-market sample grows, plus per-category and per-**sub-category** skill
tables (where your edge is real and where it isn't). Every market is auto-tagged with a
sub-category by `lib/taxonomy.py` (e.g. `politics / us-governor-primary`, `economy / fed-rates`),
also queryable via `data/forecasts.db` (`resolutions.subcategory`). Skill numbers are labelled
**provisional below ~30 resolutions** — read the trend, not the point estimate.

Two further sections close the loop between *calibration* and *money*:
- **Profit & Loss (realized)** — every resolved YES/NO paper lean scored on realized P&L, ROI, and
  win rate (entry ask, after Kalshi fees). A high win rate with negative ROI = calibration without
  profit, the gap Brier alone can't see.
- **Strategy Scoreboard** — each strategy arm's Brier skill **and** realized ROI side by side: the
  empirical answer to *which forecasting topology actually works*, which the harness keeps testing.
