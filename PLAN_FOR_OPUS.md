# PLAN FOR OPUS — Rework kalshi_soft into a verified, profitable system

**Written by Fable 5, 2026-07-17, at the user's direction.** This is a build plan, not code.
Opus: read this whole document before touching anything. Execute the workstreams in order.
Every workstream has acceptance criteria and kill criteria. Do not skip the verification
workstream to get to the exciting parts — the user's binding constraint is:

> **No live trading until profitability is VERIFIED. No bankroll committed yet.** (User, 2026-07-17)

---

## Part I — Honest review of the project as it stands

### I.1 What the evidence says (all numbers from `data/` as of 2026-07-17)

**Engine 1 — the LLM forecaster: loses to the market, everywhere that matters.**
- n=47 resolved: Brier mine 0.134 vs market 0.098 → **skill −0.036**. Beat the price on 16/47 (34%).
- Shadow A/B (n=15 of 25 target): Qwen skill **−0.326**, Mistral **−0.315** vs market Brier 0.067.
  Both local models are catastrophically worse than the price on the shadow set.
- Historical Opus arms (`S0`, `S2`): skill −0.123 and −0.165. **A bigger model did not fix it.**
- Only positive segment: `politics/us-governor-primary` +0.074 at **n=3** — statistically nothing yet.
- Counterfactual P&L: −6.7% ROI overall; only the 0–5pt-gap band is positive (+1.4%, n=26).
- The gates work correctly: ~all leans are NONE because the forecaster has no edge to act on.

**Engine 2 — the structural/atlas edge: promising, NOT verified.**
- Backtest (leakage-free, 36,408 settled markets, 70/30 split, n=10,861 OOS): calibration map beats
  raw market Brier 0.0293→0.0257; **+9.6% ROI after fees in the mid-OI (500–5k) band**; edge dies in
  deep liquidity (−1.7%). This is the defensible number (fills assumed at implied price = upper bound).
- Live forward ledger (`data/trade_recommendations.jsonl`): 13/13 wins, +$2.84 on $10.16 (+27.9% ROI),
  beat-market Brier 13/13. **But**: (a) fills are ASSUMED at `entry_limit`, never evidenced against an
  orderbook; (b) 12/13 are one correlated theme (statements-market longshot fades); (c) zero tail
  events survived — one YES hit costs ~0.7–0.9/contract and erases 3–8 wins; (d) n=13; (e) **none of
  this appears in the PDF report the user actually reads** — `lib/report.py` has no rec-ledger section.

**Verdict:** the project's one plausible path to profit is the structural edge — a classic
favorite-longshot-bias harvest — but its live verification is currently too weak to bet on, its
scoring is optimistic, its scale is tiny (~$0.22/rec average profit), and it is invisible in the
report. The LLM forecaster is a research program, not a P&L engine, and currently consumes ~95% of
each run's wall-clock producing (correctly) zero positions.

### I.2 What the project got RIGHT (keep all of this)
- **Anti-anchoring protocol** (independent probability before seeing the price) — this is what makes
  the skill measurement meaningful at all.
- **Entry-lock scoring** (performance judged at the committed entry, not the latest re-forecast).
- **Two-axis scoring** (Brier skill AND realized/counterfactual P&L) — calibration is not profit.
- **Adversarial panel post-mortems** (never self-judge) + guardrailed policy learner (min-n, max-step)
  + autonomous bounded SKILL revision. The learning machinery is sound.
- **Leakage discipline** in the atlas (train/test split, shrunk Platt, fit on past → applied to future).
- **Precommitted kill criteria** ("profitable or it dies"). Tetlock: keep score, no excuses.
- **Leans never oppose the modal forecast; hard_gap_ceiling; EV floor after fees.** The risk gates
  encode real lessons (URAN) and must survive every rework below.

### I.3 What is WRONG (what this plan fixes)
1. **Effort inversion.** ~30 min/run of Qwen retrieval+forecasting on markets we measurably cannot
   beat (Fed, CPI, headline politics); seconds on the engine with actual edge. Tetlock's first
   commandment is *triage* — "concentrate on questions where hard work is likely to pay off."
   We do the opposite.
2. **The paper record is not verification-grade.** Assumed fills, no orderbook evidence, no tail
   stress, narrow theme concentration, and the scoreboard isn't in the report.
