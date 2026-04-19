#!/usr/bin/env python3
"""Delete a row from downloads by slug (SQLite)."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="DELETE FROM downloads WHERE slug = ?")
    parser.add_argument(
        "--slug",
        "-s",
        required=True,
        metavar="CODE",
        help="Movie slug / code, e.g. JUQ-014",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to downloads.db (default: downloads.db next to this script)",
    )
    args = parser.parse_args()
    slug = (args.slug or "").strip()
    if not slug:
        print("error: empty slug", file=sys.stderr)
        return 1

    db_path = args.db
    if db_path is None:
        db_path = Path(__file__).resolve().parent / "downloads.db"
    else:
        db_path = db_path.resolve()

    if not db_path.is_file():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM downloads WHERE slug = ?", (slug,))
        conn.commit()
        n = conn.total_changes
    finally:
        conn.close()

    print("deleted", n, "row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
