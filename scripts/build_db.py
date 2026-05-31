"""Build (or rebuild) the SQLite analysis mirror from committed JSON state.

Usage
-----
    python3 scripts/build_db.py [--db PATH]

Options
-------
--db PATH   Override the default DB path (default: data/forecasts.db).

The .db file is gitignored and fully reconstructable from the JSON files in
data/.  Run this after any JSON state change to refresh the analysis mirror.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, config  # noqa: E402 — must come after sys.path insert


def _parse_args() -> Path:
    """Minimal argument parser for --db PATH override."""
    args = sys.argv[1:]
    db_path = config.DB_PATH
    i = 0
    while i < len(args):
        if args[i] == "--db" and i + 1 < len(args):
            db_path = Path(args[i + 1])
            i += 2
        else:
            i += 1
    return db_path


def main() -> None:
    db_path = _parse_args()

    print(f"Building DB at {db_path} ...")
    db.build(db_path)

    # --- Table row counts ---
    tables = ["markets", "forecasts", "resolutions", "lessons"]
    print("\nTable row counts:")
    for table in tables:
        rows = db.query(f"SELECT COUNT(*) AS n FROM {table}", db_path=db_path)
        print(f"  {table:<15} {rows[0]['n']}")

    # --- Overall Brier summary ---
    ob = db.overall_brier(db_path=db_path)
    print(f"\nOverall Brier (n={ob['n']}):")
    if ob["n"] == 0:
        print("  No resolutions yet.")
    else:
        print(f"  brier_mine_mean   = {ob['brier_mine_mean']:.4f}")
        if ob["brier_market_mean"] is not None:
            print(f"  brier_market_mean = {ob['brier_market_mean']:.4f}")
        else:
            print("  brier_market_mean = N/A")
        if ob["beat_rate"] is not None:
            print(f"  beat_rate         = {ob['beat_rate']:.1%}")
        else:
            print("  beat_rate         = N/A")

    # --- Calibration by category ---
    cat_rows = db.calibration_by_category(db_path=db_path)
    print("\nCalibration by category:")
    if not cat_rows:
        print("  No resolved markets with Brier scores yet.")
    else:
        header = f"  {'category':<14} {'n':>4}  {'brier_mine':>10}  {'brier_market':>12}  {'skill':>8}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for row in cat_rows:
            bm = f"{row['brier_mine_mean']:.4f}" if row["brier_mine_mean"] is not None else "N/A"
            bmkt = f"{row['brier_market_mean']:.4f}" if row["brier_market_mean"] is not None else "N/A"
            sk = f"{row['skill']:.4f}" if row["skill"] is not None else "N/A"
            print(f"  {row['category']:<14} {row['n']:>4}  {bm:>10}  {bmkt:>12}  {sk:>8}")

    print("\nDone.")
    sys.exit(0)


if __name__ == "__main__":
    main()
