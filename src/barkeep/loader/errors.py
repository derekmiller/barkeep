"""Loader-side failures."""

from __future__ import annotations

from datetime import date


class LoaderError(Exception):
    """Base class for ingestion and loading failures."""


class ReportRejected(LoaderError):
    """A report that parsed correctly was rejected by a loading rule.

    Carries the report identity and content hash so callers can record a
    self-describing ``failed`` row in ``core.ingest_log`` rather than a bare
    message.
    """

    def __init__(
        self,
        message: str,
        *,
        venue: str,
        period_start: date,
        period_end: date,
        content_sha256: str | None = None,
    ) -> None:
        super().__init__(message)
        self.venue = venue
        self.period_start = period_start
        self.period_end = period_end
        self.content_sha256 = content_sha256
