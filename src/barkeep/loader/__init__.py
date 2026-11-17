"""Postgres loader for parsed weekly reports."""

from __future__ import annotations

from pathlib import Path

import psycopg

from .db import connect
from .errors import LoaderError
from .load import LoadResult, load_directory, load_file, record_failure


def apply_migrations(
    conn: psycopg.Connection, sql_dir: str | Path | None = None
) -> list[str]:
    """Apply pending numbered SQL migrations. See :mod:`barkeep.loader.migrate`."""
    from .migrate import apply_migrations as _apply

    return _apply(conn, sql_dir)


__all__ = [
    "LoadResult",
    "LoaderError",
    "apply_migrations",
    "connect",
    "load_directory",
    "load_file",
    "record_failure",
]
