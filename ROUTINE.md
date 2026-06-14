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
- **Parallelize (project directive):** independent research fans out to **Opus** subagents,
  one per due market; you (Opus) orchestrate and synthesize. Pass `model: opus` explicitly.
- **This loop is an EXPERIMENT (project directive).** Every forecast is produced by a registered
  *strategy arm* (`lib/strategies.py`) and tagged with its `strategy_id`; every resolution scores
  that arm on **both Brier skill and realized profit**. We do not assume which forecasting topology
  is best — the scoreboard discovers it. See SKILL §0.
- **Retrieval runs LOCAL, judgment runs on Claude (cost unlock).** Raw web pages are condensed by a
  local open-weight model (`lib/local_llm.py`) into compact *quoted* evidence notes; raw pages never
  enter Claude context. If the local endpoint is down, fall back to an Opus retrieval agent — the
  pipeline never hard-breaks.
- **Never grade your own work.** Post-mortems run through an **adversarial panel** (blind local
  Critic → Claude Defender → Claude Judge), not single-agent self-judging. See Step 6b.
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
python3 -c "from lib import local_llm, config; print('local_llm', 'UP' if (config.local_llm_enabled() and local_llm.ping()) else 'DOWN')"
```
If `refresh_market` prints `kalshi UNREACHABLE`, skip the research/resolution steps that need the
API, jump to Step 7 (build report from existing state), Step 8, Step 9, and exit. Otherwise continue.

**Local-LLM mode:** if the healthcheck prints `local_llm UP`, retrieval condensation runs locally
(free) and the per-run cap can **relax** (see Step 3) — the cap existed to throttle frontier fan-out,
which local retrieval removes. If it prints `local_llm DOWN`, the pipeline still works via the
**Opus fallback** retrieval/critic agents, and the per-run cap stays at its default (frontier
fan-out is back in play). Note which mode you're in for the Step 8 log.

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
python3 scripts/due_for_reforecast.py --limit 12 --summary
```
Returns the JSON list of tickers needing a fresh forecast this run (new markets, markets past
their tier cadence, or `--event-driven` overrides). The list is **sorted by `days_to_close`
ascending — most urgent first.** Only these get researched — this is what keeps 3×/day affordable.

**Per-run cap (throttle — prevents rate-limit shutdowns):** research **at most the N most-urgent
due markets this run** (N defaults to 12; `/update N` overrides it). `--limit N` enforces this in
the script — it keeps the first N of the sorted list and reports how many were **deferred**. Carryover
is automatic: a market you skip stays past its cadence and reappears (more urgent) on the next run,
so nothing is lost. Always keep `--event-driven` overrides inside the kept set even if it means
dropping a less-urgent cadence market. Note in the Step 8 log how many were deferred
(`reforecast_deferred`) — the `--summary` line reports this directly.

**Cap relaxation when `local_llm UP` (Step 0):** the cap throttled *frontier* fan-out — the part
that got the org rate-limited. With local retrieval doing the heavy web work, that pressure is gone,
so when an explicit `/update N` was **not** given you may raise the default (e.g. `--limit 20`) up to
the full due list. The **wave rule below still binds**: only the lighter Claude forecasting/judge
calls fan out, and never more than 4 concurrently. On the Opus-fallback path keep the default 12.

## Step 4 — Research & forecast each due market (RETRIEVE local → assign arm → FAN OUT in WAVES)

### Step 4a — Retrieval tier (LOCAL evidence notes, free)
For each due market, gather web evidence and condense it to compact, *quoted* structured notes
**before** any frontier forecasting — so raw pages never enter Claude context.
- **`local_llm UP`:** run your web searches/fetches, then pipe the raw results through
  `lib.local_llm.extract_evidence(question, raw_results, as_of=...)` to get `EvidenceNotes`
  (claims with verbatim source quotes, base rates, key uncertainties). Hand the **notes**, not the
  raw pages, to the forecasters.
