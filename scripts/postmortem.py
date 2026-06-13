#!/usr/bin/env python3
"""postmortem.py — adversarial post-mortem panel for a resolved market (ROUTINE 6b).

Replaces the old single-agent self-judging step. The forecaster does NOT grade its
own work; instead a three-role panel does:

    Critic   (LOCAL Qwen, different model family, BLIND to forecaster identity,
              scores a FIXED rubric defined before resolution)  -> this script
    Defender (Claude tier, argues what was right / unforeseeable) -> orchestrator agent
    Judge    (Claude tier, reads critic+defender, issues verdict + lesson) -> orchestrator agent

This script is stdlib + local-LLM only (no Claude SDK): the Claude roles are sub-
agents the Opus orchestrator spawns between subcommands. The script's jobs are to
(1) assemble the blind input packet, (2) run the local critic with graceful Sonnet
fallback, (3) persist the final adversarial Lesson, and (4) surface batch pattern-
mining candidates for human-gated SKILL revision.

Subcommands
-----------
gather   --ticker T                 -> print the blind post-mortem packet (JSON)
critic   --ticker T                 -> run the LOCAL blind critic; print verdict JSON
                                       (status="fallback" if the local model is down)
record   --ticker T --lesson ... --judge-verdict ...   -> append the adversarial Lesson
patterns                            -> list pattern_tags eligible for SKILL revision

Flow (orchestrator):
  packet = gather; critic_out = critic (or spawn Sonnet critic on fallback);
  defender_out = <Claude defender agent>; judge_out = <Claude judge agent>;
  record(... --rubric-scores critic_out --judge-verdict judge_out ...)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import store, schemas, config, local_llm  # noqa: E402


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _resolution_for(ticker: str):
    for r in store.load_resolutions().resolved:
        if r.ticker == ticker:
            return r
    return None


def _reasoning_text(record) -> str:
    """Assemble the forecaster's reasoning from the final forecast entry. Identity
    is never included — the critic is blind to which model/strategy produced it."""
    entry = (record.current if record and record.current else None)
    if entry is None and record and record.history:
        entry = record.history[-1]
    if entry is None:
        return ""
    parts = []
    if entry.rationale_summary:
        parts.append(entry.rationale_summary)
    if entry.key_drivers:
        parts.append("Key drivers: " + "; ".join(entry.key_drivers))
    if entry.reference_classes:
        parts.append("Reference classes: " + "; ".join(entry.reference_classes))
    return "\n".join(parts)


def build_packet(ticker: str) -> dict:
    """Build the BLIND post-mortem packet for a resolved market. No forecaster
    identity, no strategy_id — the critic must judge reasoning, not reputation."""
    res = _resolution_for(ticker)
    if res is None:
        raise SystemExit(f"no resolution found for ticker {ticker!r}")
    record = store.load_forecast(ticker)
    return {
        "ticker": ticker,
        "question": res.title or (record.title if record else ticker),
        "category": res.category,
        "outcome": res.outcome,
        "forecast_prob": res.final_my_probability,
        "market_implied": res.final_market_implied,
        "brier_mine": res.brier_mine,
        "brier_market": res.brier_market,
        "reasoning": _reasoning_text(record),
        "rubric": list(config.POSTMORTEM_RUBRIC),
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_gather(args) -> int:
    print(json.dumps(build_packet(args.ticker), indent=2))
    return 0


def cmd_critic(args) -> int:
    """Run the LOCAL blind critic. On any local-model failure, emit a fallback
    marker (status="fallback") and exit 0 so the orchestrator spawns a Sonnet critic
    sub-agent instead of hard-failing the run."""
    packet = build_packet(args.ticker)
    if not config.local_llm_enabled() or not local_llm.ping(timeout=3.0):
        print(json.dumps({
            "status": "fallback",
            "reason": "local LLM disabled or unreachable; spawn a Sonnet critic agent",
            "packet": packet,
        }, indent=2))
        return 0
    try:
        verdict = local_llm.critique(
            question=packet["question"],
            forecast_prob=packet["forecast_prob"],
            reasoning=packet["reasoning"],
            outcome=packet["outcome"],
            market_implied=packet["market_implied"],
        )
        verdict["status"] = "ok"
        verdict["critic_model"] = config.LOCAL_LLM_MODEL
        print(json.dumps(verdict, indent=2))
    except local_llm.LocalLLMError as e:
        print(json.dumps({
            "status": "fallback",
            "reason": f"local critic error: {e}; spawn a Sonnet critic agent",
            "packet": packet,
        }, indent=2))
    return 0


def _load_json_arg(value: "str | None") -> dict:
    """Accept inline JSON or a path to a JSON file (orchestrator convenience)."""
    if not value:
        return {}
    p = Path(value)
    text = p.read_text(encoding="utf-8") if p.exists() else value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def cmd_record(args) -> int:
    """Persist the final adversarial Lesson after the panel has run."""
    res = _resolution_for(args.ticker)
    rubric_scores = _load_json_arg(args.rubric_scores)
    lesson = schemas.Lesson(
        id=args.id or (f"{res.resolved_at}-{args.ticker}" if res else args.ticker),
        source="resolution",
        ticker=args.ticker,
        category=(res.category if res else ""),
        outcome=(res.outcome if res else None),
        final_my_probability=(res.final_my_probability if res else None),
        final_market_implied=(res.final_market_implied if res else None),
        brier_mine=(res.brier_mine if res else None),
        brier_market=(res.brier_market if res else None),
        beat_market=(
            None if not res or res.brier_mine is None or res.brier_market is None
            else res.brier_mine < res.brier_market
        ),
        what_went_right=args.right,
        what_went_wrong=args.wrong,
        lesson=args.lesson,
        pattern_tag=args.pattern_tag,
        critic_model=args.critic_model,
        rubric_scores=rubric_scores,
        judge_verdict=args.judge_verdict,
        disagreement=args.disagreement,
    )
    store.append_lesson(lesson)
    counts = store.pattern_counts()
    print(f"Recorded adversarial lesson {lesson.id!r} "
          f"(critic={args.critic_model or '-'}, pattern={args.pattern_tag or '-'}).")
    if args.pattern_tag:
        n = counts.get(args.pattern_tag, 0)
        gate = config.SKILL_REVISION_MIN_PATTERN
        if n >= gate:
            print(f"  ⚠ pattern '{args.pattern_tag}' recurred {n}x (>= {gate}) — "
                  f"eligible for human-gated SKILL.md revision (run: postmortem.py patterns).")
        else:
            print(f"  pattern '{args.pattern_tag}' count = {n} "
                  f"(SKILL revision triggers at {gate}; never revise on fewer).")
    return 0


def cmd_patterns(args) -> int:
    """Batch pattern-mining: pattern_tags that recur >= threshold and are not yet
    folded into the SKILL. These are PROPOSALS — SKILL edits stay human-gated."""
    counts = store.pattern_counts()
    gate = config.SKILL_REVISION_MIN_PATTERN
    lessons = store.load_lessons().lessons
    applied = {l.pattern_tag for l in lessons if l.applied_to_skill and l.pattern_tag}
    eligible = {
        tag: n for tag, n in counts.items()
        if tag and n >= gate and tag not in applied
    }
    out = {
        "skill_revision_min_pattern": gate,
        "eligible_patterns": dict(sorted(eligible.items(), key=lambda kv: -kv[1])),
        "note": "PROPOSALS only — SKILL.md revision is human-gated; mark lessons "
                "applied_to_skill once folded in.",
    }
    print(json.dumps(out, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gather", help="print the blind post-mortem packet")
    g.add_argument("--ticker", required=True)
    g.set_defaults(func=cmd_gather)

    c = sub.add_parser("critic", help="run the local blind critic (fallback marker if down)")
    c.add_argument("--ticker", required=True)
    c.set_defaults(func=cmd_critic)

    r = sub.add_parser("record", help="append the final adversarial Lesson")
    r.add_argument("--ticker", required=True)
    r.add_argument("--id", default="")
    r.add_argument("--lesson", required=True, help="the actionable takeaway")
    r.add_argument("--right", default="", help="what went right")
    r.add_argument("--wrong", default="", help="what went wrong")
    r.add_argument("--pattern", dest="pattern_tag", default="")
    r.add_argument("--critic-model", dest="critic_model", default="",
                   help="model that produced the critique (local tag or 'sonnet-fallback')")
    r.add_argument("--rubric-scores", dest="rubric_scores", default="",
                   help="critic rubric_scores as inline JSON or a path to a JSON file")
    r.add_argument("--judge-verdict", dest="judge_verdict", default="")
    r.add_argument("--disagreement", default="",
                   help="where critic and defender diverged")
    r.set_defaults(func=cmd_record)

    pat = sub.add_parser("patterns", help="list pattern_tags eligible for SKILL revision")
    pat.set_defaults(func=cmd_patterns)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
