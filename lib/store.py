"""Atomic JSON state I/O, edge/drift computation, and idempotency guards.

This module is the single place where every JSON file in ``data/`` is read and
written.  All writes are atomic (write-to-temp + os.replace) so a crash mid-write
never leaves a half-written file.  The module is stdlib-only plus the project's own
``lib.schemas`` and ``lib.config`` (and optionally ``lib.scoring``).

Public API
----------
read_json(path, default)
write_json_atomic(path, obj)
load_watchlist() -> schemas.Watchlist
save_watchlist(wl)
load_forecast(ticker) -> Optional[schemas.ForecastRecord]
save_forecast(rec)
iter_forecasts() -> list[schemas.ForecastRecord]
append_forecast_entry(ticker, entry, *, title, category, close_time) -> schemas.ForecastRecord
load_resolutions() -> schemas.ResolutionsFile
save_resolutions(rf)
load_calibration() -> schemas.Calibration
save_calibration(c)
append_run_log(entry)
load_candidates() -> list[dict]
save_candidates(items)
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from lib import schemas, config

# ---------------------------------------------------------------------------
# Optional scoring import — graceful fallback if lib/scoring.py not yet written.
# ---------------------------------------------------------------------------
try:
    from lib import scoring as _scoring  # type: ignore[attr-defined]
    _HAS_SCORING = True
except ImportError:
    _scoring = None  # type: ignore[assignment]
    _HAS_SCORING = False


def _compute_edge(
    mine: float,
    market: Optional[float],
) -> Optional[float]:
    """Delegate to lib.scoring.edge if available; otherwise naive subtraction."""
    if market is None:
        return None
    if _HAS_SCORING:
        return _scoring.edge(mine, market)
    return mine - market


# ---------------------------------------------------------------------------
# Primitive JSON I/O
# ---------------------------------------------------------------------------

def read_json(path: "str | Path", default: Any = None) -> Any:
    """Return parsed JSON at *path*, or *default* if the file is missing/empty/corrupt."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        if not text.strip():
            return default
        return json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path: "str | Path", obj: Any) -> None:
    """Write *obj* as JSON to *path* atomically.

    Creates parent directories, writes to a temp file in the SAME directory, then
    calls ``os.replace()`` to swap in the new file (atomic on POSIX).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False)
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of the temp file on error.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def load_watchlist() -> schemas.Watchlist:
    """Load watchlist from ``config.WATCHLIST_PATH``; return empty list on first run."""
    data = read_json(config.WATCHLIST_PATH)
    if data is None:
        return schemas.Watchlist(cap=config.WATCHLIST_CAP)
    return schemas.Watchlist.from_dict(data)


def save_watchlist(wl: schemas.Watchlist) -> None:
    """Stamp ``updated_at`` and write watchlist atomically."""
    config.ensure_dirs()
    wl.updated_at = schemas.utc_now_iso()
    write_json_atomic(config.WATCHLIST_PATH, wl.to_dict())


# ---------------------------------------------------------------------------
# Forecast records
# ---------------------------------------------------------------------------

def load_forecast(ticker: str) -> Optional[schemas.ForecastRecord]:
    """Load a single forecast record; returns ``None`` if file is absent."""
    path = config.forecast_path(ticker)
    data = read_json(path)
    if data is None:
        return None
    return schemas.ForecastRecord.from_dict(data)


def save_forecast(rec: schemas.ForecastRecord) -> None:
    """Write a forecast record atomically."""
    config.ensure_dirs()
    path = config.forecast_path(rec.ticker)
    write_json_atomic(path, rec.to_dict())


def iter_forecasts() -> list[schemas.ForecastRecord]:
    """Load every ``*.json`` file under ``config.FORECASTS_DIR``."""
    forecasts_dir = Path(config.FORECASTS_DIR)
    if not forecasts_dir.exists():
        return []
    results: list[schemas.ForecastRecord] = []
    for p in sorted(forecasts_dir.glob("*.json")):
        data = read_json(p)
        if data is not None:
            results.append(schemas.ForecastRecord.from_dict(data))
    return results


def append_forecast_entry(
    ticker: str,
    entry: schemas.ForecastEntry,
    *,
    title: Optional[str] = None,
    category: Optional[str] = None,
    close_time: Optional[str] = None,
) -> schemas.ForecastRecord:
    """Append (or idempotently replace) a forecast entry on a record.

    Idempotency rule: if the most recent history entry shares the SAME calendar date
    (first 10 chars of ``as_of``) AND the same ``trigger``, replace it in place rather
    than appending (re-run safety).

    Computed fields set on *entry* (in-place):
    * ``as_of``               — filled with utc_now_iso() if empty.
    * ``edge``                — my_probability - market_implied_probability (via scoring).
    * ``prob_delta_vs_prev``  — my_probability minus previous entry's my_probability.
    """
    config.ensure_dirs()

    # Load or create the record.
    rec = load_forecast(ticker)
    if rec is None:
        rec = schemas.ForecastRecord(ticker=ticker)

    # Update metadata if provided.
    if title is not None:
        rec.title = title
    if category is not None:
        rec.category = category
    if close_time is not None:
        rec.close_time = close_time

    # Stamp as_of if missing.
    if not entry.as_of:
        entry.as_of = schemas.utc_now_iso()

    # Compute edge.
    entry.edge = _compute_edge(entry.my_probability, entry.market_implied_probability)

    # Compute prob_delta_vs_prev.
    if rec.history:
        prev = rec.history[-1]
        entry.prob_delta_vs_prev = entry.my_probability - prev.my_probability
    else:
        entry.prob_delta_vs_prev = None

    # Idempotency: same calendar date + same trigger → replace last entry.
    entry_date = entry.as_of[:10]
    if (
        rec.history
        and rec.history[-1].as_of[:10] == entry_date
        and rec.history[-1].trigger == entry.trigger
    ):
        # Re-compute delta relative to entry BEFORE the one we're replacing.
        if len(rec.history) >= 2:
            prev_before = rec.history[-2]
            entry.prob_delta_vs_prev = entry.my_probability - prev_before.my_probability
        else:
            entry.prob_delta_vs_prev = None
        rec.history[-1] = entry
    else:
        rec.history.append(entry)

    # Denormalized current = newest history entry (deep copy so mutations don't alias).
    rec.current = copy.deepcopy(rec.history[-1])

    # ENTRY LOCK (option A): the first time a lean is ACTIONABLE (YES/NO) we freeze the
    # position immutably. A vetoed position arrives here as lean=NONE, so it never locks.
    # Once locked, later re-forecasts are belief-drift only — they never move the entry,
    # so performance is scored against a real committed decision, not a shifting opinion.
    side = (entry.lean or "NONE").upper()
    if side in ("YES", "NO") and not rec.position.entered:
        ask = entry.yes_ask if side == "YES" else entry.no_ask
        rec.position = schemas.Position(
            entered=True,
            entry_as_of=entry.as_of,
            entry_side=side,
            entry_probability=entry.my_probability,
            entry_price=(ask if ask is not None else 0.0),
            entry_market_implied=entry.market_implied_probability,
            entry_confidence=entry.my_confidence or "",
            entry_conviction=entry.conviction or "",
            entry_gap=(None if entry.market_implied_probability is None
                       else round(abs(entry.my_probability - entry.market_implied_probability), 4)),
            adversarial_verdict=entry.adversarial_verdict or "",
            adversarial_challenged_prob=entry.adversarial_challenged_prob,
            adversarial_concerns=list(entry.adversarial_concerns or []),
            adversarial_model=entry.adversarial_model or "",
        )

    save_forecast(rec)
    return rec


# ---------------------------------------------------------------------------
# Resolutions
# ---------------------------------------------------------------------------

def load_resolutions() -> schemas.ResolutionsFile:
    """Load resolutions file; return empty ResolutionsFile on first run."""
    data = read_json(config.RESOLUTIONS_PATH)
    if data is None:
        return schemas.ResolutionsFile()
    return schemas.ResolutionsFile.from_dict(data)


def save_resolutions(rf: schemas.ResolutionsFile) -> None:
    """Stamp ``updated_at`` and write resolutions atomically."""
    config.ensure_dirs()
    rf.updated_at = schemas.utc_now_iso()
    write_json_atomic(config.RESOLUTIONS_PATH, rf.to_dict())


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def load_calibration() -> schemas.Calibration:
    """Load calibration file; return empty Calibration on first run."""
    data = read_json(config.CALIBRATION_PATH)
    if data is None:
        return schemas.Calibration()
    return schemas.Calibration.from_dict(data)


def save_calibration(c: schemas.Calibration) -> None:
    """Write calibration file atomically (no updated_at stamp on Calibration)."""
    config.ensure_dirs()
    write_json_atomic(config.CALIBRATION_PATH, c.to_dict())


# ---------------------------------------------------------------------------
# Lessons (post-mortem learning log)
# ---------------------------------------------------------------------------

def load_lessons() -> schemas.LessonsFile:
    """Load the lessons log; return an empty file on first run."""
    data = read_json(config.LESSONS_PATH)
    if data is None:
        return schemas.LessonsFile()
    return schemas.LessonsFile.from_dict(data)


def save_lessons(lf: schemas.LessonsFile) -> None:
    config.ensure_dirs()
    lf.updated_at = schemas.utc_now_iso()
    write_json_atomic(config.LESSONS_PATH, lf.to_dict())


def append_lesson(lesson: schemas.Lesson) -> schemas.LessonsFile:
    """Append a lesson (idempotent on id: replaces an existing lesson with the same id)."""
    lf = load_lessons()
    if not lesson.created_at:
        lesson.created_at = schemas.utc_now_iso()
    lf.lessons = [l for l in lf.lessons if l.id != lesson.id]
    lf.lessons.append(lesson)
    save_lessons(lf)
    return lf


def pattern_counts() -> dict:
    """Count lessons per pattern_tag from resolution post-mortems — used to decide when a
    pattern recurs often enough to justify a SKILL revision (>= config.SKILL_REVISION_MIN_PATTERN)."""
    counts: dict[str, int] = {}
    for l in load_lessons().lessons:
        if l.source == "resolution" and l.pattern_tag:
            counts[l.pattern_tag] = counts.get(l.pattern_tag, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Run log (append-only JSONL)
# ---------------------------------------------------------------------------

def append_run_log(entry: schemas.RunLogEntry) -> None:
    """Append one JSON line to the run log JSONL file."""
    config.ensure_dirs()
    log_path = Path(config.RUN_LOG_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry.to_dict(), ensure_ascii=False)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def load_candidates() -> list[dict]:
    """Load candidates list from ``config.CANDIDATES_PATH``."""
    data = read_json(config.CANDIDATES_PATH)
    if data is None:
        return []
    return data.get("candidates", [])


def save_candidates(items: list[dict]) -> None:
    """Write candidates list atomically."""
    config.ensure_dirs()
    payload = {
        "schema_version": schemas.SCHEMA_VERSION,
        "updated_at": schemas.utc_now_iso(),
        "candidates": items,
    }
    write_json_atomic(config.CANDIDATES_PATH, payload)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile as _tempfile
    import shutil as _shutil

    # -----------------------------------------------------------------------
    # Monkey-patch config paths to a temporary directory so we never pollute
    # the real data/ tree.
    # -----------------------------------------------------------------------
    _tmp_root = Path(_tempfile.mkdtemp(prefix="store_test_"))
    try:
        # Override every path that store.py touches.
        _orig_data_dir = config.DATA_DIR
        _orig_forecasts_dir = config.FORECASTS_DIR
        _orig_scratch_dir = config.SCRATCH_DIR
        _orig_reports_dir = config.REPORTS_DIR
        _orig_archive_dir = config.ARCHIVE_DIR
        _orig_watchlist_path = config.WATCHLIST_PATH
        _orig_resolutions_path = config.RESOLUTIONS_PATH
        _orig_calibration_path = config.CALIBRATION_PATH
        _orig_candidates_path = config.CANDIDATES_PATH
        _orig_run_log_path = config.RUN_LOG_PATH

        config.DATA_DIR = _tmp_root / "data"
        config.FORECASTS_DIR = config.DATA_DIR / "forecasts"
        config.SCRATCH_DIR = config.DATA_DIR / "scratch"
        config.REPORTS_DIR = _tmp_root / "reports"
        config.ARCHIVE_DIR = config.REPORTS_DIR / "archive"
        config.WATCHLIST_PATH = config.DATA_DIR / "watchlist.json"
        config.RESOLUTIONS_PATH = config.DATA_DIR / "resolutions.json"
        config.CALIBRATION_PATH = config.DATA_DIR / "calibration.json"
        config.CANDIDATES_PATH = config.DATA_DIR / "candidates.json"
        config.RUN_LOG_PATH = config.DATA_DIR / "run_log.jsonl"

        # ensure_dirs now uses the patched paths
        config.ensure_dirs()

        # -------------------------------------------------------------------
        # Test 1: append_forecast_entry — same-day + same-trigger idempotency
        # -------------------------------------------------------------------
        TICKER = "TEST-TICKER-001"
        entry1 = schemas.ForecastEntry(
            my_probability=0.60,
            market_implied_probability=0.55,
            trigger="scheduled",
            as_of="2026-05-31T10:00:00Z",
        )
        rec = append_forecast_entry(
            TICKER, entry1,
            title="Test market", category="politics", close_time="2026-12-31T00:00:00Z"
        )
        assert len(rec.history) == 1, f"Expected 1 history entry, got {len(rec.history)}"
        assert rec.history[0].edge is not None, "edge should be computed"
        assert abs(rec.history[0].edge - 0.05) < 1e-9, (
            f"edge mismatch: {rec.history[0].edge}"
        )
        assert rec.history[0].prob_delta_vs_prev is None, "first entry delta should be None"

        # Same day, same trigger → replace (idempotency)
        entry2 = schemas.ForecastEntry(
            my_probability=0.65,
            market_implied_probability=0.55,
            trigger="scheduled",
            as_of="2026-05-31T11:30:00Z",
        )
        rec = append_forecast_entry(TICKER, entry2)
        assert len(rec.history) == 1, (
            f"Idempotency failed: expected 1 history entry, got {len(rec.history)}"
        )
        assert abs(rec.history[0].my_probability - 0.65) < 1e-9, "entry not replaced"
        assert abs(rec.history[0].edge - 0.10) < 1e-9, f"edge after replace: {rec.history[0].edge}"
        assert rec.history[0].prob_delta_vs_prev is None, (
            "prob_delta should be None when only 1 entry (after replace)"
        )
        assert rec.current is not None and abs(rec.current.my_probability - 0.65) < 1e-9

        # -------------------------------------------------------------------
        # Test 2: different trigger → appended (history grows to 2)
        # -------------------------------------------------------------------
        entry3 = schemas.ForecastEntry(
            my_probability=0.70,
            market_implied_probability=0.55,
            trigger="event_driven",        # different trigger
            as_of="2026-05-31T14:00:00Z",
        )
        rec = append_forecast_entry(TICKER, entry3)
        assert len(rec.history) == 2, (
            f"Expected 2 history entries, got {len(rec.history)}"
        )
        assert abs(rec.history[1].prob_delta_vs_prev - (0.70 - 0.65)) < 1e-9, (
            f"prob_delta wrong: {rec.history[1].prob_delta_vs_prev}"
        )
        assert abs(rec.history[1].edge - 0.15) < 1e-9

        # -------------------------------------------------------------------
        # Test 3: Watchlist round-trip
        # -------------------------------------------------------------------
        wl = load_watchlist()
        assert wl.cap == config.WATCHLIST_CAP, "default cap wrong"
        entry_wl = schemas.WatchlistEntry(
            ticker="SOME-MARKET", title="Some market", added_at=schemas.utc_now_iso()
        )
        wl.markets.append(entry_wl)
        save_watchlist(wl)
        wl2 = load_watchlist()
        assert len(wl2.markets) == 1, f"Expected 1 market, got {len(wl2.markets)}"
        assert wl2.markets[0].ticker == "SOME-MARKET"
        assert wl2.updated_at != "", "updated_at should be stamped"

        # -------------------------------------------------------------------
        # Test 4: RunLogEntry round-trip
        # -------------------------------------------------------------------
        run_entry = schemas.RunLogEntry(
            run_id="test-run-001",
            status="complete",
            discovered=5,
            watchlist_size=3,
            reforecast=2,
            resolved_new=0,
        )
        append_run_log(run_entry)
        log_path = Path(config.RUN_LOG_PATH)
        assert log_path.exists(), "run log file not created"
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1, f"Expected 1 log line, got {len(lines)}"
        loaded = json.loads(lines[0])
        assert loaded["run_id"] == "test-run-001"

        # Append a second line
        append_run_log(run_entry)
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2, f"Expected 2 log lines, got {len(lines)}"

        # -------------------------------------------------------------------
        # Test 5: iter_forecasts returns the saved record
        # -------------------------------------------------------------------
        all_forecasts = iter_forecasts()
        assert len(all_forecasts) == 1, f"Expected 1 forecast, got {len(all_forecasts)}"
        assert all_forecasts[0].ticker == TICKER

        print("store OK")

    finally:
        # Restore original config paths.
        config.DATA_DIR = _orig_data_dir
        config.FORECASTS_DIR = _orig_forecasts_dir
        config.SCRATCH_DIR = _orig_scratch_dir
        config.REPORTS_DIR = _orig_reports_dir
        config.ARCHIVE_DIR = _orig_archive_dir
        config.WATCHLIST_PATH = _orig_watchlist_path
        config.RESOLUTIONS_PATH = _orig_resolutions_path
        config.CALIBRATION_PATH = _orig_calibration_path
        config.CANDIDATES_PATH = _orig_candidates_path
        config.RUN_LOG_PATH = _orig_run_log_path

        # Clean up temp directory.
        _shutil.rmtree(_tmp_root, ignore_errors=True)
