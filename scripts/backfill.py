#!/usr/bin/env python3
"""One-off backfill: load every weekly CSV in a directory into Postgres.

Usage:
    python scripts/backfill.py [directory]   # default: /data/raw

Migrations are expected to have been applied first. In Docker, the ``backfill``
Compose service runs the migration step before this script.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from barkeep.loader import connect, load_directory


def main(argv: list[str]) -> int:
    directory = Path(argv[1]) if len(argv) > 1 else Path("/data/raw")
    if not directory.is_dir():
        print(f"not a directory: {directory}", file=sys.stderr)
        return 2

    with connect() as conn:
        results = load_directory(conn, directory)

    for result in results:
        if result.status == "failed":
            print(
                f"failed: {result.filename}: {result.message}",
                file=sys.stderr,
            )

    counts = Counter(result.status for result in results)
    summary = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
    print(f"{len(results)} files: {summary or 'none'}")
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
