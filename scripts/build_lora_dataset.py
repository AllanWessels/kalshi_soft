#!/usr/bin/env python3
"""build_lora_dataset.py — export the resolved record as a LoRA SFT dataset (#4, scaffold).

The learning policy (lib/learning.py) and error-memory (lib/error_memory.py) make the forecaster
better WITHOUT touching weights — post-hoc recalibration, shrink-to-market, and in-context recall.
LoRA fine-tuning is the heavier, slower lever: fold the corrected behavior into the local model's
weights so it forecasts better from the first token, not just after a recalibration map. This
script builds the supervised dataset; the actual training runs out-of-process on the home GPU via
`scripts/lora_finetune.py` (external tooling — see docs/LORA.md). Nothing here trains a model.

LEAKAGE DISCIPLINE (the part that makes this defensible, not overfitting):
We do NOT train the model to output the realized outcome — that teaches "this market resolved YES",
which is a memorized label, not a transferable skill, and would be catastrophic leakage. Instead the
target probability encodes the PROFITABILITY THESIS the resolved record proves:

  * BEAT THE MARKET on a resolved market  -> target = our OWN committed probability.
    Reinforce the reasoning that produced a genuine edge. "When you deviated like this, you were right."
  * LOST TO THE MARKET                     -> target = the MARKET-implied price.
    Teach humility toward the crowd: "here, you should have been at the price." This directly
    counters the over-fading that the diagnosis says costs us money.
  * No market baseline captured            -> SKIP (no defensible target).

The input mirrors inference: the question + a reconstructed EVIDENCE block (the key drivers and
reference classes the forecaster actually reasoned over, which we DO persist). The assistant target
is the same JSON schema the live forecaster emits, plus a short reflection (the post-mortem lesson
when one exists) so the model learns to articulate the correction.

Output: data/lora/{sft_train.jsonl, sft_val.jsonl, manifest.json} in OpenAI/ShareGPT messages
format (llama-factory, unsloth, and axolotl all read it). Deterministic ~85/15 split by ticker hash.

Usage: python3 scripts/build_lora_dataset.py [--out data/lora] [--val-frac 0.15] [--min-n 20]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config, store, schemas  # noqa: E402

_SYSTEM = (
    "You are a calibrated superforecaster for human-behavior markets (politics, policy, culture). "
    "Reason from the supplied evidence ONLY. Establish a base rate / reference class first, update "
    "incrementally, run a quick pre-mortem, and do not overreact to a single noisy signal. You are "
    "NOT given the market price. Output a granular probability and judge epistemic confidence "
    "separately. Default toward the crowd unless you have a specific, well-sourced reason to deviate."
)


def _stable_hash(s: str) -> int:
    h = 0
    for ch in s or "":
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def _commit_entry(record, commit_as_of: str):
    """The forecast entry that produced the committed probability (what we score). Falls back to
    the first, then the last entry. Its rationale is the closest proxy we persist for the evidence
    the forecaster actually reasoned over."""
    if record and record.history:
        for e in record.history:
            if commit_as_of and e.as_of == commit_as_of:
                return e
        return record.history[0]
    return None


def _evidence_block(entry) -> str:
    """Reconstruct an inference-shaped evidence block from the persisted reasoning artifacts."""
    if entry is None:
        return "(no evidence captured)"
    parts = []
    if entry.key_drivers:
        parts.append("KEY DRIVERS:\n" + "\n".join(f"- {d}" for d in entry.key_drivers))
    if entry.reference_classes:
        parts.append("REFERENCE CLASSES:\n" + "\n".join(f"- {r}" for r in entry.reference_classes))
    if entry.rationale_summary:
        parts.append("PRIOR ANALYSIS:\n" + entry.rationale_summary)
    return "\n\n".join(parts) if parts else "(no evidence captured)"


def _lesson_for(ticker: str, lessons) -> str:
    for l in reversed(lessons):
        if l.ticker == ticker and l.lesson:
            return l.lesson
    return ""


def build_example(res, record, lessons) -> dict | None:
    """One SFT example from a resolution, or None if no defensible target exists."""
    if res.brier_mine is None or res.final_market_implied is None:
        return None  # need a market baseline to set a humility/reinforcement target
    beat = (res.brier_market is not None) and (res.brier_mine < res.brier_market)
    own = res.commit_probability if res.commit_probability is not None else res.final_my_probability
    if own is None:
        return None
    # The thesis target: reinforce our number where we beat the price; defer to the price where we lost.
    target_p = round(float(own), 4) if beat else round(float(res.final_market_implied), 4)

    entry = _commit_entry(record, res.commit_as_of)
    user = (
        f"QUESTION: {res.title or res.ticker}\n"
        f"AS_OF: {res.commit_as_of or res.final_as_of}\n\n"
        f"EVIDENCE:\n{_evidence_block(entry)}\n\n"
        'OUTPUT JSON: {"my_probability": <0-1 float>, "my_confidence": "low|medium|high", '
        '"rationale_summary": str, "key_drivers": [str], "reference_classes": [str]}'
    )
    lesson = _lesson_for(res.ticker, lessons)
    reflection = (
        (entry.rationale_summary if entry and entry.rationale_summary else "")
        + ((" Correction: " + lesson) if (lesson and not beat) else "")
    ).strip()
    assistant = {
        "my_probability": target_p,
        "my_confidence": (res.my_confidence or "medium"),
        "rationale_summary": reflection or "Anchor to the reference-class base rate and the crowd.",
        "key_drivers": (entry.key_drivers if entry else []),
        "reference_classes": (entry.reference_classes if entry else []),
    }
    return {
        "_meta": {  # stripped before training; kept for auditing the dataset
            "ticker": res.ticker, "segment": f"{res.category} / {res.subcategory}",
            "beat_market": beat, "outcome": res.outcome,
            "own_prob": own, "market": res.final_market_implied, "target": target_p,
            "label": "reinforce" if beat else "defer-to-crowd",
        },
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
            {"role": "assistant", "content": json.dumps(assistant, ensure_ascii=False)},
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(config.DATA_DIR / "lora"))
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--min-n", type=int, default=20,
                    help="warn (do not fail) when the usable corpus is below this; LoRA on a tiny "
                         "set overfits — the manifest records the count so training can gate on it.")
    args = ap.parse_args()

    resolutions = store.load_resolutions().resolved
    lessons = store.load_lessons().lessons

    examples = []
    skipped = 0
    for res in resolutions:
        record = store.load_forecast(res.ticker)
        ex = build_example(res, record, lessons)
        if ex is None:
            skipped += 1
            continue
        examples.append(ex)

    # Deterministic split by ticker hash so re-runs are stable and a ticker never straddles train/val.
    train, val = [], []
    for ex in examples:
        bucket = _stable_hash(ex["_meta"]["ticker"]) % 100
        (val if bucket < int(args.val_frac * 100) else train).append(ex)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def _dump(path: Path, rows: list[dict]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({"messages": r["messages"]}, ensure_ascii=False) + "\n")

    _dump(out / "sft_train.jsonl", train)
    _dump(out / "sft_val.jsonl", val)
    # Audit sidecar with the _meta (target provenance) for every example.
    with open(out / "dataset_audit.jsonl", "w", encoding="utf-8") as f:
        for r in examples:
            f.write(json.dumps(r["_meta"], ensure_ascii=False) + "\n")

    reinforce = sum(1 for e in examples if e["_meta"]["beat_market"])
    manifest = {
        "schema": "openai-messages",
        "built_at": schemas.utc_now_iso(),
        "n_resolved": len(resolutions),
        "n_examples": len(examples),
        "n_skipped_no_baseline": skipped,
        "n_train": len(train),
        "n_val": len(val),
        "n_reinforce_beat_market": reinforce,
        "n_defer_to_crowd": len(examples) - reinforce,
        "val_frac": args.val_frac,
        "below_min_n": len(examples) < args.min_n,
        "min_n": args.min_n,
        "target_rule": "beat-market -> own committed prob; lost -> market price; no baseline -> skip",
        "leakage_note": "targets never encode the realized outcome directly (see build_lora_dataset.py docstring)",
        "base_model_qwen": config.LOCAL_LLM_MODEL,
        "base_model_mistral": config.LOCAL_LLM_MODEL_MISTRAL,
    }
    store.write_json_atomic(out / "manifest.json", manifest)

    print(json.dumps(manifest, indent=2))
    if manifest["below_min_n"]:
        print(f"\nWARNING: only {len(examples)} usable examples (< min_n={args.min_n}). "
              f"A LoRA on this few will overfit — keep accruing resolutions before training. "
              f"The dataset is written so the pipeline is ready, but DO NOT train yet.", file=sys.stderr)
    print(f"\nWrote {out}/sft_train.jsonl ({len(train)}), sft_val.jsonl ({len(val)}), "
          f"dataset_audit.jsonl, manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
