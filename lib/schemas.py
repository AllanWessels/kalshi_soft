"""Shared data contract for the kalshi_soft superforecaster.

Every persisted file carries ``schema_version`` for forward migration. All
timestamps are ISO-8601 UTC strings. All probabilities are floats in [0, 1].

This module is the integration contract that every other ``lib`` module and
``scripts`` entrypoint depends on. It is stdlib-only (no third-party imports) so
it can be imported anywhere, including inside the deterministic CLI scripts.

Design notes
------------
* Dataclasses model each record; ``to_dict``/``from_dict`` give explicit, stable
  JSON (de)serialization (we do not rely on bare ``asdict`` so unknown/legacy
  keys are tolerated on load).
* The *agent* writes judgment fields (probability, confidence, rationale, ...);
  deterministic Python computes ``edge`` and ``prob_delta_vs_prev`` and stamps
  ``as_of`` (see ``lib/store.py``).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

# Soft-market categories this project forecasts. These are OUR canonical labels;
# Kalshi's own ``series.category`` strings are mapped onto these in config.py.
CATEGORIES = ("politics", "culture", "statements", "economy")

CONFIDENCE_LEVELS = ("low", "medium", "high")   # epistemic confidence
LEANS = ("YES", "NO", "NONE")                   # paper-only direction
CONVICTION_LEVELS = ("low", "medium", "high")   # size of the paper lean

# Watchlist entry lifecycle.
WATCHLIST_STATUSES = ("active", "resolved", "delisted", "dropped")

# Why a forecast entry was produced.
TRIGGERS = ("bootstrap", "scheduled", "near_close", "event_driven")


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> _dt.datetime:
    """Parse an ISO-8601 UTC timestamp (tolerates trailing ``Z``)."""
    return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Helpers for tolerant (de)serialization
# ---------------------------------------------------------------------------

def _filtered(cls, data: dict[str, Any]) -> dict[str, Any]:
    """Keep only keys that are fields of dataclass ``cls`` (drops legacy keys)."""
    valid = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return {k: v for k, v in data.items() if k in valid}


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

@dataclass
class LiquiditySnapshot:
    volume_24h: float = 0.0
    open_interest: float = 0.0
    spread_cents: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LiquiditySnapshot":
        return cls(**_filtered(cls, d or {}))


@dataclass
class WatchlistEntry:
    ticker: str
    event_ticker: str = ""
    title: str = ""                       # human-readable; REQUIRED so user can find it on Kalshi
    category: str = ""                    # one of CATEGORIES
    close_time: str = ""                  # ISO-8601 UTC
    added_at: str = ""
    liquidity_snapshot: LiquiditySnapshot = field(default_factory=LiquiditySnapshot)
    status: str = "active"                # one of WATCHLIST_STATUSES
    reforecast_cadence_days: float = 7.0  # baseline tier; runtime overrides as close approaches

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["liquidity_snapshot"] = self.liquidity_snapshot.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WatchlistEntry":
        d = dict(d or {})
        ls = d.pop("liquidity_snapshot", None)
        obj = cls(**_filtered(cls, d))
        obj.liquidity_snapshot = LiquiditySnapshot.from_dict(ls or {})
        return obj


@dataclass
class Watchlist:
    cap: int = 20
    updated_at: str = ""
    markets: list[WatchlistEntry] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def active(self) -> list[WatchlistEntry]:
        return [m for m in self.markets if m.status == "active"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "cap": self.cap,
            "markets": [m.to_dict() for m in self.markets],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Watchlist":
        d = d or {}
        return cls(
            cap=int(d.get("cap", 20)),
            updated_at=d.get("updated_at", ""),
            markets=[WatchlistEntry.from_dict(m) for m in d.get("markets", [])],
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Forecast records
# ---------------------------------------------------------------------------

@dataclass
class ForecastEntry:
    """One re-forecast event; appended to a record's ``history``."""
    as_of: str = ""
    my_probability: float = 0.0
    my_confidence: str = "low"            # CONFIDENCE_LEVELS
    market_implied_probability: Optional[float] = None
    market_price_cents: Optional[float] = None  # raw yes midpoint (cents) for audit
    edge: Optional[float] = None          # my_probability - market_implied (computed by store)
    # Profitability (fee-aware): prices you would actually trade at + net expected value.
    yes_ask: Optional[float] = None       # price to BUY yes (dollars)
    no_ask: Optional[float] = None        # price to BUY no  (dollars) ~= 1 - yes_bid
    fee_per_contract: Optional[float] = None   # Kalshi fee on the lean side at the SPOT (ask) price ($)
    ev_per_contract: Optional[float] = None    # SPOT net expected profit/contract ($): buy lean side at the ask, after fee
    # Limit-order alternative: rest a buy on the lean side at its current best bid.
    limit_price: Optional[float] = None        # the bid on the lean side (the resting limit price, dollars)
    ev_limit_per_contract: Optional[float] = None  # net EV/contract IF filled at limit_price, after fee
    lean: str = "NONE"                    # LEANS — actionable side; NONE if EV<min OR confidence gate fails
    conviction: str = "low"               # CONVICTION_LEVELS
    lean_note: Optional[str] = None       # why a positive-EV side was gated to NONE (low conf / large market gap)
    prob_delta_vs_prev: Optional[float] = None   # computed by store
    trigger: str = "scheduled"            # TRIGGERS
    strategy_id: str = ""                 # which forecasting strategy/arm produced this (see lib/strategies)
    rationale_summary: str = ""           # 1-3 sentences, agent-written
    key_drivers: list[str] = field(default_factory=list)
    reference_classes: list[str] = field(default_factory=list)
    research_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ForecastEntry":
        return cls(**_filtered(cls, d or {}))


