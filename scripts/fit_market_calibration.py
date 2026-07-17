"""fit_market_calibration.py — fit the per-cell market-calibration map from settled history.

Validates HONESTLY: 70/30 train/test split, fit on train, report out-of-sample Brier of the
corrected price vs the raw market price. Then refit on ALL data and persist (standard: validate
with a split, ship the full-data fit). If the correction does NOT beat raw market out-of-sample,
that is the finding — we say so and do not pretend.

Usage: python3 scripts/fit_market_calibration.py [--in PATH]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json

from lib import config
from lib.atlas import CalibrationMap, stable_hash

DEFAULT_IN = config.DATA_DIR / "history" / "markets.jsonl"


def _stream(path, split=None):
    """Yield usable rows from the corpus, optionally filtered to a hash split.

    STREAMING (B3): the full-universe corpus is ~9M rows — never materialize it.
    split: None (all) | "train" (hash%10 < 7) | "test" (hash%10 >= 7)."""
    for line in Path(path).open():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not (isinstance(r.get("implied_yes"), (int, float)) and r.get("outcome") in (0, 1)):
            continue
        if split is not None:
            h = stable_hash(r.get("ticker", "")) % 10
            if (split == "train") != (h < 7):
                continue
        yield r


def _brier(rows, mapper=None):
    n = 0
    s = 0.0
    for r in rows:
        p = r["implied_yes"]
        if mapper is not None:
            p = mapper.calibrate(r.get("category", "?"), r["implied_yes"],
                                 r.get("open_interest", 0.0), r.get("duration_days"))["calibrated"]
        s += (p - r["outcome"]) ** 2
        n += 1
    return (s / n if n else None), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    args = ap.parse_args()

    # Deterministic 70/30 split, STREAMED (never materialize ~9M rows). md5-based stable_hash,
    # NOT python hash() — the builtin is randomized per process.
    cmap = CalibrationMap.fit(_stream(args.inp, "train"))
    if not cmap.cells:
        print("no corpus — run harvest_history.py first", file=sys.stderr)
        return 2
    raw_b, n_test = _brier(_stream(args.inp, "test"))
    cal_b, _ = _brier(_stream(args.inp, "test"), cmap)
    n_cells = len(cmap.cells)
    print(f"test rows: {n_test}")
    print("\n=== OUT-OF-SAMPLE (test split) ===")
    print(f"  fitted cells (n>=min): {n_cells}")
    print(f"  market raw   Brier: {raw_b:.5f}")
    print(f"  calibrated   Brier: {cal_b:.5f}")
    delta = raw_b - cal_b
    print(f"  improvement: {delta:+.5f}  ({'BEATS raw market' if delta > 0 else 'no improvement'})")

    # Ship the full-data fit regardless (the map is identity where no cell qualifies, so it can
    # only help live where history showed a real, shrunk correction).
    full = CalibrationMap.fit(_stream(args.inp))
    full.save()
    print(f"\nwrote {len(full.cells)} calibrated cells -> {config.DATA_DIR/'history'/'market_calibration.json'}")
    # Show the strongest corrections (largest |b|, the bias term).
    strong = sorted(full.cells.items(), key=lambda kv: -abs(kv[1]["b"]))[:15]
    print("\n=== STRONGEST CELL CORRECTIONS (by bias term b) ===")
    print(f"  {'cell':<34} {'n':<6} {'a':<7} {'b':<7} brier_raw->cal")
    for key, c in strong:
        print(f"  {key:<34} {c['n']:<6} {c['a']:<7} {c['b']:+.3f}  {c['brier_raw']}->{c['brier_cal']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
