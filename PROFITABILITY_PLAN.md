# Profitability Plan — make it pay or sunset it

**Directive (user, 2026-06-29):** "this project needs to become profitable or it dies."
Profitability is now the single governing objective. This document is the plan, the timeline,
and the kill criterion. It is grounded in the resolved record, not aspiration.

## The brutal diagnosis (resolved record, n=31, 2026-06-29)

- **We are WORSE than the market.** Mean Brier: **mine 0.157 vs market 0.112 → skill −0.045.**
  We beat the price on only **10/31 = 32%** of resolved markets.
- **You cannot profit while being less calibrated than the price you trade against.** This is the
  root cause. "No edge → no position" is the gates working correctly, not the problem.
- **Counterfactual P&L by gap:** the **0–5pt band is the ONLY profitable one (+0.061 ROI, n=17)**.
  5–10pt −0.31, 10–20pt −0.68, 20pt+ −0.14. Every real fade of a liquid market loses (the URAN lesson,
  now confirmed across the whole record).
- **Where we have any skill:** only `politics/governor-primary` (+0.07, n=3) and `politics/mayoral`
  (+0.03, n=2) — small samples, but both are **down-ballot / under-covered** races. Everything
  efficient loses: economy/fed −0.03, us-president −0.11, box-office −0.07.

**Conclusion:** the path to profit is NOT "take more positions." It is (1) stop forecasting efficient
markets we can't beat, (2) concentrate on under-covered niches where the crowd is lazy and research can
win, (3) become measurably better than the price there, and (4) only deploy capital where we have
*earned, measured* skill. If local models cannot beat the market anywhere, the all-local thesis is
falsified and we change the forecaster or sunset.

## Betting policy ≠ learning policy (user correction, 2026-06-29)

The lever is **NOT** a smarter *betting* policy (when to act on a forecast — the EV/gap gates in
`learn_policy.py`). No betting policy can rescue a forecaster worse than the price. The lever is a
**learning policy**: make the *forecasts themselves* better from resolved outcomes. The record proves
this — historically **even Opus lost to the market** (S-arms skill ≈ −0.12, worse than Qwen −0.09), so
adding a bigger model alone won't fix it. We use **3 forecasters (Qwen + Mistral + Opus)** for diversity;
the learning loop is what must make the ensemble beat the price.

**Built: `lib/learning.py` — the learning policy** (recomputed every reconcile, persisted to
`data/learning_policy.json`):
1. **Recalibration** per model (logit-temperature fit to outcomes, shrunk at low n). Already learned
   Qwen k=0.88 / Opus k=0.80 — both overconfident, now corrected.
2. **Segment × model trust** — learned ensemble weights (Qwen 0.40 ≫ Opus 0.08 from realized Brier).
3. **Shrink-to-market** — deviate from the price only by our *measured, shrunk* skill in that segment
   (min 3 resolved). No skill ⇒ track the market ⇒ no edge ⇒ no bet. **Position-taking emerges from
   learning, not a hardcoded gate** — this is what ties the loop directly to profitability.

**WIRED AT FORECAST TIME (2026-06-29):** the learning policy is no longer just recomputed — it now
shapes every recorded forecast (`scripts/ab_forecast.py` Phase D): the arm's aggregated probability is
recalibrated for its model and shrunk toward the price by measured segment skill. On the current
record exactly ONE segment has earned a deviation — `politics / us-governor-primary` (n=3,
**alpha=0.344**); everywhere else alpha=0, so we track the price and take no position. That is the
emergence working as designed. Two more learning levers shipped alongside it:
- **Error-memory injection** (`lib/error_memory.py`) — before forecasting each market, the forecaster
  is shown its most-similar PAST MISSES (markets where it failed to beat the price) + the post-mortem
  lesson, in-context, so it stops repeating avoidable errors. Same block feeds both Qwen and Mistral.