@dataclass
class ForecastRecord:
    ticker: str
    title: str = ""
    category: str = ""
    close_time: str = ""
    current: Optional[ForecastEntry] = None   # denormalized copy of newest history entry
    history: list[ForecastEntry] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ticker": self.ticker,
            "title": self.title,
            "category": self.category,
            "close_time": self.close_time,
            "current": self.current.to_dict() if self.current else None,
            "history": [h.to_dict() for h in self.history],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ForecastRecord":
        d = d or {}
        cur = d.get("current")
        return cls(
            ticker=d.get("ticker", ""),
            title=d.get("title", ""),
            category=d.get("category", ""),
            close_time=d.get("close_time", ""),
            current=ForecastEntry.from_dict(cur) if cur else None,
            history=[ForecastEntry.from_dict(h) for h in d.get("history", [])],
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Resolutions + calibration
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    ticker: str
    title: str = ""
    category: str = ""
    subcategory: str = ""                 # finer taxonomy slug (see lib/taxonomy)
    resolved_at: str = ""
    outcome: int = 0                      # 1 = YES occurred, 0 = NO
    final_my_probability: float = 0.0     # last forecast BEFORE resolution
    final_as_of: str = ""
    final_market_implied: Optional[float] = None
    brier_mine: Optional[float] = None
    brier_market: Optional[float] = None
    num_forecasts: int = 0
    first_forecast_prob: Optional[float] = None
    # Strategy that produced the final forecast (experimentation harness).
    strategy_id: str = ""
    # Realized paper-trade economics (populated only when the final lean was YES/NO;
    # all None when lean was NONE = no position taken). See lib/profit.py.
    entry_side: str = ""                  # YES | NO | "" (no trade)
    entry_price: Optional[float] = None   # price paid on the lean side (dollars)
    fee_at_entry: Optional[float] = None
    realized_pnl: Optional[float] = None  # net $/contract after fee; None = no trade
    roi: Optional[float] = None           # realized_pnl / staked
    won: Optional[bool] = None
    clv: Optional[float] = None           # closing-line value (skill signal)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Resolution":
        return cls(**_filtered(cls, d or {}))


@dataclass
class ResolutionsFile:
    updated_at: str = ""
    resolved: list[Resolution] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def tickers(self) -> set[str]:
        return {r.ticker for r in self.resolved}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "resolved": [r.to_dict() for r in self.resolved],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResolutionsFile":
        d = d or {}
        return cls(
            updated_at=d.get("updated_at", ""),
            resolved=[Resolution.from_dict(r) for r in d.get("resolved", [])],
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class Lesson:
    """A post-mortem insight. Lessons accumulate; the SKILL is revised only when a
    pattern recurs across MULTIPLE resolutions (never on a single outcome — same
    single-data-point discipline as forecasting)."""
    id: str = ""                          # e.g. "<resolved_at>-<ticker>" or "feedback-<n>"
    created_at: str = ""
    source: str = "resolution"            # resolution | self_review | user_feedback
    ticker: str = ""
    category: str = ""
    outcome: Optional[int] = None         # 1/0 for the resolved market (if source=resolution)
    final_my_probability: Optional[float] = None
    final_market_implied: Optional[float] = None
    brier_mine: Optional[float] = None
    brier_market: Optional[float] = None
    beat_market: Optional[bool] = None
    what_went_right: str = ""
    what_went_wrong: str = ""
    lesson: str = ""                      # the actionable takeaway
    pattern_tag: str = ""                 # short tag to group recurring lessons (e.g. "primary-overconfidence")
    applied_to_skill: bool = False        # set true once folded into SKILL.md (only on a recurring pattern)
    # Adversarial post-mortem panel (scripts/postmortem.py). Empty when the lesson
    # predates the panel or came from user_feedback.
    critic_model: str = ""                # model that produced the blind critique (e.g. local Qwen tag)
    rubric_scores: dict[str, Any] = field(default_factory=dict)  # {rubric_item: {"pass": bool, "reason": str}}
    judge_verdict: str = ""               # the Claude judge's final ruling after critic+defender
    disagreement: str = ""                # where critic and defender diverged (the signal worth keeping)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lesson":
        return cls(**_filtered(cls, d or {}))


@dataclass
class LessonsFile:
    updated_at: str = ""
    lessons: list[Lesson] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "lessons": [l.to_dict() for l in self.lessons],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LessonsFile":
        d = d or {}
        return cls(
            updated_at=d.get("updated_at", ""),
            lessons=[Lesson.from_dict(x) for x in d.get("lessons", [])],
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class CalibrationBin:
    range: list[float] = field(default_factory=lambda: [0.0, 0.0])
    n: int = 0
    mean_forecast: Optional[float] = None
    observed_freq: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CalibrationBin":
        return cls(**_filtered(cls, d or {}))


@dataclass
class Calibration:
    updated_at: str = ""
    n_resolved: int = 0
    brier_mine_mean: Optional[float] = None
    brier_market_mean: Optional[float] = None
    skill_vs_market: Optional[float] = None   # market_mean - mine_mean (positive = we beat market)
    bins: list[CalibrationBin] = field(default_factory=list)
    by_category: dict[str, Any] = field(default_factory=dict)
    by_segment: dict[str, Any] = field(default_factory=dict)   # "category / subcategory" -> stats
    by_strategy: dict[str, Any] = field(default_factory=dict)   # strategy_id -> Brier skill stats
    # Realized-profit scoreboards (see lib/profit.aggregate_profit); empty until trades resolve.
    profit_by_category: dict[str, Any] = field(default_factory=dict)
    profit_by_segment: dict[str, Any] = field(default_factory=dict)
    profit_by_strategy: dict[str, Any] = field(default_factory=dict)  # the "which topology wins" view
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "n_resolved": self.n_resolved,
            "brier_mine_mean": self.brier_mine_mean,
            "brier_market_mean": self.brier_market_mean,
            "skill_vs_market": self.skill_vs_market,
            "bins": [b.to_dict() for b in self.bins],
            "by_category": self.by_category,
            "by_segment": self.by_segment,
            "by_strategy": self.by_strategy,
            "profit_by_category": self.profit_by_category,
            "profit_by_segment": self.profit_by_segment,
            "profit_by_strategy": self.profit_by_strategy,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Calibration":
        d = d or {}
        return cls(
            updated_at=d.get("updated_at", ""),
            n_resolved=int(d.get("n_resolved", 0)),
            brier_mine_mean=d.get("brier_mine_mean"),
            brier_market_mean=d.get("brier_market_mean"),
            skill_vs_market=d.get("skill_vs_market"),
            bins=[CalibrationBin.from_dict(b) for b in d.get("bins", [])],
            by_category=d.get("by_category", {}),
            by_segment=d.get("by_segment", {}),
            by_strategy=d.get("by_strategy", {}),
            profit_by_category=d.get("profit_by_category", {}),
            profit_by_segment=d.get("profit_by_segment", {}),
            profit_by_strategy=d.get("profit_by_strategy", {}),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Run log / usage
# ---------------------------------------------------------------------------

@dataclass
class Usage:
    """Best-effort cost proxies captured in-run. NOT authoritative for $/tokens
    (see claude.ai/settings/usage)."""
    web_searches: int = 0
    web_fetches: int = 0
    tool_calls: int = 0
    markets_researched: int = 0
    duration_s: float = 0.0
    est_tokens: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Usage":
        return cls(**_filtered(cls, d or {}))


@dataclass
class RunLogEntry:
    run_id: str = ""                      # typically the run's UTC timestamp
    status: str = "complete"             # complete | partial | failed
    discovered: int = 0
    watchlist_size: int = 0
    reforecast: int = 0
    resolved_new: int = 0
    errors: list[str] = field(default_factory=list)
    pdf: str = ""
    usage: Usage = field(default_factory=Usage)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["usage"] = self.usage.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunLogEntry":
        d = dict(d or {})
        usage = d.pop("usage", None)
        obj = cls(**_filtered(cls, d))
        obj.usage = Usage.from_dict(usage or {})
        return obj
