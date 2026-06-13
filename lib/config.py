"""Configuration: paths, thresholds, secrets, and category classification.

Second shared contract (after ``schemas.py``). Every module imports paths and
constants from here. Stdlib-only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from . import schemas

# ---------------------------------------------------------------------------
# Paths (repo root = parent of the lib/ directory)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
FORECASTS_DIR = DATA_DIR / "forecasts"
REPORTS_DIR = REPO_ROOT / "reports"
ARCHIVE_DIR = REPORTS_DIR / "archive"
SCRATCH_DIR = DATA_DIR / "scratch"          # gitignored chart intermediates

WATCHLIST_PATH = DATA_DIR / "watchlist.json"
RESOLUTIONS_PATH = DATA_DIR / "resolutions.json"
CALIBRATION_PATH = DATA_DIR / "calibration.json"
CANDIDATES_PATH = DATA_DIR / "candidates.json"
LESSONS_PATH = DATA_DIR / "lessons.json"
RUN_LOG_PATH = DATA_DIR / "run_log.jsonl"
DB_PATH = DATA_DIR / "forecasts.db"          # SQLite analysis mirror (gitignored, rebuilt from JSON)

# Skill self-revision: only fold a lesson into SKILL.md once the same pattern_tag
# recurs across at least this many resolved markets (never on a single outcome).
SKILL_REVISION_MIN_PATTERN = 3
LATEST_PDF_PATH = REPORTS_DIR / "latest.pdf"


def forecast_path(ticker: str) -> Path:
    """Path to a single market's forecast record. Ticker is sanitized for FS use."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", ticker)
    return FORECASTS_DIR / f"{safe}.json"


