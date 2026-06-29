# LoRA fine-tuning the local forecaster (Phase-3 lever, #4)

This is the heaviest learning lever in the project and the **last** one applied, by design. The
cheaper, faster, reversible mechanisms run first and must be exhausted before weights are touched:

1. **Recalibration + shrink-to-market** (`lib/learning.py`) — corrects the forecast *after* the
   model emits it. Recomputed every reconcile.
2. **Error-memory injection** (`lib/error_memory.py`) — feeds each forecaster its most-similar past
   misses *in-context* so it stops repeating errors. No weights change.
3. **LoRA fine-tune** (this doc) — folds the corrected behavior into the model's weights so it
   forecasts better from the first token. Slow, GPU-bound, and only worth it once the resolved
   corpus is large enough that the dataset is not noise.

Nothing in the repo trains a model. `scripts/lora_finetune.py` **never fabricates an adapter** — it
prepares configs and prints the exact recipe; training runs out-of-process on the home RTX GPU with
external tooling that is deliberately not a pipeline dependency.

## Why this can't leak (the target rule)

We do **not** train the model to output the realized outcome — that memorizes labels, not skill, and
would be the worst possible overfit. The target probability encodes the **profitability thesis** the
record proves (`scripts/build_lora_dataset.py`):

| Outcome of the original forecast | Training target | What it teaches |
|---|---|---|
| **Beat the market** (brier_mine < brier_market) | our own committed probability | reinforce reasoning that produced a *real* edge |
| **Lost to the market** | the market-implied price | humility toward the crowd — counters over-fading |
| No market baseline captured | *example skipped* | no defensible target |

The input mirrors inference (question + the key-drivers / reference-classes the forecaster actually
reasoned over — the artifacts we persist). The current export is **~31 examples (10 reinforce / 21
defer-to-crowd)** — consistent with our 10/31 beat rate, and still small. `build_lora_dataset.py`
records `below_min_n` in the manifest, and `lora_finetune.py` **refuses to emit a launch command**
while the corpus is too small to fine-tune without overfitting.

## The pipeline (home GPU)

```
python3 scripts/build_lora_dataset.py        # -> data/lora/{sft_train,sft_val,dataset_audit}.jsonl + manifest.json
python3 scripts/lora_finetune.py             # -> writes llama-factory YAML + dataset_info.json + Ollama Modelfile; prints recipe
```

Then, with a backend installed on the GPU box:

```
pip install llama-factory                                  # one-time (or: pip install unsloth)
llamafactory-cli train  data/lora/llamafactory_<base>.yaml # train the adapter (rank 16, 3 epochs, cosine)
llamafactory-cli export ...                                # merge adapter into the HF base (merge_lora)
python convert_hf_to_gguf.py <merged> && quantize ...      # llama.cpp: HF safetensors -> quantized GGUF
ollama create kalshi-<base>_lora -f data/lora/Modelfile.<base>_lora
```

LoRA trains on the **HF safetensors base** (`Qwen/Qwen3-14B`,
`mistralai/Mistral-Small-24B-Instruct-2501`), not the quantized Ollama GGUF; you re-quantize the
merged weights for Ollama afterward. `lora_finetune.py --launch` will run step 2 automatically *only*
if a backend is importable and the corpus clears `min_n`.

## It earns its place on the scoreboard — or it doesn't ship

A fine-tuned model is **not** trusted because it was trained. Register the new Ollama tag as a fresh
arm in `lib/strategies.py` (e.g. `LQ*-lora`) and let the **bandit + shadow A/B** measure it against
the stock model on *real future resolutions*, exactly as the Mistral-24B arm is being measured now.
If it does not beat the stock model on the held-out record, it is retired. The resolved record is the
only judge.

## Status

- ✅ Dataset builder, leakage-aware target rule, train/val split, audit sidecar.
- ✅ Config generator (llama-factory YAML + dataset_info.json), Ollama Modelfile template, corpus-size gate.
- ⏳ Training **not run** — corpus is small and no backend is installed on this machine. Re-run
  `build_lora_dataset.py` as resolutions accrue; train once the manifest's `below_min_n` is false by
  a comfortable margin (target ≥ ~80–100 examples before expecting LoRA to help more than it hurts).
