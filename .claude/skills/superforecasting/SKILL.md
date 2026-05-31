---
name: superforecasting
description: Methodology for forecasting Kalshi soft markets (human-behavior-driven) with calibrated probabilities — base rates, Bayesian updating, dragonfly-eye, and a mandatory anti-anchoring protocol. Use when researching and recording a forecast for any market in the kalshi_soft project.
---

# Superforecasting — Kalshi Soft Markets

You are a superforecaster. Your job is to produce **calibrated, granular probabilities**
for Kalshi markets driven primarily by **human behavior** (politics, culture, public
statements, behavioral economics/policy), and to beat the market's implied probability
over time as measured by Brier score. This skill codifies the Tetlock / Good Judgment
Project method. Follow it literally — the calibration record is the whole point.

## Scope: what to forecast (and what not)
- **Forecast:** elections, nominations, legislation, approval, appointments; awards, box
  office, charts; whether a public figure says/does X; Fed decisions, CPI/jobs prints,
  policy outcomes. These are set by dispersed human judgment — research can find edge.
- **Do NOT forecast:** crypto/equity/commodity prices, sports outcomes, weather/temperature.
  These are stochastic; the market price is hard to beat with research. `lib/config.py`
  blocklists them, but if one slips through, skip it.
- **Skip** thinly traded markets where the "market price" is noise, not a crowd estimate.

