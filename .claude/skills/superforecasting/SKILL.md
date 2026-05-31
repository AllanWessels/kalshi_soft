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
- `--lean` / `--conviction` — paper-only direction. Set a lean only when your edge is real:
  - edge = your_prob − market_prob. |edge| < 0.05 → `NONE`.
  - 0.05–0.10 with medium+ evidence → low/medium conviction.
  - > 0.10 with solid, disconfirmation-tested evidence → medium/high conviction.
  - This is paper only — the API key is read-only, no orders are ever placed.
- `--rationale` — 1–3 crisp sentences: the base rate, the decisive update, the disagreement
  with the market. `--drivers`, `--reference-classes`, `--refs` — the supporting lists.

### 8. Drift discipline (on re-forecast)
When you re-forecast a market you've seen before, read your previous entry first. Justify any
change against new evidence. Don't anchor rigidly to your old number, and don't churn it on
noise. A forecast that moves only when the world moves is a calibrated forecast.

## Calibration feedback loop
Once markets resolve, `data/calibration.json` holds your Brier score vs the market's and a
reliability curve. Read it before forecasting:
- If your high-confidence forecasts resolve worse than their probabilities imply, you are
  **overconfident** — compress toward 0.5.
- If they resolve better, you are **underconfident** — be bolder.
- Watch `skill_vs_market` (positive = you are beating the market). Per-category breakdowns tell
  you where your edge is real and where it isn't.

## Non-negotiables
1. Independent estimate before the market price — always.
2. Granular probabilities, epistemic confidence stated separately.
3. Every forecast names the market title + ticker so it's findable on Kalshi.
4. Cite your sources in `--refs`. No forecast without research.
