"""Loader-side failures."""

from __future__ import annotations


class LoaderError(Exception):
    """Base class for ingestion and loading failures."""
