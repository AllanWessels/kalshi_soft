"""taxonomy.py — deterministic category + sub-category classification for markets.

The top-level ``category`` (politics / economy / culture / statements) answers
"what domain is this?". The ``subcategory`` answers the finer "what KIND within
that domain?" — e.g. politics → ``us-senate`` vs ``us-governor-primary`` vs
``mayoral``. Per-subcategory scoring is how we learn *where* our forecasting edge
is real and where it is not.

Design
------
* Pure, stdlib-only, side-effect-free, fully unit-testable.
* Classification is a deterministic, ORDERED rule sweep over a haystack built
  from ``ticker + title`` (ticker prefixes like ``KXSENATE``/``KXFEDDECISION``
  are the strongest, most stable signal; title keywords back them up).
* Every market resolves to exactly one subcategory; an unmatched market falls
  back to ``other-<category>`` so aggregates never silently drop rows.

Public API
----------
classify_subcategory(ticker, title, category) -> str
classify(ticker, title, category_hint="") -> tuple[str, str]   # (category, subcategory)
SUBCATEGORIES: dict[str, tuple[str, ...]]                       # category -> ordered subcats
"""

from __future__ import annotations

from typing import Optional

from lib import config

# ---------------------------------------------------------------------------
# Rule tables.  Each entry: (subcategory, (keyword/prefix substrings...)).
# Matched in order against a lowercased "<ticker> <title>" haystack; first hit
# wins.  Ticker fragments are lowercased here so e.g. "kxsenate" matches the
# ticker prefix and "senate" matches the title — both are folded into one list.
# ---------------------------------------------------------------------------

# Foreign-locale signals — used to split US races from overseas ones.
_FOREIGN_SIGNALS: tuple[str, ...] = (
    "seoul", "peru", "peruvian", "canada", "canadian", "mexico", "mexican",
    "united kingdom", "british", "france", "french", "germany", "german",
    "japan", "japanese", "korea", "korean", "india", "indian", "brazil",
    "argentina", "australia", "ireland", "irish", "israel", "israeli",
    "parliament", "prime minister", "chancellor",
)

_POLITICS_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Foreign races first (so a "Seoul mayor" doesn't land in US mayoral).
    ("foreign-election", _FOREIGN_SIGNALS),
    ("foreign-policy", (
        "nuclear deal", "agreement", "treaty", "ceasefire", "sanction",
        "diplomat", "summit", "hormuz", "nato", "annex",
    )),
    # US offices. (Gubernatorial primary-vs-general is split earlier, in
    # _refine_governor, because a flat keyword sweep can't express it.)
    ("us-senate", ("kxsenate", "senate")),
    ("us-house", ("house of representatives", "congressional district", "kxhouse")),
    ("us-president", ("president", "presidential", "kxpres", "white house")),
    ("mayoral", ("mayor", "kxmayor", "kxlamayor")),
    ("primary-nomination", ("primary", "nominee", "nomination", "nomr")),
    ("appointments", (
        "confirm", "cabinet", "secretary of", "supreme court", "nominate",
        "appoint", "ambassador",
    )),
    ("legislation", (
        "bill", "legislation", "shutdown", "debt ceiling", "veto",
        "referendum", "ballot measure",
    )),
    ("approval", ("approval rating", "approve", "favorability")),
)

_ECONOMY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fed-rates", (
        "kxfeddecision", "fed ", "fomc", "federal reserve", "interest rate",
        "rate cut", "rate hike", "jerome powell", "basis point", "bps",
    )),
    ("inflation-cpi", ("cpi", "inflation", "pce", "core price")),
    ("jobs-unemployment", (
        "kxu3", "unemployment", "jobs report", "payroll", "nonfarm", "jobless",
    )),
    ("gdp-growth", ("gdp", "gross domestic", "economic growth")),
    ("recession", ("recession",)),
    ("trade-tariffs", ("tariff", "trade deal", "import duty")),
)

_CULTURE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("awards", (
        "oscar", "academy award", "emmy", "grammy", "tony award", "golden globe",
        "best picture", "nobel", "pulitzer", "time person of the year",
    )),
    ("box-office", ("box office", "rotten tomatoes", "opening weekend", "gross")),
    ("music-charts", ("billboard", "spotify", "album", "chart", "number one single")),
    ("streaming", ("netflix", "streaming", "hbo", "disney+")),
)

_STATEMENTS_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("social-post", ("tweet", "post on", "truth social", "will post", "will tweet")),
    ("public-mention", ("mention", "will say", "says", "word", "utter")),
    ("announcement", ("announce", "press conference", "statement", "interview")),
)

_RULES_BY_CATEGORY: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "politics": _POLITICS_RULES,
    "economy": _ECONOMY_RULES,
    "culture": _CULTURE_RULES,
    "statements": _STATEMENTS_RULES,
}