def ensure_dirs() -> None:
    for d in (DATA_DIR, FORECASTS_DIR, REPORTS_DIR, ARCHIVE_DIR, SCRATCH_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Kalshi API
# ---------------------------------------------------------------------------

KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_FALLBACK_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Basic tier ~20 read req/s; stay well under.
KALSHI_MAX_REQ_PER_SEC = 8
KALSHI_TIMEOUT_S = 20
KALSHI_MAX_RETRIES = 4

# Kalshi trading fee: ceil(FEE_RATE * contracts * price * (1 - price)) per contract,
# rounded up to the next cent. 0.07 is the standard retail rate; some series differ
# (series metadata carries a fee_multiplier we could read later). Settlement is free.
KALSHI_FEE_RATE = 0.07

# Minimum net expected profit per contract (in dollars, after fees + crossing the
# spread) for a market to count as a profitable/tradable lean. Below this we record
# lean=NONE regardless of raw edge.
MIN_PROFITABLE_EV = 0.02

# Confidence gate on leans: a positive-EV lean is only ACTIONABLE if my epistemic
# confidence backs it. EV is computed from my probability as if it were truth, so a
# low-confidence estimate that disagrees with a liquid market is more likely my error
# than real edge. Rules (see scoring.confidence_gate):
#   - confidence "low"  -> never an actionable lean (probability too shaky to fade the crowd)
#   - gap = |my_prob - market_implied| > MAX_MARKET_DISAGREEMENT and confidence != "high"
#       -> treat as probable model error -> no actionable lean
MAX_MARKET_DISAGREEMENT = 0.20

# ---------------------------------------------------------------------------
# Watchlist / discovery thresholds
# ---------------------------------------------------------------------------

WATCHLIST_CAP = 20

# Tuned so a healthy pool (~tens) of soft markets passes, which the agent then
# curates down to WATCHLIST_CAP. Soft markets are thinner than sports/crypto, so
# these are deliberately permissive; the agent applies judgment on top.
MIN_VOLUME_24H = 100            # contracts traded in last 24h
MIN_OPEN_INTEREST = 500         # open contracts
MAX_SPREAD_CENTS = 12           # yes_ask - yes_bid, in cents
MIN_DAYS_TO_CLOSE = 1           # skip markets about to close (no time to forecast)
# Deployment strategy: only track markets that SETTLE within ~1 month, keyed off
# expected_expiration_time (the true resolution date), not the trading close_time.
# Near-term resolvers give fast calibration and actionable trades.
MAX_DAYS_TO_RESOLVE = 31


def first_run_max() -> Optional[int]:
    """Optional cap on the heavier bootstrap run (env FIRST_RUN_MAX)."""
    v = os.environ.get("FIRST_RUN_MAX", "").strip()
    return int(v) if v.isdigit() else None


# ---------------------------------------------------------------------------
# Tiered re-forecast cadence (deterministic; used by due_for_reforecast.py)
# ---------------------------------------------------------------------------

def cadence_days_for(days_to_close: float) -> float:
    """Baseline re-forecast cadence as a function of days until the market closes.

    With 3 runs/day, a cadence < ~0.33 days means "every run".
    """
    if days_to_close <= 1:
        return 0.0     # every run
    if days_to_close <= 7:
        return 0.0     # every run (near-close)
    if days_to_close <= 30:
        return 3.0
    return 7.0


# ---------------------------------------------------------------------------
# Model routing + local open-weight LLM (cost unlock + adversarial critic)
# ---------------------------------------------------------------------------
# The forecasting/judging tiers stay on Claude; raw web retrieval and the blind
# adversarial post-mortem critic run on a LOCAL open-weight model (different model
# family => kills self-preference bias; free + unmetered on the RTX 5080 box).
# Base URL is env-overridable so a tunnel / hosted endpoint is a drop-in later.
LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1").strip()
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen3:14b-instruct-q4_K_M").strip()
LOCAL_LLM_TIMEOUT_S = 60
LOCAL_LLM_API_KEY = os.environ.get("LOCAL_LLM_API_KEY", "ollama").strip()  # Ollama ignores it

# Claude tiers per role (lifted out of markdown prose into code). The orchestrator
# reads these to decide which model each step's sub-agents use.
MODEL_FORECASTER = "sonnet"   # the N independent forecasters (strategy arms)
MODEL_CRITIC = "local"        # blind adversarial critic -> local Qwen (different family)
MODEL_DEFENDER = "sonnet"     # argues what was right / unforeseeable
MODEL_JUDGE = "opus"          # reads critic+defender, issues verdict + lesson

# Post-mortem panel rubric: fixed BEFORE resolution so the judge can't retrofit
# "good reasoning" onto a lucky/unlucky outcome.
POSTMORTEM_RUBRIC = (
    "base_rate_established",      # did the forecast anchor on an explicit base rate?
    "three_independent_sources",  # >=3 genuinely independent pieces of evidence?
    "confidence_interval",        # was forecaster uncertainty considered, not just a point?
    "market_divergence_justified",  # if we faded the market, was the divergence reasoned?
)


def local_llm_enabled() -> bool:
    """Whether the local-LLM tier is opted in (env LOCAL_LLM_ENABLED, default on).

    Lets the operator force the Sonnet-fallback path (e.g. GPU-less context) without
    editing code. Any of 0/false/no/off disables it."""
    v = os.environ.get("LOCAL_LLM_ENABLED", "1").strip().lower()
    return v not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def load_secrets() -> tuple[Optional[str], Optional[str]]:
    """Return (key_id, private_key_pem) from the environment, or (None, None).

    Public market-data reads need no auth, so absence is not fatal. A private key
    provided with literal ``\\n`` escapes (single-env-var form) is normalized to
    real newlines. Never logs key material.
    """
    _load_dotenv_if_present()
    key_id = os.environ.get("KALSHI_KEY_ID", "").strip() or None
    pem = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if pem and "\\n" in pem and "\n" not in pem.strip():
        pem = pem.replace("\\n", "\n")
    pem = pem.strip() or None
    return key_id, pem


def _load_dotenv_if_present() -> None:
    """Minimal .env loader (no python-dotenv dependency). Does not override
    variables already set in the environment (Routine env wins)."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    # Support multi-line PEM values wrapped in single/double quotes.
    for key, val in _parse_dotenv(text):
        os.environ.setdefault(key, val)


def _parse_dotenv(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        s = line.strip()
        if not s or s.startswith("#") or "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        val = rest.lstrip()
        if val[:1] in ("'", '"'):
            quote = val[0]
            val = val[1:]
            # accumulate until closing quote (handles multi-line PEM)
            buf = []
            if quote in val:
                val = val[: val.index(quote)]
                pairs.append((key, val))
                continue
            buf.append(val)
            while i < len(lines):
                nxt = lines[i]
                i += 1
                if quote in nxt:
                    buf.append(nxt[: nxt.index(quote)])
                    break
                buf.append(nxt)
            pairs.append((key, "\n".join(buf)))
        else:
            pairs.append((key, val.split(" #", 1)[0].strip()))
    return pairs


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------
# Kalshi's own series.category strings are not documented/stable, so we classify
# from the market title + any provided category hint onto OUR canonical labels.
# Returning None means "not a soft market we forecast" (filtered out).

_STOCHASTIC_BLOCKLIST = (
    "bitcoin", "btc", "ethereum", "eth", "crypto", "dogecoin", "solana",
    "nfl", "nba", "mlb", "nhl", "ncaa", "soccer", "premier league", "ufc",
    "super bowl", "world series", "stanley cup", "golf", "tennis", "f1",
    "temperature", "high temp", "rainfall", "hurricane category",
    "close above", "close below", "price of", "s&p 500 close", "nasdaq close",
    "dow close", "eth/usd", "btc/usd",
)

_CATEGORY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("politics", (
        "election", "senate", "house of representatives", "president", "presidential",
        "governor", "primary", "nominee", "nomination", "congress", "approval rating",
        "cabinet", "supreme court", "impeach", "shutdown", "legislation", "speaker of",
        "secretary of", "confirm", "electoral", "ballot", "referendum", "parliament",
        "prime minister", "chancellor",
    )),
    ("economy", (
        "fed ", "fomc", "interest rate", "rate cut", "rate hike", "federal reserve",
        "cpi", "inflation", "jobs report", "unemployment rate", "recession",
        "gdp", "jerome powell", "debt ceiling", "tariff",
    )),
    ("culture", (
        "oscar", "academy award", "emmy", "grammy", "box office", "billboard",
        "album", "movie", "film", "rotten tomatoes", "golden globe", "best picture",
        "rotten", "tony award", "spotify", "netflix", "streaming", "tv show",
        "celebrity", "time person of the year", "nobel",
    )),
    ("statements", (
        "will say", "says", "mention", "tweet", "post on", "truth social",
        "announce", "statement", "press conference", "interview", "word",
        "will post", "will tweet",
    )),
)


def classify_category(title: str, category_hint: str = "") -> Optional[str]:
    """Map a market to one of ``schemas.CATEGORIES`` or None (not soft / blocked)."""
    hay = f"{title} {category_hint}".lower()
    if any(b in hay for b in _STOCHASTIC_BLOCKLIST):
        return None
    # Honor an explicit canonical hint if Kalshi already gives us one.
    hint = category_hint.strip().lower()
    if hint in schemas.CATEGORIES:
        return hint
    for canonical, kws in _CATEGORY_PATTERNS:
        if any(kw in hay for kw in kws):
            return canonical
    return None
