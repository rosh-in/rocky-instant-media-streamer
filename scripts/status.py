"""Print a summary of recent sync runs for quick health checks."""

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from rocky.config import load_settings


def _format_row(row: sqlite3.Row) -> str:
    status = row["status"].upper()
    started = row["started_at"] or "—"
    finished = row["finished_at"] or "(running)"
    parts = [
        f"  Run #{row['id']}  {status}",
        f"    Started:  {started}",
        f"    Finished: {finished}",
        f"    Seen={row['items_seen']}  Enriched={row['items_enriched']}  "
        f"Availability={row['items_availability_refreshed']}  Requested={row['items_requested']}",
    ]
    if row["error_message"]:
        parts.append(f"    Error: {row['error_message']}")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show recent sync run status.")
    parser.add_argument(
        "--last", type=int, default=5, help="Number of recent runs to display (default: 5)"
    )
    args = parser.parse_args()

    settings = load_settings()
    db_path = settings.sqlite_path

    if not db_path.exists():
        print(f"Database not found at {db_path}. No sync runs yet.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (args.last,)
    ).fetchall()
    conn.close()

    if not rows:
        print("No sync runs recorded yet.")
        sys.exit(0)

    print(f"Last {len(rows)} sync run(s):\n")
    for row in rows:
        print(_format_row(row))
        print()

    latest = rows[0]
    if latest["status"] == "failed":
        print("⚠  Most recent sync run FAILED.")
        sys.exit(1)
    elif latest["status"] == "running":
        print("⏳ Most recent sync run is still running.")
    else:
        print("✅ Most recent sync run succeeded.")


if __name__ == "__main__":
    main()
