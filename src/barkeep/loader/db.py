"""Postgres connection helpers for the loader."""

from __future__ import annotations

import os

import psycopg

from .errors import LoaderError


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Open a Postgres connection from ``dsn`` or ``DATABASE_URL``."""
    resolved = dsn or os.environ.get("DATABASE_URL")
    if not resolved:
        raise LoaderError("DATABASE_URL is not set and no dsn was provided")
    return psycopg.connect(resolved)