# Ordered reference list of every subcategory we can emit, per category
# (the trailing ``other-<cat>`` fallback is appended programmatically).
SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    cat: tuple(name for name, _ in rules) + (f"other-{cat}",)
    for cat, rules in _RULES_BY_CATEGORY.items()
}


def _refine_governor(haystack: str) -> Optional[str]:
    """Disambiguate gubernatorial markets: primary/nomination vs general."""
    if "kxgov" not in haystack and "governor" not in haystack:
        return None
    if any(k in haystack for k in ("nomr", "primary", "nominee", "nomination")):
        return "us-governor-primary"
    return "us-governor"


def classify_subcategory(ticker: str, title: str, category: str) -> str:
    """Return the subcategory slug for a market within its top-level *category*.

    Falls back to ``other-<category>`` when nothing matches, and to
    ``"uncategorized"`` when *category* is not one we recognise.
    """
    cat = (category or "").strip().lower()
    rules = _RULES_BY_CATEGORY.get(cat)
    if rules is None:
        return "uncategorized"

    haystack = f"{ticker or ''} {title or ''}".lower()

    # Governor needs primary-vs-general disambiguation that a flat keyword
    # sweep can't express, so handle it explicitly before the generic sweep.
    if cat == "politics":
        gov = _refine_governor(haystack)
        if gov is not None:
            return gov

    for subcat, needles in rules:
        if any(n in haystack for n in needles):
            return subcat

    return f"other-{cat}"


def classify(ticker: str, title: str, category_hint: str = "") -> tuple[str, str]:
    """Return ``(category, subcategory)``.

    The top-level category reuses :func:`config.classify_category` (so the
    canonical labels and stochastic blocklist stay in one place); if that
    returns ``None`` (blocked / unknown) we fall back to any provided hint, then
    to ``"uncategorized"``. The subcategory is then derived within that category.
    """
    category = config.classify_category(title, category_hint)
    if category is None:
        category = (category_hint or "").strip().lower() or "uncategorized"
    subcategory = classify_subcategory(ticker, title, category)
    return category, subcategory


# ---------------------------------------------------------------------------
# Inline self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cases = [
        # (ticker, title, category, expected_subcategory)
        ("KXSENATENJR-26-VBRA", "Will X win the NJ Senate seat?", "politics", "us-senate"),
        ("KXGOVOKNOMR-26-MMAZ", "Will Mazzei be the Republican nominee for Governor in Oklahoma?", "politics", "us-governor-primary"),
        ("KXGOVCA-26-CBIA", "Will X be the next Governor of California?", "politics", "us-governor"),
        ("KXMAYORLA-26-SPRA", "Will X be elected Mayor of Los Angeles?", "politics", "mayoral"),
        ("KXSEOULMAYOR-26JUN03-CWON", "Will X win the Seoul mayoral election?", "politics", "foreign-election"),
        ("KXPERUPRES-26-RPAL", "Will X win the Peruvian presidential election?", "politics", "foreign-election"),
        ("KXUSAIRANAGREEMENT-27-26JUL", "Will the US agree to a new Iranian nuclear deal?", "politics", "foreign-policy"),
        ("KXFEDDECISION-26JUN-H0", "Will the Federal Reserve Hike rates by 0bps?", "economy", "fed-rates"),
        ("KXU3-26MAY-T4.2", "Will the unemployment rate be 4.2%?", "economy", "jobs-unemployment"),
        ("KXCPI-26-X", "Will CPI inflation exceed 3%?", "economy", "inflation-cpi"),
        ("KXOSCAR-26-BP", "Will X win Best Picture at the Oscars?", "culture", "awards"),
        ("KXWORD-26-T", "Will the President say 'tariff' during the speech?", "statements", "public-mention"),
        ("KXUNKNOWN-26", "Some unmatched politics market", "politics", "other-politics"),
        ("KXWHO-26", "Totally unknown", "", "uncategorized"),
    ]
    errors = []
    for ticker, title, cat, expected in cases:
        got = classify_subcategory(ticker, title, cat)
        if got != expected:
            errors.append(f"  {ticker!r} ({cat}) -> {got!r}, expected {expected!r}")

    # classify() end-to-end: Fed market with no explicit category hint.
    c, s = classify("KXFEDDECISION-26JUN-H0", "Will the Federal Reserve Hike rates by 0bps?")
    if (c, s) != ("economy", "fed-rates"):
        errors.append(f"  classify() fed -> {(c, s)}, expected ('economy', 'fed-rates')")

    if errors:
        print("TAXONOMY TEST FAILURES:")
        print("\n".join(errors))
        raise SystemExit(1)
    print("taxonomy OK")