3. **The "ensemble" is not a crowd.** 5 temperature-samples of one 14B model is a homogeneous
   committee with near-zero diversity. Page's diversity-prediction theorem: collective error =
   average individual error − diversity. We have the first term without the second.
4. **The screen is starved.** `recommend_trades.py` screens only `data/candidates.json` (top-150 soft
   pool) — the structural edge needs no research and could screen the entire open-market universe.
5. **The map is coarse.** Cells are (category × 7 price bands × 3 OI tiers); no time-to-close axis
   (mispricing concentrates far from resolution and decays into the close); OI tiers too wide.
6. **Known operational faults:** local critic intermittently emits malformed JSON (5 of 7 post-mortems
   deferred on 2026-07-17); the A/B dual pass doubles forecast wall-clock and both models are losing.

---

## Part II — The methodology this plan is grounded in

Opus: internalize these; they are the design rationale for every workstream. This section is the
"become an expert" distillation the user asked for.

### II.1 Tetlock / Good Judgment Project — what actually produced superforecasting
- **Triage (Goldilocks zone).** Work only questions where effort pays: not clock-like (efficiently
  priced — the market already knows) and not cloud-like (irreducible noise — announcer-say markets
  for a *forecaster*; note the structural engine can still harvest *pricing bias* there without
  forecasting anything). → Workstream C cuts the watchlist to the Goldilocks zone.
- **Fermi decomposition, outside view first.** Base rates before case details. The atlas is exactly
  this, industrialized: 36k settled markets = a reference-class library; "how often do markets priced
  8¢ in culture resolve YES" IS the outside view. → Workstream B scales it.
- **Granularity & calibration win.** Superforecasters' gains came from calibration and granular
  updating, not bold insight. A Platt map that turns 8¢ into a calibrated 3¢ is granularity that the
  crowd's fee/attention structure won't arbitrage away.
- **Update often, in small increments; underreact to noise, don't miss real signal.** Already encoded
  in SKILL §4a. Keep.
- **Teams + diversity + extremizing beat prediction markets.** GJP's aggregate beat intrade-style
  markets by ~15–30% Brier *only* with: genuinely diverse forecasters, weighted pooling, and
  **extremization** of the pooled logit (the crowd underweights shared information). A homogeneous
  Qwen ensemble has none of these properties. → Workstream C rebuilds the ensemble for diversity;
  extremization is applied to the *pooled deviation from market*, never naively.
- **Keep score, precommit, no outcome-bias.** Brier ledger + fixed pre-resolution rubric post-mortems
  (already right). The plan adds the same discipline to *trading*: precommitted verification bar,
  precommitted per-cell kill switches.

### II.2 Wisdom of crowds — and its exploitable failure modes
- Surowiecki's conditions for crowd wisdom: diversity, independence, decentralization, aggregation.
  **A Kalshi mid-liquidity soft market fails these** — few traders, correlated retail flow,
  entertainment-driven, no sharps arbitraging 8¢ announcer-mention longshots because fees + attention
  costs exceed the pickup. That is WHY the atlas finds structure there and dies in deep markets.
- **Favorite-longshot bias** is the most replicated pricing anomaly in betting markets (racetracks:
  Ali 1977, Thaler & Ziemba 1988; misperception-vs-risk-love: Snowberg & Wolfers 2010). Kalshi soft
  markets show the same signature (harvest: mean YES-overpricing −0.020, worst in culture). Fading
  overpriced longshots in exactly the band where the crowd is thin is not "beating the crowd" — it is
  **correcting a known aggregation bias of the crowd**. This is the intellectually honest frame for
  the whole profit thesis.
- **Prediction-market efficiency literature** (Wolfers & Zitzewitz): markets are hard to beat at the
  center of the book in liquid conditions — consistent with our −0.036 skill and with the edge dying
  at OI>5k. Do not fight this; select venues where the conditions for efficiency fail.
- **LLM forecasting literature** (Halawi et al. 2024, Schoenegger et al. 2024): frontier LLM systems
  *approach* crowd accuracy, rarely beat it; blending toward the crowd helps (~0.01 Brier). Our own
  record replicates this. Expectation setting: the forecaster engine's realistic ceiling is
  parity-plus-niche, not alpha-machine.
