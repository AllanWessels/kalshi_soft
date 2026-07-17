"""money_path.py — the structural-edge pipeline as ONE command (Workstream E).

The money path is pure API + arithmetic — no LLM anywhere — so it must run on EVERY cycle,
including when local_llm is DOWN (the forecaster is the optional half of the loop, not this).
Sequence (order matters — settle before you screen, screen before you place):

  1. score_recommendations   — settle the signal ledger (conservative column = official)
  2. screen_universe         — B1: screen the entire open exchange into new recs
  3. scan_coherence          — B4: dutch-NO arbs + incoherence flags
  4. paper_broker            — D2: settle fills -> maintain resting orders -> place sized orders

Failure discipline: every step always runs (degrade gracefully — a screen outage must not
stop settlement), each failure is printed loudly, and the exit code is non-zero if ANY step
failed so the run log records a degraded money path instead of silently swallowing it.

Usage: python3 scripts/money_path.py [--max-recs 12] [--fast]
  --fast: smoke mode — skips the full-exchange screen and caps the coherence scan (the two
          long scans); settlement + broker still run for real.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import subprocess
import time

STEPS = [
    ("score", ["python3", "scripts/score_recommendations.py"]),
    ("screen", ["python3", "scripts/screen_universe.py"]),          # --max appended below
    ("coherence", ["python3", "scripts/scan_coherence.py"]),        # --max-events in --fast
    ("broker", ["python3", "scripts/paper_broker.py"]),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the full structural-edge money path")
    ap.add_argument("--max-recs", type=int, default=12,
                    help="per-cycle rec cap passed to screen_universe (exposure, not discovery)")
    ap.add_argument("--fast", action="store_true",
                    help="smoke mode: skip the full-exchange screen, cap the coherence scan")
    args = ap.parse_args()

    failures = []
    t0 = time.time()
    for name, cmd in STEPS:
        cmd = list(cmd)
        if name == "screen":
            if args.fast:
                print("\n### [screen] SKIPPED (--fast smoke mode)")
                continue
            cmd += ["--max", str(args.max_recs)]
        if name == "coherence" and args.fast:
            cmd += ["--max-events", "300"]
        print(f"\n### [{name}] {' '.join(cmd)}")
        t = time.time()
        r = subprocess.run(cmd)
        dur = time.time() - t
        if r.returncode != 0:
            failures.append(f"{name} (exit {r.returncode})")
            print(f"### [{name}] FAILED exit={r.returncode} after {dur:.0f}s "
                  f"— continuing (degrade gracefully, fail loudly)")
        else:
            print(f"### [{name}] ok ({dur:.0f}s)")

    print(f"\n=== MONEY PATH {'DEGRADED' if failures else 'COMPLETE'} "
          f"({time.time() - t0:.0f}s) ===")
    if failures:
        print("FAILED STEPS: " + ", ".join(failures)
              + "  -> record in run_log errors; investigate before the next cycle")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
