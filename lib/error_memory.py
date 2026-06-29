"""error_memory.py — feed each forecaster its most-similar PAST MISSES at forecast time.

The learning policy (lib/learning.py) corrects a forecast AFTER the model produces it
(recalibration + shrink-to-market). This module closes the loop EARLIER — in-context, while
the model is still reasoning — by surfacing the specific, concrete ways we got similar
questions WRONG before. It is the cheapest, highest-leverage learning mechanism in an LLM
pipeline (no weights change; the model conditions on its own track record), and it directly
attacks the diagnosis: we lose to the market by repeating avoidable reasoning errors
(over-fading liquid markets, base-rate neglect, over-updating on one vivid signal).

A "miss" is a resolved market where we did NOT beat the market price (brier_mine >= brier_market,
or a wrong-side committed forecast). We rank candidate misses by similarity to the live question
(token overlap on the title + a bonus for the same taxonomy segment), attach the post-mortem
lesson takeaway when one exists, and return the top K as compact bullet strings the forecaster
prompt can include verbatim.

Deliberately dependency-free (no embeddings): the resolved corpus is small (tens of markets), so
lexical Jaccard + segment match is both sufficient and fully auditable. Swap in embeddings later
only if the corpus grows enough to need it.
"""
from __future__ import annotations

import re
from typing import Optional

from . import store, taxonomy

# Common words that carry no topical signal — excluded from the similarity tokens so
# "Will the ..." boilerplate doesn't manufacture false matches between unrelated markets.
_STOP = {
    "will", "the", "a", "an", "to", "of", "in", "on", "for", "by", "be", "is", "are",
    "at", "and", "or", "this", "that", "with", "as", "it", "before", "after", "than",
    "have", "has", "do", "does", "win", "any", "from", "into", "over", "under", "what",
    "who", "when", "which", "during", "yes", "no",
}
SEGMENT_BONUS = 0.25   # added to similarity when the candidate shares the live segment
MIN_SIM = 0.08         # floor: below this the match is noise — don't inject it