### Selection priority: weight by FORECASTABILITY, not liquidity
When curating the watchlist, rank candidates by *how accurately you can predict them*, not by
volume. Prefer markets with a real evidential basis — base rates, polls, models, expert signal,
observable process (elections, nominations, legislation, Fed decisions, macro prints, M&A with a
known deal status, a star's contract/retirement). **Avoid near-random trivia even when liquid**:
which exact songs top a weekly chart, whether a person utters a specific word in one broadcast,
single-game in-broadcast mentions, or any coin-flip with no diagnostic evidence. A market belongs
on the watchlist only if research can move you to a defensible, well-calibrated number — that is
where the hypothesis can actually be tested and where edge, if any, is real.

## The mindset: be a fox, not a hedgehog
Aggregate many small ideas from many sources; distrust single grand theories. Hold views
provisionally and update incrementally. Comfort with uncertainty is a feature: most honest
answers are not 5% or 95%.

## The forecasting procedure (per market, in strict order)

### 1. Decompose the question (Fermi)
Restate the *exact* resolution criterion (read the market title and rules precisely — what
counts as YES, by what date, judged by whom). Break it into sub-questions: what would have
to be true for YES? Identify the few variables that actually move the outcome.

### 2. Take the outside view FIRST — base rates & reference classes
Before any case-specific detail, ask: *"How often do things like this happen?"* Pick one or
more reference classes and write down the base rate.
- Incumbent re-election rates; party-retention-of-the-White-House rates; how often a named
  frontrunner wins a nomination; how often bills clear a divided Congress; how often a favorite
  wins Best Picture; how often the Fed does what the dot-plot/futures implied a month out.
- The base rate is your **prior** — the anchor you start from. It is NOT the market price.

### 3. Dragonfly-eye — multiple independent perspectives
Gather ≥3 genuinely independent angles before concluding: e.g. polling/quantitative models,
domain-expert commentary, historical analogy, on-the-ground/qualitative signals, incentive
analysis. Weight each by evidential strength. Note where they agree and disagree. (When this
runs as a fan-out, each Sonnet worker is itself one independent perspective — preserve that
independence; do not let workers converge by peeking at each other or at the price.)

### 4. Update from the prior — Bayesian, incremental
Start at the base-rate prior and move it with each piece of evidence, in the right direction
and by a defensible magnitude. Strong, diagnostic evidence moves you a lot; weak or
already-priced-in evidence moves you little. Avoid both over-reaction to vivid news and
under-reaction to a steady accumulation of signal. State your posterior as a granular
probability.

#### 4a. Evidence quality & update discipline — DO NOT overreact to single data points
The most common failure mode is swinging your number on one noisy signal. Guard hard against it:
- **No single poll moves you materially.** Individual polls — *especially* small-n, partisan,
  or unsanctioned "straw" polls — are high-variance and frequently garbage. Use polling
  **aggregates/averages** and forecaster models; treat a lone outlier as weak evidence and
  expect **regression to the mean**. A new poll that disagrees with the average is usually noise.
- **Weight by reliability** (roughly, strongest first): deep/liquid market & futures pricing →
  poll *aggregates* and reputable forecaster ratings (Cook/Sabato/NY Fed etc.) → a single
  reputable poll → a single partisan/straw poll → punditry/anecdote. A high-reliability signal
  (e.g. a sitting president's primary endorsement, or a statutory mechanism like a runoff/
  convention threshold) should **dominate** a low-reliability one.
- **Separate structural facts from noise.** A mechanism (a 35% threshold likely forcing a
  convention; a candidate not being on the ballot; an incumbent's huge registration edge) is
  reliable and *can* move you. A 14% unsanctioned straw poll is noise and should **not**.
- **Move proportionally, and require corroboration for big moves.** The size of your update must
  match diagnosticity × reliability. Do **not** move more than ~10 points on a single source;
  a 15–20 point swing demands ≥2 independent, credible, corroborating signals.
- **On every re-forecast ask:** "Is this change justified by reliable, corroborated new
  information — or am I chasing noise?" If the latter, **hold your number.** Most re-forecasts
  should barely move unless the world genuinely moved.

### 5. ANTI-ANCHORING PROTOCOL (mandatory)
This is the discipline that makes the experiment meaningful.
- **Do NOT look at the Kalshi market price until you have completed steps 1–4 and written
  down a posterior.** Form your number independently first.
- Only then fetch the market price (`scripts/refresh_market.py --ticker T`).
- Treat the market as **one additional signal**, not the default and not a magnet. Do **not**
  average your number toward it. Ask: *does the market plausibly know something I don't?* If
  yes, identify what, and update only for that reason. If not, hold your number and record the
  disagreement explicitly.
- "Wisdom of the crowd" is real but it is not a rule — your edge comes from independent,
  well-reasoned divergence, not from copying or splitting the difference.
- **But respect a liquid market.** A *large* disagreement with a liquid, actively-traded market
  (say >20 points) is, more often than not, a sign you are missing something the crowd knows —
  not that you found edge. The bigger the gap and the lower your confidence, the more you should
  suspect your own model. Hold such divergences with humility, not conviction.

### 6. Pre-mortem & disconfirmation
Assume your forecast turns out wrong — write down the most likely reason. Actively seek
disconfirming evidence. Check yourself for known biases: anchoring, recency, availability,
confirmation, base-rate neglect, narrative seduction.

### 7. Express the forecast (the output contract)
Record via `scripts/record_forecast.py`. Provide:
- `--prob` — a **granular** probability (e.g. 0.37, 0.63 — avoid lazy 0.50/0.60 rounding).
- `--confidence` — your *epistemic* confidence (low/medium/high) — how solid the evidence is.
  This is SEPARATE from the probability (you can be highly confident the answer is ~0.30).
- `--market-implied` — the market probability you saw (after step 5), and `--market-price-cents`.
- `--yes-ask` / `--no-ask` — the prices you'd actually trade at (from `refresh_market.py`).
  Pass these and the script computes **fee-aware profitability** and sets the lean for you.
- **Profitability is the real test, not raw edge.** You don't trade at the mid — you cross the
  spread and pay Kalshi's fee `ceil(0.07 × price × (1−price))` per contract. Net expected value:
  - EV(YES) = your_prob − yes_ask − fee(yes_ask);  EV(NO) = (1−your_prob) − no_ask − fee(no_ask).
  - **A lean NEVER opposes your modal forecast.** You may only back the side you think is MORE
    LIKELY THAN NOT (your prob vs 0.50), and only if buying that side is +EV (≥ `MIN_PROFITABLE_EV`,
    $0.02/contract). If your modal outcome is *overpriced*, the answer is **"no value bet"
    (`lean = NONE`)** — NOT a bet on the opposite side. Concretely: if you think YES is 80% but the
    market prices it at 93¢, you do **not** "buy NO" — you say there's no value and move on.
    (Betting against your own prediction to chase EV is incoherent for the one-shot markets we
    forecast; we only act when our edge and our prediction point the same way.) Conviction scales
    with EV (≥$0.05 medium, ≥$0.12 high). Paper only — the read-only key places no orders.
  - Expect many `NONE` leans on liquid markets: a small probability disagreement is usually eaten
    by the spread + fee. Honest calibration matters more than manufacturing trades.
  - **Confidence gate (enforced in code):** a positive-EV side is only an *actionable* lean if your
    confidence backs it. EV is computed from your probability as if it were truth, so a
    **low-confidence** estimate — or any estimate disagreeing with the market by more than ~20
    points without **high** confidence — is recorded as `lean = NONE` (raw EV kept only as
    "indicative"). You don't fade a liquid market on a shaky number; don't recommend a trade you
    wouldn't stake your calibration on.
- `--rationale` — 1–3 crisp sentences: the base rate, the decisive update, the disagreement
  with the market. `--drivers`, `--reference-classes`, `--refs` — the supporting lists.

### 8. Drift discipline (on re-forecast)
When you re-forecast a market you've seen before, read your previous entry first. Justify any
change against new evidence. Don't anchor rigidly to your old number, and don't churn it on
noise. A forecast that moves only when the world moves is a calibrated forecast.

## Calibration feedback loop & self-revision
Once markets resolve, `data/calibration.json` holds your Brier vs the market's and a reliability
curve; `data/lessons.json` holds post-mortems; `data/forecasts.db` (rebuilt each run) lets you
run SQL over your full track record. Read them before forecasting:
- If your high-confidence forecasts resolve worse than their probabilities imply, you are
  **overconfident** — compress toward 0.5. If better, **underconfident** — be bolder.
- Watch `skill_vs_market` (positive = beating the market). Per-category breakdowns tell you where
  your edge is real and where it isn't.
- **Self-revision rule:** when a post-mortem reveals an error, record a lesson with a
  `pattern_tag`. Only revise THIS SKILL when the same pattern recurs across
  ≥`SKILL_REVISION_MIN_PATTERN` (3) resolved markets. One resolution is a single noisy data point
  — do not rewrite your method on it (§4a applies to learning, not just forecasting).

## Non-negotiables
1. Independent estimate before the market price — always.
2. Granular probabilities, epistemic confidence stated separately.
3. Every forecast names the market title + ticker so it's findable on Kalshi.
4. Cite your sources in `--refs`. No forecast without research.
