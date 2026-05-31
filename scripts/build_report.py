"""Regenerate the PDF report from current committed state, then archive a dated copy.

Usage
-----
    python3 scripts/build_report.py [--out PATH]

Arguments
---------
--out PATH
    Override the default output path (default: config.LATEST_PDF_PATH).

Exit codes
----------
0   Success.
1   Report generation failed (error printed to stderr).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json

from lib import config, store, report, gitops, schemas


def _load_run_log() -> list[schemas.RunLogEntry]:
    """Read config.RUN_LOG_PATH if it exists; parse each non-empty line as JSON."""
    log_path = Path(config.RUN_LOG_PATH)
    if not log_path.exists():
        return []
    entries: list[schemas.RunLogEntry] = []
    for raw_line in log_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            entries.append(schemas.RunLogEntry.from_dict(data))
        except (json.JSONDecodeError, Exception):
            # Skip malformed lines rather than aborting.
            pass
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the Kalshi superforecaster PDF report.",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default=None,
        help="Output PDF path (default: %(default)s → config.LATEST_PDF_PATH)",
    )
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else config.LATEST_PDF_PATH

    # Ensure data/reports directories exist.
    config.ensure_dirs()

    # Load all state.
    watchlist   = store.load_watchlist()
    forecasts   = store.iter_forecasts()
    resolutions = store.load_resolutions()
    calibration = store.load_calibration()
    run_log     = _load_run_log()

    # Generate the PDF.
    try:
        latest_pdf = report.build_pdf(
            watchlist,
            forecasts,
            resolutions,
            calibration,
            run_log,
            out_path=out_path,
        )
    except Exception as exc:
        print(f"ERROR: report generation failed: {exc}", file=sys.stderr)
        return 1

    # Archive a dated copy.
    archive_path = gitops.archive_report(latest_pdf, date_str=None)

    print(f"latest pdf : {latest_pdf}")
    if archive_path is not None:
        print(f"archive    : {archive_path}")
    else:
        print("archive    : (skipped — latest pdf not found after build)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