- **LoRA fine-tune scaffold** (`scripts/build_lora_dataset.py`, `scripts/lora_finetune.py`,
  `docs/LORA.md`) — leakage-aware SFT export (beat-market ⇒ reinforce own prob; lost ⇒ defer to the
  crowd price; never the raw outcome). Training is NOT run (corpus small, no backend on-box); the
  scaffold gates on corpus size and a future LoRA arm must beat the stock model on the held-out record.
The 3-way shadow A/B (adding an Opus forecaster) was deliberately deferred as too expensive; the
shadow stays Qwen-vs-Mistral.

## The methods (what we're adding/replacing, and why)

Current learning: 5× ensemble (variance), policy-knob learner (when to bet), strategy bandit
(topology), adversarial gate + autonomous SKILL revision. Missing pieces, by leverage:

1. **Market-as-prior, not base-rate-as-prior.** The data says edge lives at 0–5pt from the price.
   Strongest LLM-forecasting result (Halawi 2024): models approach but rarely beat the crowd; the win
   is to anchor to the market and deviate only on specific, well-sourced, high-conviction reasons. New
   arm: `market-anchored` (heavy market weight, model nudges). **Caveat:** only profits if the nudge is
   genuinely informative — if we're noise, this loses to fees. So it's gated to positive-skill segments.
2. **Post-hoc recalibration.** Learn an isotonic/Platt map from the resolved record (raw model prob →
   calibrated prob). Cheap, proven, data-efficient; shaves Brier if we're systematically over/under-
   confident. Necessary-not-sufficient (won't make a worse model beat the market, but stops self-inflicted
   miscalibration).
3. **Cross-MODEL ensemble.** Diverse models cut correlated error better than 5 passes of one. Once the
   shadow A/B shows per-segment strengths, combine Qwen+Mistral (and optionally a frontier model).
4. **Segment-conditioned position-taking.** Only take live leans in segments with measured non-negative
   skill (`by_segment`). Paper-only everywhere else until skill is earned. Trade where we've earned it.
5. **Active market selection toward inefficiency.** Curate toward thin, mid-priced (0.15–0.85),
   under-covered markets (down-ballot/foreign nominations, local races, niche culture) and AWAY from
   efficient macro/headline markets where we reliably lose.
6. **The ceiling test (the big lever).** Local models may simply not be good enough to beat the market.
   A frontier forecaster arm (Opus) would measure this directly. It is **flag-gated OFF for scheduled
   runs** (respects the no-Anthropic-scheduled-spend / on-device directive) but can be flipped on to
   prove whether local is the ceiling. If frontier beats the market where local can't, that decides it.

## Timeline (the loop runs 3×/day; resolutions accrue over weeks)

- **Phase 0 — NOW (this run):** brutal diagnosis published; shadow A/B made standard
  (`ab_forecast.py` + `ab_score.py`, persists both models, auto-resolves the head-to-head).
- **Phase 1 — week 1–2 (stop the bleeding + calibrate):** curation concentrates on positive-skill /
  under-covered segments, drops efficient macro; isotonic recalibration layer; segment-conditioned
  leans (paper-only outside earned segments).
- **Phase 2 — week 2–4 (beat the market in a niche):** market-anchored arm for the 0–5pt fee-screened
  band; cross-model ensemble from the shadow A/B winner; optional frontier ceiling test.
- **Phase 3 — week 4+ (scale what works):** bandit + learner concentrate on the winning (arm × segment)
  cells; profit becomes a first-class continuously-tracked metric driving selection.

## Go / no-go (the "or it dies" bar)

By the end of Phase 2 (~4 weeks / ~target resolutions):
- **GO** if (a) overall `skill_vs_market ≥ 0`, AND (b) at least one (arm × segment) cell shows
  **positive realized ROI after fees over ≥15 resolved leans.**
- **ESCALATE** (flip on the frontier forecaster) if local arms still trail the market everywhere but
  the niche/curation thesis looks alive.
- **SUNSET** if neither local nor frontier can clear the bar — the markets we can reach are efficient
  enough that no edge survives fees. Better to know than to bleed.

_This plan is reviewed and revised as the record grows. Every change is git-tracked and reversible._