- **`local_llm DOWN`:** fall back to an Opus retrieval agent that returns the same notes shape.
The notes carry source quotes so the forecaster can verify rather than blindly trust a small model.

### Step 4b — Assign the strategy arm (the experiment)
For each due market, pick its arm:
```
python3 -c "from lib import strategies, scoring, store; \
  s=strategies.select_strategy('<TICKER>', scoring.compute_calibration(store.load_resolutions().resolved).by_strategy); \
  print(s.id, s.n_forecasters, s.aggregation, s.crowd_adjust_weight, s.redteam)"
```
The arm config tells you **how many independent forecasters to spawn** (`n_forecasters`), how to
**combine** them (`aggregation` — use `strategies.combine(probs, arm, market_price)`), whether to
**crowd-adjust** toward the market, and whether to run a **red-team** pass before committing.
Selection is round-robin while cold, epsilon-greedy on `by_strategy` skill+ROI once arms have a
record. Carry the chosen `strategy_id` to Step 5.

### Step 4c — Forecast (FAN OUT to Opus, IN WAVES)
**Dispatch Opus subagents in bounded waves of at most 4 at a time** — never all at once. This is
the concurrency throttle that keeps the run under Anthropic's burst limits (a single 20-wide
fan-out is what got the org rate-limited). For an arm with `n_forecasters > 1`, the independent
forecasters for a market also count toward the ≤4 concurrent budget.

> **Wave protocol (MANDATORY):** issue at most **4** `Task`/subagent calls in one message, then
> **wait for all 4 to return** before issuing the next batch of ≤4. Do **not** start a new wave
> until the previous wave's agents have all completed. Never put more than 4 subagent calls in a
> single message.

Give each worker the market title + ticker + resolution rules + the **evidence notes from 4a** and
this instruction:

> Follow `.claude/skills/superforecasting/SKILL.md` steps 1–4 and 6. Form an INDEPENDENT
> probability from the supplied evidence notes: decompose the question, establish base rates /
> reference classes, weigh ≥3 independent perspectives, run a pre-mortem. **Do NOT look up the
> Kalshi market price.** Return a structured result: `my_probability` (granular), `my_confidence`
> (low/med/high), `rationale_summary` (1–3 sentences), `key_drivers`, `reference_classes`,
> `research_refs` (URLs).

For a multi-forecaster arm, combine the returned probabilities with
`strategies.combine(probs, arm, market_price=None)` (anti-anchoring: do not pass the price yet).
If the arm sets `redteam`, run one adversarial red-team pass on the combined estimate before
committing. Then **you (Opus), per market**, execute SKILL step 5 (anti-anchoring):
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
  --trigger <bootstrap|scheduled|near_close|event_driven> --strategy-id <ARM from Step 4b> \
  --rationale "<1-3 sentences>" --drivers "a,b,c" --reference-classes "x,y" --refs "url1,url2" \
  --title "<market title>" --category <politics|culture|statements|economy> --close-time <ISO8601Z>
