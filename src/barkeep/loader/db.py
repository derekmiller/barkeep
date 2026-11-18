"""Postgres connection helpers for the loader."""

from __future__ import annotations

import os
from collections.abc import Mapping

import psycopg

from .errors import LoaderError
from .secrets import read_secret_file


def resolve_database_url(
    dsn: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve a DSN from an explicit value, ``DATABASE_URL``, or a file.

    Precedence: explicit ``dsn``, then ``DATABASE_URL``, then
    ``DATABASE_URL_FILE`` (used for Docker/systemd secrets).
    """
    if dsn:
        return dsn
    env = os.environ if environ is None else environ
    value = env.get("DATABASE_URL")
    if value:
        return value
    path = env.get("DATABASE_URL_FILE")
    if path:
        return read_secret_file(path)
    return None


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Open a Postgres connection from ``dsn``, ``DATABASE_URL``, or its file."""
    resolved = resolve_database_url(dsn)
    if not resolved:
        raise LoaderError(
            "no database DSN: set DATABASE_URL or DATABASE_URL_FILE"
        )
    try:
        return psycopg.connect(resolved)
    except psycopg.Error as exc:
        raise LoaderError(f"cannot connect to Postgres: {exc}") from exc