- **Kelly** (fractional): with edge b and win prob p, full Kelly maximizes log growth but assumes the
  edge estimate is exact; we size at ≤0.25 Kelly with hard caps because our p comes from a shrunk map.

### II.3 The three-sentence thesis
The market is the best forecaster we have access to, so we only trade where the market itself is
structurally weak (thin, biased, fee-protected corners), using a calibration map learned from tens of
thousands of its own settled outcomes. The LLM loop is a *research program* trying to earn deviation
rights in a few niches, and it only converts to positions through measured, segment-level skill.
Everything is scored, everything has a precommitted kill criterion, and nothing goes live until the
paper record proves fills, survives tail events, and clears the verification bar below.

---

## Part III — The rework (workstreams, in execution order)

### Workstream A — VERIFICATION FIRST (make the paper record trustworthy and visible)
*Nothing else matters until the one profitable engine is honestly measured. Do this first.*

> **STATUS: LANDED 2026-07-17** (`lib/recledger.py` + reworked recommend/score scripts +
> `stress_recs.py` + report money page + `structural_verification` policy block v8). The 13
> legacy rows are quarantined (`fills_unverified`); the verified cohort starts at n=0 and the
> verification clock runs from here. First evidenced rec: fill snapshot shows the current open
> rec would REST, not fill — the optimistic/conservative gap is real and now measured.

**A1. Fill realism in the rec ledger.**
- `scripts/recommend_trades.py`: at rec time, fetch the orderbook (`client.get_orderbook(ticker)`)
  and record on each rec row: best bid/ask on the recommended side, top-of-book depth (contracts),
  and `fillable_now` = (entry_limit crosses the current ask for that side). Record the snapshot.
- `scripts/score_recommendations.py`: score **two P&L columns** per resolved rec:
  - `pnl_optimistic` — current behavior (assume fill at entry_limit).
  - `pnl_conservative` — fill only if `fillable_now` was true at rec time AND depth ≥ assumed size;
    otherwise the rec scores as NO-FILL (excluded from deployed capital, counted separately).
  The **conservative column is the official number** everywhere.
- Backfill: the 13 already-scored rows cannot be retro-evidenced — mark them `fills_unverified: true`
  and report them as a separate provisional cohort. The verification clock starts from A1 landing.

**A2. Tail-risk stress (the 13/13 illusion killer).**
- New `scripts/stress_recs.py`: Monte Carlo the basket using each rec's `calibrated_yes` as true
  hit probability → distribution of ROI over the next 50/100 recs; report P(ROI<0), expected max
  drawdown, and the break-even hit rate. Run it in every report build. If realized hit rate ever
  exceeds calibrated expectation at n≥30 (map is optimistic), that is a cell kill signal (A4).

**A3. Reporting: the money page.**
- `lib/report.py`: new FIRST page section — **Structural Edge (paper)**: conservative-fill scoreboard
  (n, win rate, ROI, P&L, beat-market), cohort split (verified-fill vs legacy-unverified), stress
  results, open recs with entry limits, per-cell live record, and the verification-bar progress
  (below). The user reads ONE artifact; the P&L engine must live on its front page.
- Also surface: run cost of each engine (wall-clock), so effort inversion stays visible.

**A4. Precommitted verification bar & cell kill switches (write into `data/policy.json`):**
- **VERIFIED** (unlocks the live-trading *conversation* with the user, nothing more) requires ALL of:
  1. n ≥ 40 resolved recs with **verified fills** (conservative column),
  2. conservative ROI ≥ +8% with 90% bootstrap CI excluding 0,
  3. ≥3 distinct cells each individually ROI-positive (kills single-theme luck),
  4. at least one realized tail hit absorbed with basket ROI still positive, OR stress P(ROI<0
     over next 100 recs) < 10%,
  5. realized hit rate consistent with calibrated_yes (no systematic optimism).
- **Per-cell kill switch:** any cell with n≥15 verified-fill resolved recs and negative conservative
  ROI is removed from the screen (map entry stays, trading stops). Auto, logged, reversible.
- **Global halt:** trailing-30 conservative ROI < −10% → recommendations pause, investigation run.

*Acceptance: report shows the money page; every new rec carries orderbook evidence; stress script
runs in CI of each loop; verification bar visible with live progress. Est. effort: 1–2 sessions.*