def _tokens(text: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {t for t in toks if len(t) > 2 and t not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _is_miss(r) -> bool:
    """A resolution we should LEARN from: we failed to beat the market on it. Falls back to
    a plain Brier threshold when the market price wasn't captured (still a clear miss)."""
    if r.brier_mine is None:
        return False
    if r.brier_market is not None:
        return r.brier_mine >= r.brier_market   # did not beat the price
    return r.brier_mine > 0.25                   # no market baseline: wrong-ish on its own


def _segment(category: str, subcategory: str) -> str:
    return f"{category} / {subcategory}" if subcategory else (category or "?")


def recall(question: str, segment: str = "", *, k: int = 3,
           resolutions=None, lessons=None) -> list[dict]:
    """Return up to ``k`` most-similar past MISSES for ``question`` (optionally biased toward
    ``segment``). Each item: ``{ticker, title, segment, sim, my_prob, market, outcome,
    brier_mine, brier_market, lesson, pattern_tag}``. Pure ranking — no I/O if both stores
    are passed in (keeps it cheap to call once per due market)."""
    if resolutions is None:
        resolutions = store.load_resolutions().resolved
    if lessons is None:
        lessons = store.load_lessons().lessons
    lesson_by_ticker: dict[str, object] = {}
    for l in lessons:                      # last lesson per ticker wins (most recent post-mortem)
        if l.ticker:
            lesson_by_ticker[l.ticker] = l

    qtok = _tokens(question)
    scored = []
    for r in resolutions:
        if not _is_miss(r):
            continue
        seg = _segment(r.category, r.subcategory)
        sim = _jaccard(qtok, _tokens(r.title))
        if segment and seg == segment:
            sim += SEGMENT_BONUS
        if sim < MIN_SIM:
            continue
        l = lesson_by_ticker.get(r.ticker)
        scored.append({
            "ticker": r.ticker,
            "title": r.title,
            "segment": seg,
            "sim": round(sim, 3),
            "my_prob": r.commit_probability if r.commit_probability is not None else r.final_my_probability,
            "market": r.final_market_implied,
            "outcome": r.outcome,
            "brier_mine": r.brier_mine,
            "brier_market": r.brier_market,
            "lesson": (getattr(l, "lesson", "") or "") if l else "",
            "pattern_tag": (getattr(l, "pattern_tag", "") or "") if l else "",
        })
    scored.sort(key=lambda d: d["sim"], reverse=True)
    return scored[:k]


def as_prompt_block(misses: list[dict]) -> str:
    """Render recalled misses as a compact prompt section the forecaster can read verbatim.
    Returns "" when there is nothing to inject (so callers can append unconditionally)."""
    if not misses:
        return ""
    lines = [
        "LESSONS FROM YOUR OWN PAST MISSES on similar questions. These are markets where your "
        "earlier forecast FAILED TO BEAT THE PRICE. Do not repeat the same error; if you are "
        "about to fade the market again, justify why THIS case differs:",
    ]
    for m in misses:
        out = "YES" if m["outcome"] == 1 else "NO"
        mk = "n/a" if m["market"] is None else f"{m['market']:.2f}"
        mp = "n/a" if m["my_prob"] is None else f"{m['my_prob']:.2f}"
        tail = f" Lesson: {m['lesson']}" if m["lesson"] else ""
        lines.append(
            f"- [{m['segment']}] \"{m['title']}\" — you said {mp}, market {mk}, resolved {out}."
            f"{tail}"
        )
    return "\n".join(lines)


def recall_block(question: str, category: str = "", title_for_segment: str = "",
                 ticker: str = "", *, k: int = 3,
                 resolutions=None, lessons=None) -> str:
    """Convenience: classify the segment, recall misses, and render the prompt block in one
    call. Safe — any failure returns "" so it can never break a forecast."""
    try:
        seg = ""
        if category:
            sub = taxonomy.classify_subcategory(ticker, title_for_segment or question, category)
            seg = _segment(category, sub)
        return as_prompt_block(recall(question, seg, k=k, resolutions=resolutions, lessons=lessons))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Inline self-test (synthetic; no live data needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    errs = []
    def chk(name, cond):
        if not cond: errs.append(name)

    class R:
        def __init__(s, tk, title, cat="politics", sub="mayoral", bm=0.3, bk=0.1,
                     y=0, mp=0.7, mi=0.3, commit=None):
            s.ticker=tk; s.title=title; s.category=cat; s.subcategory=sub
            s.brier_mine=bm; s.brier_market=bk; s.outcome=y
            s.final_my_probability=mp; s.final_market_implied=mi; s.commit_probability=commit

    class L:
        def __init__(s, tk, lesson, tag=""):
            s.ticker=tk; s.lesson=lesson; s.pattern_tag=tag

    res = [
        R("MAYOR-NYC", "Will the NYC mayoral race be won by the incumbent", bm=0.4, bk=0.1),  # miss
        R("MAYOR-LA", "Will the Los Angeles mayoral election go to a runoff", bm=0.05, bk=0.2),  # beat market -> not a miss
        R("CPI-JUN", "Will CPI inflation exceed 3 percent in June", cat="economy", sub="cpi", bm=0.5, bk=0.1),  # miss, other segment
    ]
    les = [L("MAYOR-NYC", "Stop fading liquid down-ballot favorites without a specific catalyst.", "fade-favorite")]

    hits = recall("Will the mayoral election in Boston be won by the incumbent",
                  "politics / mayoral", k=3, resolutions=res, lessons=les)
    chk("returns_misses_only", all(h["ticker"] != "MAYOR-LA" for h in hits))
    chk("most_similar_first", hits and hits[0]["ticker"] == "MAYOR-NYC")
    chk("attaches_lesson", hits and "fading liquid" in hits[0]["lesson"])
    chk("segment_bonus_helps", hits[0]["sim"] >= SEGMENT_BONUS)

    block = as_prompt_block(hits)
    chk("block_mentions_market_and_lesson", "market" in block.lower() and "Lesson:" in block)
    chk("empty_when_no_hits", as_prompt_block([]) == "")

    # no overlap at all -> nothing injected (noise floor)
    none = recall("Will SpaceX launch Starship to orbit", "space / launch", k=3,
                  resolutions=res, lessons=les)
    chk("noise_floor_filters", none == [])

    if errs:
        print("ERROR_MEMORY TEST FAILURES:", ", ".join(errs)); raise SystemExit(1)
    print("error_memory OK")
