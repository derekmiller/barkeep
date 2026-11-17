"""Apply numbered SQL migrations to Postgres."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from .db import connect

DEFAULT_SQL_DIR = Path(__file__).resolve().parents[3] / "sql"


def apply_migrations(
    conn: psycopg.Connection, sql_dir: str | Path | None = None
) -> list[str]:
    """Apply every unapplied ``*.sql`` file in ``sql_dir`` in name order.

    Returns the filenames that were applied by this call. Each migration runs
    in its own transaction and is recorded in ``public.schema_migrations``.
    """
    directory = Path(sql_dir) if sql_dir is not None else DEFAULT_SQL_DIR

    with conn.transaction():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
                filename text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

    with conn.transaction():
        applied = {
            row[0]
            for row in conn.execute(
                "SELECT filename FROM public.schema_migrations"
            ).fetchall()
        }

    newly_applied: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        if path.name in applied:
            continue
        script = path.read_text(encoding="utf-8")
        with conn.transaction():
            conn.execute(script)
            conn.execute(
                "INSERT INTO public.schema_migrations (filename) VALUES (%s)",
                (path.name,),
            )
        newly_applied.append(path.name)

    return newly_applied


def main() -> None:
    """CLI entry point: ``python -m barkeep.loader.migrate``."""
    dsn = os.environ.get("DATABASE_URL")
    sql_dir = os.environ.get("BARKEEP_SQL_DIR")
    with connect(dsn) as conn:
        applied = apply_migrations(conn, sql_dir)
    if applied:
        print("Applied migrations:")
        for name in applied:
            print(f"  {name}")
    else:
        print("No migrations to apply.")


if __name__ == "__main__":
    main()