### Workstream B — SCALE THE HARVEST (the only engine with measured edge)

> **STATUS: LANDED 2026-07-17** (B1 `screen_universe.py`; B2 granular map — fine OI tiers ×
> duration bands, coarse fallback, `walkforward_validate.py` month-fold validation gating the
> screen via `atlas.tradeable_cell` (fails closed), stable-hash split fixing a latent
> randomized-split bug; B3 `harvest_history.py --all-categories` running (36k→150k+ rows, refit
> + walk-forward re-run due when it completes); B4 `scan_coherence.py` — dutch-NO auto-log with
> guaranteed floor, dutch-YES + T-threshold monotonicity report-only). First universe scan:
> 1.49M open markets → 291 +EV candidates → 12 logged, ALL fillable-now with orderbook
> evidence. Walk-forward verdict on the soft corpus: +17.4% ROI, 2,033 trades, 72/91 cells
> positive, 19 blocked. NOTE for time-remaining axis: history rows carry ONE near-close price
> snapshot, so a time-REMAINING axis is not learnable from this harvest — duration(lifetime)
> bands used instead; candlestick harvest is the future path if time-remaining matters.

**B1. Full-universe screening.** Decouple the structural screen from the watchlist/candidates
funnel. New `scripts/screen_universe.py`: `client.iter_markets(status="open")` over the ENTIRE
exchange (thousands of markets), map each into its atlas cell, emit every corrected-cell, mid-OI,
+EV-after-fee-and-half-spread market to the rec ledger (respecting per-day idempotency and A4 kill
switches). The current 150-candidate funnel throttles the profitable engine to ~1 rec/cycle; volume
is how a +8–10% edge becomes dollars. Cap per-cycle recs by exposure rules (D1), not by discovery.

**B2. Finer, richer map.** Refit `lib/atlas.py` with:
- a **time-to-close axis** (e.g. >30d / 7–30d / 1–7d / <1d) — mispricing decays into the close;
  the backtest's fill-at-implied assumption is also least wrong far from close,
- finer OI tiers inside the tradeable band (500–1k / 1–2k / 2–5k),
- guard: keep shrunk-Platt + MIN_CELL_N discipline; more axes × 36k rows is fine, but validate with
  **walk-forward** (fit on months 1..k, test k+1) rather than one random split, since bias can decay
  as Kalshi's crowd matures. Report per-cell OOS ROI with CIs; only cells positive in walk-forward
  enter the live screen.
- **Refit cadence:** monthly, automated, with the old map archived and a fit-vs-fit diff logged.

**B3. Category expansion for the MAP only.** The sports/crypto blocklist exists because *research*
can't beat those markets — but the structural harvest needs no research, and favorite-longshot bias
was first documented in sports betting. Extend `harvest_history.py` to all categories, fit cells,
and let walk-forward OOS decide which categories carry a tradeable correction. The LLM-forecast
blocklist is untouched. (Default-on per the autonomy directive; trivially reversible; flagged here
so the user sees it.)

**B4. Coherence/arbitrage scanner (crowd-incoherence harvest, no model at all).**
New `scripts/scan_coherence.py`: within each event of mutually exclusive outcomes, check
Σ(YES asks) and Σ(NO asks) against 1 ± (fees + spreads); for ordered bracket markets (CPI ranges,
RT-score thresholds) check monotonicity of the implied CDF. Emit violations that clear fees as
recs tagged `arb` (their own cell + kill switch). These are the purest wisdom-of-crowds trades —
no opinion, just enforcing probability axioms the crowd violated. Expect few but near-riskless.

*Acceptance: screen sees the full exchange; map has time axis + walk-forward validation; coherence
scanner live; rec volume 5–20×; all still paper + conservative-scored. Est. effort: 2–3 sessions.*

### Workstream C — REFIT THE FORECASTER AS R&D (Tetlock-aligned, cheap, honest)