```
The script computes edge + drift and is idempotent for same-day/same-trigger re-runs. `--strategy-id`
tags the forecast with the arm from Step 4b so the resolution scoreboard can attribute Brier + ROI
to it (defaults to `strategies.DEFAULT_STRATEGY` if omitted).

## Step 6 — Reconcile resolutions & score
```
python3 scripts/reconcile_resolutions.py
```
Detects markets that resolved on Kalshi, records outcomes + Brier (yours vs market), tags each
with a **sub-category** (`lib/taxonomy.py`), frees watchlist slots, and recomputes
`data/calibration.json` (cumulative + per-category + per-sub-category skill in `by_segment`).

## Step 6b — Adversarial post-mortem panel (only when markets newly resolved this run)
If Step 6 recorded any **new** resolutions, learn from them — but **never grade your own work**.
Run the three-role panel via `scripts/postmortem.py` for each newly-resolved market:
1. Rebuild the analysis DB: `python3 scripts/build_db.py` (SQLite mirror of the JSON; gitignored).
2. **Critic (blind, rubric-anchored, different model family).**
   ```
   python3 scripts/postmortem.py critic --ticker <TICKER>
   ```
   - `status: ok` → the **local Qwen** critic scored the fixed rubric (`config.POSTMORTEM_RUBRIC`),
     blind to forecaster identity. Use its `rubric_scores` + `summary` + `biggest_miss`.
   - `status: fallback` → local model down: spawn a **Opus critic sub-agent** with the printed
     `packet` and the same blind rubric instruction; that becomes the critique (`critic_model:
     opus-fallback`). The critic must judge **reasoning quality, not the outcome** (a good forecast
     can still lose).
3. **Defender (Claude sub-agent):** argue what the forecast got *right* and whether the outcome was
   genuinely unforeseeable at forecast time. **Judge (Claude, Opus tier):** read critic + defender,
   issue a per-rubric verdict and a single actionable `lesson` with a short `pattern` tag; note where
   critic and defender disagreed (that gap is the signal).
4. Persist the adversarial lesson:
   ```
   python3 scripts/postmortem.py record --ticker <TICKER> --pattern <short-tag> \
     --critic-model <local-model-tag|opus-fallback> --rubric-scores '<critic rubric_scores JSON>' \
     --judge-verdict "<judge ruling>" --disagreement "<where critic/defender diverged>" \
     --right ".." --wrong ".." --lesson "<actionable takeaway>"
   ```
5. **SKILL revision is human-gated and pattern-gated.** Never edit the SKILL on one outcome. Check
   eligible patterns:
   ```
   python3 scripts/postmortem.py patterns
   ```
   Only a `pattern_tag` that has recurred across ≥ `config.SKILL_REVISION_MIN_PATTERN` (3) resolved
   markets is eligible, and even then the edit is a **proposal for the user** — surface it in your
   summary; do not auto-edit `.claude/skills/superforecasting/SKILL.md`. A single resolution is one
   noisy data point — same discipline as forecasting (SKILL §4a).

## Step 6c — Autonomous learning pass (the system tunes its own decision policy)
Always run this (it's cheap and self-gating):
```
python3 scripts/learn_policy.py --apply
```
The learner reads the resolved track record, finds which **entry criteria** actually predicted
profit (via the counterfactual conditioning engine), and proposes nudges to the **learnable policy**
(`data/policy.json`) — the "when do I take a position" knobs (EV floor, market-fade gate, conviction
thresholds, confidence gating, **adversarial-veto authority**). `--apply` lands **only** proposals
that clear every anti-overfit guardrail (`min_n`, `max_step`); everything else stays
**INSUFFICIENT_DATA / HUMAN_GATE** and is written to `data/policy_proposals.json` for review, never
auto-applied. Every applied change is appended to `policy.changelog` (auditable + reversible). At
small `n` this correctly applies nothing — by design; the loop earns authority as the record grows.
Surface any `AUTO_OK` (applied) or `HUMAN_GATE` (awaiting you) proposals in the Step "summary".

This is the closed loop: the **adversary** (Step 5) challenges each decision *before* commit; this
pass rewrites the rules that *define* a good decision *after* outcomes land. The forecast that counts
is the one at the **locked entry** (`Position`), not the latest re-forecast — performance is scored
against that committed point (entry-lock, option A).

## Step 7 — Build the report
```
python3 scripts/build_report.py
```
Regenerates `reports/latest.pdf` and archives a dated copy. Always run this, even on a degraded
run. The report shows: **Performance Over Time** (cumulative Brier/skill trend + per-category /
per-sub-category skill tables), **Profit & Loss (realized)** (P&L, ROI, win rate on resolved
YES/NO leans — calibration is not profit), and the **Strategy Scoreboard** (each arm's Brier skill
*and* realized ROI — the "which topology wins" view). A provisional caveat shows below ~30
resolutions.

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
