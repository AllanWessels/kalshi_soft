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
- **Model routing (2026-06-29 — Qwen does ALL cognition; Opus is pure plumbing + Defender/Judge).**
  The local open-weight model (`lib/local_llm.py` + `lib/retrieval.py`, Qwen) does ALL of:
  (1) **web RETRIEVAL** — Qwen *drives its own browser* via `lib.retrieval.gather_evidence`: it issues
  the `web_search`/`wiki_lookup`/`web_fetch` tool-calls itself (keyless backends: Google News RSS +
  Wikipedia), reaches **>5 disparate sources**, and condenses to quoted `EvidenceNotes`; (2)
  **forecasting** (`forecast_ensemble` — see below); (3) **adversarial analysis** (the in-loop
  `challenge` gate, Step 5, and the blind post-mortem `critique`, Step 6b); and (4) **autonomous SKILL
  revision** (`revise_skill`, Step 6b). **No Anthropic model forms a forecast or runs a web search.**
  **Opus's ONLY jobs:** (a) running the deterministic scripts, recording, committing (plumbing), and
  (b) the post-mortem **Defender** and **Judge** roles (different family from the Qwen critic). You
  spawn NO model subagents for retrieval or forecasting. **Hard dependency:** if `local_llm` is DOWN,
  the loop CANNOT retrieve or forecast — skip the research/forecast steps, still rebuild the report +
  commit, and report the outage (Step 0).
- **Confidence is EARNED by ensemble, not asserted.** Because a single small model is weakly
  calibrated, every market is forecast by `lib.local_llm.forecast_ensemble` — **N=5 independent Qwen
  passes at temperature>0**, fused by median. Tight agreement → `high` confidence, wide spread → `low`
  (a thin evidence base of <5 sources also caps confidence at `medium`). This is what lets a lean
  clear the `min_confidence_for_lean` floor without weakening any risk gate.
- **Source breadth (HARD RULE):** every market must draw on **> 5 disparate sources** (distinct
  orgs/types, primary-first). Consult `data/source_registry.json` for per-domain sources + non-partisan
  reliability ratings; cross-check any single-source claim before it enters a forecast.
- **This loop is an EXPERIMENT (project directive).** Every forecast is produced by a registered
  *strategy arm* (`lib/strategies.py`, now the local-Qwen `LQ*` arms) and tagged with its
  `strategy_id`; every resolution scores that arm on **both Brier skill and realized profit**. The
  scoreboard discovers which local topology (ensemble size, crowd-adjust, red-team) wins. See SKILL §0.
- **Adversarial caveat (known trade-off):** with Qwen now forecasting, the `challenge` gate and
  post-mortem `critic` are Qwen-reviewing-Qwen (same family), weaker than the old cross-family check.
  Mitigate by running the challenge at a different temperature/framing; the EV floor, ≤5pt fade cap,
  and URAN ceiling remain the binding risk gates.
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

**Local-LLM mode (now a HARD dependency):** Qwen does retrieval, forecasting, AND adversarial work,
so `local_llm UP` is REQUIRED to forecast. If it prints `local_llm UP`, proceed — everything runs
locally and free, and the per-run cap is bounded only by wall-clock (ensemble passes are sequential
Qwen calls on the home GPU), not by token cost. If it prints `local_llm DOWN`, the loop **cannot
forecast**: skip Steps 2–6, jump to Step 7 (rebuild report from existing state), Step 8, Step 9, and
report the outage. There is no Anthropic-model fallback — that is the point of the all-Qwen routing.
Note the mode in the Step 8 log.

## Step 1 — Discover
```
python3 scripts/fetch_candidates.py
```
Writes the top-150 soft-market pool to `data/candidates.json`. Read it.