> **STATUS: LANDED 2026-07-17.** C1: `TRIAGE_EXCLUDED_SUBCATS` enforced mechanically in
> `curate_watchlist.py` (refused a Fed add in test; active Fed market dropped). C1b: `sports`
> canonical category + `config.is_sports_decision` gate (MVP/coach/ruling markets classify in,
> game outcomes stay blocked — 6/6 test cases), taxonomy segments award-vote/personnel/ruling/
> participation, 7 sports-decision sources added to the registry. C2: shadow A/B OFF
> (`SHADOW_AB_ENABLED=False`; ab_score still scores persisted pairs). C3: `LD5-diverse` arm is
> DEFAULT — live smoke: 4/4 members (Qwen std 0.62 / Qwen outside 0.65 / Mistral std 0.572 /
> Mistral inside 0.593 → median 0.6065), atlas price joins at combine time only. C4:
> `response_format=json_object` + repair pass + `data/llm_json_stats.json` malformed_rate —
> all 3 previously-deferred critics (Platner, CPI-T0.0, YoY-3.9) now return status:ok,
> malformed_rate 0.0. Deferred post-mortems from 2026-07-17 are unblocked — complete them on
> the next /update.

**C1. Triage the watchlist (CHAMPS commandment #1).** Curation policy change in `ROUTINE.md` +
`curate_watchlist.py`: DROP efficient-macro and deep-liquidity headline markets entirely (Fed, CPI,
us-president — all measured negative-skill, and they eat the run's wall-clock). The watchlist holds
ONLY: (a) down-ballot/under-covered politics (the one +skill segment — deliberately grow its n),
(b) mid-priced (0.15–0.85) thin/mid-OI culture with real evidential basis, (c) markets the atlas
flags as sitting in a corrected cell (forecaster as a second opinion on structural candidates),
(d) **sports human-decision markets (C1b)**. Target ≤10 forecasts/run. The goal is to get
`us-governor-primary`-type segments to n≥10 fast and find out if the niche skill is real.

**C1b. The sports human-decision pivot (user directive, 2026-07-17).** Sports is admitted to the
*judgmental* forecaster — but only its political layer. The scope rule, stated once and enforced in
code: **a sports market is forecastable iff resolution runs through HUMAN DELIBERATION, never
through play on the field.** Game outcomes (spreads, moneylines, totals, "who wins X") stay
blocklisted forever — they are the most efficiently priced crowd-forecasts in existence and a
judgmental forecaster attacking them is triage-negligence. What qualifies:
- **Award votes** — MVP/Cy Young/Heisman/Ballon d'Or/HOF: *elections with a known, small, studiable
  electorate*. Voter regularities (narrative arcs, stat thresholds, team-success requirements,
  ballot timing — e.g. regular-season-only voting that casual money contaminates with playoff
  narrative) are exactly reference-class material.
- **Personnel decisions** — coach firings/hires, trades by deadline, draft picks, extensions,
  retirements, holdouts: one or few identifiable deciders under observable incentives.
- **Institutional rulings** — suspensions/appeals, CFP committee rankings, expansion/relocation and
  rule-change votes, host-city selection.
- **Participation/announcement questions** — "will X play in Y", comeback/opt-out announcements.
Why the crowd is beatable there (behaviorally, not quantitatively): fan sentiment (hopes get bet),
narrative seduction (media storyline ≠ decider incentives), single-game recency overreaction (the
"no single poll moves you" rule maps to **"no single game moves you"**), and thin attention (a
coach-firing market gets a fraction of a moneyline's scrutiny). These markets are structurally
identical to down-ballot nominations — the one segment with measured positive skill.
Implementation:
1. `lib/config.py` — carve the blocklist: keep game-outcome sports blocked; whitelist
   human-decision sports series (maintain an explicit allowlist of series patterns, reviewed at
   curation; when ambiguous, excluded).
2. `lib/taxonomy.py` — new segments: `sports/award-vote`, `sports/personnel`, `sports/ruling`,
   `sports/participation`, so skill accrues per sub-category from day one.
3. `fetch_candidates.py` — include the whitelisted series in discovery; curation ranks them by the
   same forecastability rule (real evidential basis: voter history, beat-reporter signal, incentive
   structure — no coin-flips).
4. Source registry — add the source families this niche needs (national vs local beat reporters,
   cap/contract analysts, public ballot trackers), with reliability ratings.
5. **Same earning bar as everything else (C5/Part IV):** sports-decision segments take ZERO
   positions until a segment reaches n≥10 resolved with positive skill; any segment at n≥10 with
   negative skill is permanently barred. This is an experiment with a precommitted verdict, not a
   new profit assumption. (Note: B3's *structural* sports expansion is independent — the map may
   trade game-outcome cells mechanically; the forecaster never does.)

**C2. End the A/B, pick one model.** The shadow reaches its n=25 target within ~2 runs. Both models
lose to the market badly; the leader (currently Mistral) becomes the sole forecaster; the dual pass
ends (halves forecast wall-clock). Keep the loser available as an ensemble *member* (C3), not as a
parallel full pass.

**C3. A real diversity ensemble (Page's theorem, GJP's recipe).** Replace "5× same-model samples"
in `lib/local_llm.forecast_ensemble` with 5 *heterogeneous* members: (1) Qwen with the standard
prompt, (2) Mistral with the standard prompt, (3) a base-rate-only persona (outside view, forbidden
from citing news), (4) an incentive/insider persona (inside view), (5) **the atlas-calibrated market
price as a member** (the crowd, corrected, gets a vote). Combine with the trimmed mean; then apply
**learned extremization to the pooled deviation from the market price only** (never extremize toward
the ensemble's own absolute number — with skill<0 that's amplifying noise; the learning-policy
shrink-to-market already estimates exactly the right α per segment, so extremization = α>1 must be
EARNED per segment by the record, exactly like every other deviation right).

**C4. Fix the critic reliability.** 5/7 post-mortems deferred on JSON parse errors. In
`lib/local_llm`: enforce structured output (Ollama `format: json` / JSON-schema constrained decoding)
on `critique`, `challenge`, `revise_skill`; retry-with-repair once; log a `malformed_rate` metric.
The adversarial panel is the learning loop's backbone — it can't be 30% deaf.

**C5. Keep the emergence rule untouched.** Positions from forecasts still flow ONLY through the
learning policy's measured segment skill (shrink-to-market α). No new gates, no loosened gates
(URAN trap). If no segment ever earns deviation rights, the forecaster remains a $0 research
program — that is an acceptable steady state; the structural engine is the P&L.

*Acceptance: run wall-clock cut ~60%; watchlist 100% Goldilocks; ensemble genuinely diverse;
malformed_rate <5%; niche segments accruing n; sports human-decision segments discoverable, taxed
into their own sub-categories, and accruing a scored record. Est. effort: 2–3 sessions.*

### Workstream D — RISK & EXECUTION FRAMEWORK (build the rails now, connect money LATER)

> **STATUS: LANDED 2026-07-17.** D1: `position_sizing` policy block v9 (quarter-Kelly,
> 2%/market, 10%/cell, 5%/event-family, 50% total, −15% drawdown halt, 5-day GTC expiry;
> `live_bankroll: null` per user constraint). D2: `lib/broker.py` PaperBroker +
> `scripts/paper_broker.py` — resting-limit simulation against the live book (fills at the
> observed ask only when it crosses the limit, depth-capped, partial fills, expiry = the
> honest no-fill record); first cycle: 13 orders → 4 filled at ask, 3 resting, 6 SKIPPED by
> the per-cell 10% cap (the correlation rail binding exactly as designed on a single-cell
> basket), $118.93/$1000 notional deployed. Money page reports broker equity/no-fill/ROI.
> D3: `docs/EXECUTION.md` LiveBroker spec (contract parity, reconciliation loop, dry-run
> flag, kill file, rollout ladder) — zero live-order code exists, by design.

**D1. Sizing & exposure policy (encode in `data/policy.json` now, used by the paper broker):**
- per-market: min(0.25 × Kelly fraction from conservative cell edge, 2% of bankroll),
- per-cell: 10% of bankroll; per-event-family (e.g. one broadcast's mention markets): 5%,
- total deployed: 50%; drawdown halt: −15% from equity peak pauses new entries,
- bankroll is a config variable with NO value set (user decides at the gate; "not yet" on record).

**D2. Paper broker with real fills.** New `lib/broker.py` with a `PaperBroker` that simulates limit
orders against the LIVE orderbook over time (order rests; fills only when the book actually crosses
it; partial fills by displayed depth). This replaces fill *assumptions* with fill *simulation* — the
final verification layer, and the exact interface a future `LiveBroker` implements. All Workstream B
recs route through it.

**D3. Live execution — DESIGN ONLY, behind a dead flag.** Spec the `LiveBroker` (Kalshi trading API:
auth, create/cancel order, positions, settlements, reconciliation loop) in `docs/EXECUTION.md`, but
**write no live-order code until the A4 verification bar passes AND the user explicitly signs off
and provisions a trading key + bankroll figure.** The current key stays read-only; `gitops`
secrets guard already covers key material.

*Acceptance: every rec flows through PaperBroker with simulated resting-order fills; sizing policy
enforced in paper; EXECUTION.md spec reviewed; zero live-order code exists. Est. effort: 2 sessions.*

### Workstream E — OPERATING LOOP RESTRUCTURE (money path first-class)

> **STATUS: LANDED 2026-07-17.** `scripts/money_path.py` — the whole structural pipeline
> (score → screen → coherence → broker) as ONE LLM-free command: every stage always runs,
> failures print loudly and exit non-zero for the run log. ROUTINE.md rewritten to the
> two-half shape (money path Steps 1–2 unconditional incl. local_llm DOWN; forecaster R&D
> pass Steps 3–4 conditional, ≤10 markets; report/log/commit always), with the degradation
> matrix explicit. `.claude/commands/update.md` rewritten to match (summary leads with the
> official conservative scoreboard). Smoke-tested: 4/4 stages chain, COMPLETE in fast mode.
> ALL FIVE WORKSTREAMS A–E ARE NOW LANDED — what remains is accrual (verified fills toward
> the A4 bar) + the post-harvest refit (B3 harvest still running, >5.5M rows).

New `/update` step order (rewrite `ROUTINE.md` accordingly):
1. Preflight → 2. Reconcile resolutions + **score recs (conservative)** → 3. **Full-universe screen
→ coherence scan → PaperBroker order maintenance** (the money path, always runs, fails loudly) →
4. Triage-curated forecaster pass (≤10 markets, diverse ensemble) → 5. Post-mortems + learners →
6. Stress + report (money page first) → 7. Log + commit.
The structural path must be independent of local-LLM availability — it is pure API + arithmetic and
must run even when Qwen is down (today the whole loop no-ops on local_llm outage; only steps 4–5
should).

---

## Part IV — Sequencing, measurement, and kill criteria

**Order: A → B → C/D (parallelizable) → E.** A alone makes the current record honest and visible
within a session. B multiplies the rec flow so the verification bar's n≥40 accrues in weeks, not
months. C stops burning the run on unbeatable markets. D readies the rails. E locks the new shape.

**The go/no-go ladder (supersedes the Phase-2 bar in PROFITABILITY_PLAN.md for the trading engine):**
- **Milestone V (verify):** A4 bar met on conservative fills → present the user the evidence and
  ask for the live decision + bankroll. *Until then, live trading is not discussed again.*
- **Milestone S (sunset test), 8 weeks from Workstream A landing:** if conservative-fill ROI at
  n≥40 is ≤0, the structural thesis fails forward validation → the engine (and, per the standing
  directive, likely the project) sunsets. The forecaster alone does not justify the loop unless some
  segment has by then earned deviation rights with positive realized paper ROI (n≥10, per C5).
- **Forecaster segment rule:** any segment reaching n≥10 with skill<0 is permanently barred from
  position-taking; any segment with n≥10, skill>0, and positive counterfactual ROI earns a paper
  position pilot (still through the α mechanism).

**Metric definitions (single source of truth):** official ROI = conservative-fill realized P&L ÷
capital deployed at verified fills, after Kalshi fee `ceil(0.07·p·(1−p))` and simulated slippage.
Optimistic ROI is reported alongside, clearly labeled, never headlined. Brier skill vs market stays
the forecaster's metric; it is not a trading metric.

**Standing constraints that survive this rework (do not relitigate):**
anti-anchoring; leans never oppose the modal forecast; hard_gap_ceiling; EV floor; adversarial gate
unskippable; never self-judge; no secrets committed; Qwen does forecaster cognition, Opus is
plumbing + Defender/Judge; act autonomously within the gates — but the LIVE-MONEY switch and
bankroll are the user's alone.

## Part V — Open items for the user (none block Workstreams A–E)
1. **Live-trading decision** — deferred until Milestone V presents verified evidence. (Your call.)
2. **Bankroll** — "not yet"; D1 keeps it a config variable.
3. **B3 category expansion** (structural map over sports etc.) — default-on, reversible; veto anytime.
