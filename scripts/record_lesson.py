#!/usr/bin/env python3
"""Persist a post-mortem lesson to data/lessons.json (ROUTINE step 6b).

The agent calls this after analyzing a resolved market (or a self-review / user
feedback). Lessons accumulate; the SKILL is revised only when a pattern_tag recurs
across >= config.SKILL_REVISION_MIN_PATTERN resolutions — never on one outcome.

Example:
  python3 scripts/record_lesson.py --id 2026-06-02-KXGOVIANOMR-26-RFEE \
    --source resolution --ticker KXGOVIANOMR-26-RFEE --category politics --outcome 1 \
    --final-prob 0.80 --final-market 0.93 --brier-mine 0.04 --brier-market 0.0049 \
    --beat-market false \
    --right "Held on the Trump endorsement + convention mechanic." \
    --wrong "Briefly over-discounted the frontrunner on noisy straw polls." \
    --lesson "In contested primaries, a presidential endorsement dominates late noisy polls." \
    --pattern primary-endorsement-weight
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import store, schemas, config  # noqa: E402


def _opt_float(v):
    return None if v is None else float(v)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--id", required=True, help="Stable id (e.g. <resolved_at>-<ticker>).")
    p.add_argument("--source", default="resolution", choices=["resolution", "self_review", "user_feedback"])
    p.add_argument("--ticker", default="")
    p.add_argument("--category", default="", choices=["", *schemas.CATEGORIES])
    p.add_argument("--outcome", type=int, default=None, choices=[0, 1])
    p.add_argument("--final-prob", dest="final_prob", type=float, default=None)
    p.add_argument("--final-market", dest="final_market", type=float, default=None)
    p.add_argument("--brier-mine", dest="brier_mine", type=float, default=None)
    p.add_argument("--brier-market", dest="brier_market", type=float, default=None)
    p.add_argument("--beat-market", dest="beat_market", default=None, choices=["true", "false"])
    p.add_argument("--right", default="", help="What went right.")
    p.add_argument("--wrong", default="", help="What went wrong.")
    p.add_argument("--lesson", required=True, help="The actionable takeaway.")
    p.add_argument("--pattern", dest="pattern_tag", default="", help="Short tag grouping recurring lessons.")
    p.add_argument("--applied-to-skill", dest="applied", action="store_true")
    args = p.parse_args()

    lesson = schemas.Lesson(
        id=args.id, source=args.source, ticker=args.ticker, category=args.category,
        outcome=args.outcome,
        final_my_probability=_opt_float(args.final_prob),
        final_market_implied=_opt_float(args.final_market),
        brier_mine=_opt_float(args.brier_mine),
        brier_market=_opt_float(args.brier_market),
        beat_market=(None if args.beat_market is None else args.beat_market == "true"),
        what_went_right=args.right, what_went_wrong=args.wrong,
        lesson=args.lesson, pattern_tag=args.pattern_tag, applied_to_skill=args.applied,
    )
    store.append_lesson(lesson)

    counts = store.pattern_counts()
    print(f"Recorded lesson {args.id!r} (pattern={args.pattern_tag or '-'}).")
    if args.pattern_tag:
        n = counts.get(args.pattern_tag, 0)
        if n >= config.SKILL_REVISION_MIN_PATTERN:
            print(f"  ⚠ pattern '{args.pattern_tag}' has recurred {n}x across resolutions "
                  f"(>= {config.SKILL_REVISION_MIN_PATTERN}) — consider revising SKILL.md and "
                  f"marking these lessons applied_to_skill.")
        else:
            print(f"  pattern '{args.pattern_tag}' count = {n} "
                  f"(SKILL revision triggers at {config.SKILL_REVISION_MIN_PATTERN}; do NOT revise on fewer).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