## Step 2 — Curate the watchlist (judgment, cap `config.WATCHLIST_CAP` = 30)
Read `data/watchlist.json`. Drop entries already `resolved`/`delisted`. Then top up toward the
cap (`config.WATCHLIST_CAP`, currently **30**) from `candidates.json`, ranking candidates
**primarily by FORECASTABILITY** (per the SKILL's selection-priority rule): pick markets with a real
evidential basis where research yields a defensible, well-calibrated number; do NOT add near-random
trivia (weekly-chart specifics, single-broadcast word/mention markets, coin-flips) even when liquid.
**Widen-the-funnel directive (2026-06-19):** within the forecastable set, deliberately favor *softer,
less efficiently-priced* markets — ones priced in the uncertain middle (~0.15–0.85) and in
less-nationally-covered corners (down-ballot/foreign nominations, the cheap side of a race you already
have a view on, RT-score/box-office/award/chart markets with real review tracking, topical
event-driven statements) — because that is where research can actually beat the price and produce a
lean. Efficiently-priced macro/headline markets are worth tracking but mostly resolve to NONE. Accuracy
still beats diversity, but the cap was raised from 20→30 precisely to hold a wider funnel of soft
forecastable markets. To add a market, you will create its first forecast in Step 5 (the
`--title/--category/--close-time` args seed the record); also reflect membership by editing
`data/watchlist.json` **via a script or `store` helper**, not by guessing fields. Keep active count ≤
the cap. `curate_watchlist.py` is
**self-cleaning**: every run auto-purges dead entries (any non-active status), so the file holds only
`active` markets and a `--drop` removes the entry outright. Resolved-market history is preserved in
`resolutions.json`, not the watchlist — so nothing is lost.

> **First run (bootstrap):** the watchlist is empty. Fill it with your best ~30 (up to the cap; fewer if you
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

**Cap is now wall-clock-bound, not token-bound.** Forecasting is free local Qwen, so there is no
Anthropic burst limit to respect and no wave rule. The only throttle is how many sequential Qwen
ensemble calls finish in a reasonable run (each market = ~5 `forecast` passes). `/update N` sets the
target; default 12 when no N is given. Deferred markets carry over automatically.

## Step 4 — Research & forecast each due market (RETRIEVE → assign arm → ENSEMBLE-FORECAST)

> **STANDARD PATH while the shadow A/B is active (user directive 2026-06-29):** run
> `python3 scripts/ab_forecast.py --limit N`. It executes Steps 4a–5 for the whole due set:
> shared Qwen retrieval, then forecasts EVERY market with **both Qwen and Mistral** on the same
> evidence, records the assigned arm officially (adversarial gate + entry-lock), and persists both
> blind forecasts to `data/ab_shadow.jsonl`. `scripts/ab_score.py` resolves the Qwen-vs-Mistral
> Brier head-to-head as those markets settle; the dual pass **auto-disables** once the Mistral arm
> reaches `SHADOW_AB_TARGET_RESOLUTIONS` (25). The manual 4a–4c below documents what that script does.
> **Profitability is the governing objective — see `PROFITABILITY_PLAN.md`** (concentrate on
> measured positive-skill segments; we currently lose to the market on Brier and must fix that).

### Step 4a — Retrieval tier (Qwen drives its own browser, free)
For each due market, **Qwen does the retrieval itself** — the orchestrator does NOT run WebSearch/
WebFetch. Call `lib.retrieval.gather_evidence(question, as_of=..., min_sources=5)`: the local model
issues its own `web_search`/`wiki_lookup`/`web_fetch` tool-calls against keyless backends (Google News
RSS for current events, Wikipedia for base rates), reaches **> 5 disparate sources** (distinct
publishers — the returned `n_sources` proves it), reads the most informative pages, and returns
`EvidenceNotes` (claims with verbatim quotes, base rates, key uncertainties, `sources_consulted`,
`n_sources`). Only the condensed notes flow onward — raw pages never enter the forecaster context.
Pass `notes["n_sources"]` to `forecast_ensemble` (thin base <5 caps confidence at `medium`). If
`gather_evidence` raises (model unreachable), the run cannot forecast — defer per Step 0.
The orchestrator owns the web tool-calls only as a last-resort fallback for an offline local model
(`lib.local_llm.extract_evidence` over raw text); in normal operation it never browses.

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

### Step 4c — Forecast (local ENSEMBLE — no subagents)
**No model subagents. Forecasting is `lib.local_llm.forecast_ensemble`** — N independent local passes
(N = the arm's `n_forecasters`, default 5) at temperature>0, fused by median, with confidence earned
from agreement (tight spread → `high`, wide → `low`; <5 sources caps at `medium`). **Pass the arm's
model** via `strategies.resolve_forecaster_model(arm)` so the assigned arm decides WHICH local model
forecasts (Qwen3-14B by default, or Mistral-Small-24B for the `LQM5-mistral24` arm) — that is how the
scoreboard compares models head-to-head. Thinking is suppressed centrally (`reasoning_effort="none"`)
so every pass returns clean JSON; truncated passes auto-retry once. The forecaster sees the evidence
notes ONLY, never the Kalshi price (anti-anchoring). Per market:

```
python3 -c "
from lib import local_llm, strategies, scoring, store, json as _j  # arm already chosen in 4b
notes = ...   # EvidenceNotes from 4a (pass via a temp file or inline)
mdl = strategies.resolve_forecaster_model(ARM)   # None -> default Qwen; Mistral tag for LQM5
r = local_llm.forecast_ensemble(QUESTION, notes, n=ARM_N, as_of=AS_OF, n_sources=NUM_SOURCES, model=mdl)
print(r['my_probability'], r['my_confidence'], r['stdev'], r['rationale_summary'])"
```

In practice the orchestrator: writes the notes to a scratch JSON, calls `forecast_ensemble` (n =
arm `n_forecasters`), then for a multi-pass arm the ensemble already aggregates; if the arm sets
`crowd_adjust_weight`, apply `strategies.combine(r['probs'], arm, market_price)` AFTER the price is
revealed; if it sets `redteam`, run one `local_llm.challenge`-style pass on the fused estimate before
committing. Then execute SKILL step 5 (anti-anchoring):
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
   - `status: skipped` → local model down: **SKIP and DEFER this market's post-mortem** — do NOT
     spawn a same-family (Opus) critic, since Opus grading an Opus forecast is self-judging, exactly
     what this panel exists to prevent. Leave the market un-reviewed; it gets the adversarial
     post-mortem on a later run when `local_llm` is UP. Note the deferral in the Step 8 log.
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
5. **SKILL revision is AUTONOMOUS (2026-06-29 — no human gate).** After recording lessons, fold them
   into the method automatically:
   ```
   python3 scripts/postmortem.py revise-skill
   ```
   Qwen (`local_llm.revise_skill`) re-drafts the **auto-maintained heuristics block** (between the
   `AUTO-HEURISTICS` markers) of `.claude/skills/superforecasting/SKILL.md` from the resolved track
   record — a bounded (≤12 one-line heuristics), git-committed, reversible edit that **refines** the
   method and can never override the anti-anchoring protocol or a risk gate. It runs as often as the
   record warrants. `postmortem.py patterns` is now only an advisory recurrence view, not a gate.
   Surface the revision (heuristics changed + rationale) in your summary.

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

## Step 6d — Trade recommendations (route the structural edge into TRACKED positions)
The LLM forecaster loses to the price, so its leans are correctly ~zero — but that must not mean
**zero trades**, or there is no profit. The history-learned market-calibration edge (`lib/atlas`)
**beat the market out-of-sample** (and walk-forward, month-by-month) and nets positive after fees
in the mid-liquidity band; this step routes THAT edge into explicit, scored position
recommendations. **Workstreams A+B (PLAN_FOR_OPUS.md) rebuilt this path — always run all three:**
```
python3 scripts/score_recommendations.py       # score prior open recs (CONSERVATIVE = official)
python3 scripts/screen_universe.py --max 12    # B1: screen the ENTIRE open exchange
python3 scripts/scan_coherence.py              # B4: dutch-NO arbs + incoherence flags
```
`screen_universe.py` walks every open market (not just the 150-candidate funnel): corrected cell
(granular first) → **walk-forward-POSITIVE** cell only (`data/history/walkforward.json`; fails
closed) → mid open-interest band → **+EV after fee AND half-spread** → A4 kill-switch check →
**orderbook fill evidence** snapshotted per rec → append to `data/trade_recommendations.jsonl`
(idempotent per ticker+day). `scan_coherence.py` adds probability-axiom trades (guaranteed-floor
dutch-NO baskets; dutch-YES and bracket monotonicity report-only). `score_recommendations.py`
scores both P&L columns — the **conservative fills-evidenced column is the official record**; the
A4 verification bar and per-cell kill switches read only from it. (`recommend_trades.py` remains
as the legacy candidates-pool screen; superseded by `screen_universe.py`.)
Surface in the Step "summary": the new basket (explicit BUY-NO/YES + entry limit + fillable_now),
the OFFICIAL scoreboard (verified cohort, conservative), and verification-bar progress. Size small
& equal — the basket is a correlated longshot-fade, one thematic position. **After any re-harvest
or map refit, rerun `fit_market_calibration.py` + `walkforward_validate.py` (the screen fails
closed without a fresh walk-forward record).**

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
