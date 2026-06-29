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
(1) assemble the blind input packet, (2) run the local critic (deferred if local is
down — never a same-family fallback), (3) persist the final adversarial Lesson, and
(4) AUTONOMOUSLY revise the SKILL's auto-maintained heuristics from the resolved record
(`revise-skill`, no human gate — project directive 2026-06-29).

Subcommands
-----------
gather       --ticker T             -> print the blind post-mortem packet (JSON)
critic       --ticker T             -> run the LOCAL blind critic; print verdict JSON
                                       (status="skipped" if the local model is down)
record       --ticker T --lesson ... --judge-verdict ...   -> append the adversarial Lesson
patterns                            -> list pattern_tag recurrence (advisory annotation)
revise-skill                        -> AUTONOMOUS: Qwen re-drafts the auto-maintained
                                       heuristics block in SKILL.md from the resolved record

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
    """Run the LOCAL blind critic. If the local model is unavailable, SKIP and DEFER —
    do NOT fall back to a same-family (Opus) critic. The adversary's whole value is being
    a different model family than the Opus forecaster; an Opus-critiques-Opus pass is just
    self-judging dressed up, which is exactly what this panel exists to avoid. So on
    fallback we emit status="skipped", record nothing, and let the post-mortem run next
    time the local model is up (the market stays flagged as un-reviewed)."""
    packet = build_packet(args.ticker)
    if not config.local_llm_enabled() or not local_llm.ping(timeout=3.0):
        print(json.dumps({
            "status": "skipped",
            "reason": "local model down/disabled — adversarial post-mortem DEFERRED. Do NOT "
                      "run a same-family (Opus) critic; that would be self-judging. Re-run when "
                      "local_llm is UP.",
            "ticker": args.ticker,
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
            "status": "skipped",
            "reason": f"local critic error: {e} — adversarial post-mortem DEFERRED (no "
                      "same-family fallback). Re-run when local_llm is UP.",
            "ticker": args.ticker,
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
        print(f"  pattern '{args.pattern_tag}' count = {n}. "
              f"Run `postmortem.py revise-skill` to fold lessons into SKILL.md autonomously.")
    return 0


# ---------------------------------------------------------------------------
# Autonomous SKILL revision (no human gate) — ROUTINE Step 6b tail.
# ---------------------------------------------------------------------------

def _read_skill() -> str:
    return config.SKILL_PATH.read_text(encoding="utf-8")


def _splice_auto_section(skill_text: str, new_block: str) -> str:
    """Replace the content between the AUTO-HEURISTICS markers. If the markers are
    missing, append a fresh fenced section at the end of the file."""
    begin, end = config.SKILL_AUTO_BEGIN, config.SKILL_AUTO_END
    fenced = f"{begin}\n{new_block}\n{end}"
    if begin in skill_text and end in skill_text:
        pre = skill_text[: skill_text.index(begin)]
        post = skill_text[skill_text.index(end) + len(end):]
        return pre + fenced + post
    return (skill_text.rstrip() + "\n\n### Learned heuristics (auto-maintained)\n"
            + fenced + "\n")


def cmd_revise_skill(args) -> int:
    """AUTONOMOUS SKILL revision: have Qwen re-draft the auto-maintained heuristics block
    from the resolved-market lessons, write it into SKILL.md, and mark those lessons
    applied. No human gate (project directive 2026-06-29). Skips quietly if the local
    model is down or there are no lessons."""
    lf = store.load_lessons()
    lessons = lf.lessons
    if not lessons:
        print("revise-skill: no lessons yet — nothing to fold in.")
        return 0
    if not config.local_llm_enabled() or not local_llm.ping(timeout=3.0):
        print("revise-skill: local model down — SKILL revision DEFERRED to a later run.")
        return 0

    skill_text = _read_skill()
    begin, end = config.SKILL_AUTO_BEGIN, config.SKILL_AUTO_END
    current = ""
    if begin in skill_text and end in skill_text:
        current = skill_text[skill_text.index(begin) + len(begin): skill_text.index(end)].strip()

    lesson_dicts = [{
        "pattern_tag": l.pattern_tag, "category": l.category,
        "what_went_right": l.what_went_right, "what_went_wrong": l.what_went_wrong,
        "lesson": l.lesson, "judge_verdict": l.judge_verdict,
        "beat_market": l.beat_market,
    } for l in lessons]

    try:
        draft = local_llm.revise_skill(current, lesson_dicts)
    except local_llm.LocalLLMError as e:
        print(f"revise-skill: Qwen draft failed ({e}) — SKILL unchanged this run.")
        return 0

    heuristics = draft.get("heuristics", [])
    if not heuristics:
        print("revise-skill: Qwen proposed no heuristics — SKILL unchanged.")
        return 0
    block = "\n".join(f"- {h}" for h in heuristics)
    block += (f"\n\n_Auto-maintained by `postmortem.py revise-skill` from "
              f"{len(lessons)} resolved-market lesson(s); reversible via git._")

    new_text = _splice_auto_section(skill_text, block)
    if new_text == skill_text:
        print("revise-skill: heuristics unchanged — SKILL left as-is.")
        return 0
    config.SKILL_PATH.write_text(new_text, encoding="utf-8")

    # Mark every lesson folded in (so `patterns` shows them as applied).
    for l in lf.lessons:
        l.applied_to_skill = True
    store.save_lessons(lf)

    print(f"revise-skill: SKILL.md updated — {len(heuristics)} heuristic(s). "
          f"{draft.get('rationale','')}")
    for h in heuristics:
        print(f"  - {h}")
    return 0


def cmd_patterns(args) -> int:
    """Advisory pattern recurrence view. SKILL revision is now AUTONOMOUS (run
    `revise-skill`); this is just a recurrence annotation, no longer a gate."""
    counts = store.pattern_counts()
    lessons = store.load_lessons().lessons
    applied = {l.pattern_tag for l in lessons if l.applied_to_skill and l.pattern_tag}
    out = {
        "advisory_recurrence_threshold": config.SKILL_REVISION_MIN_PATTERN,
        "pattern_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "already_folded_in": sorted(t for t in applied if t),
        "note": "SKILL revision is AUTONOMOUS (no human gate) — `revise-skill` re-drafts the "
                "auto-maintained heuristics block every run with new lessons.",
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

    pat = sub.add_parser("patterns", help="advisory pattern-recurrence view (no longer a gate)")
    pat.set_defaults(func=cmd_patterns)

    rs = sub.add_parser("revise-skill",
                        help="AUTONOMOUS: Qwen re-drafts the auto-maintained heuristics in SKILL.md")
    rs.set_defaults(func=cmd_revise_skill)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
