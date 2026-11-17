"""Filename contract: ``{venue}-SalesByMenuItem-{start}-{end}.csv``."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from barkeep.errors import FilenameParseError

_FILENAME_RE = re.compile(
    r"^(?P<venue>.+)-SalesByMenuItem-"
    r"(?P<start>\d{4}-\d{2}-\d{2})-"
    r"(?P<end>\d{4}-\d{2}-\d{2})\.csv$"
)


def parse_filename(filename: str) -> tuple[str, date, date]:
    """Extract ``(venue, period_start, period_end)`` from a report filename.

    The CSV carries no date column, so the period comes only from the filename.
    Any name that does not match the contract raises :class:`FilenameParseError`;
    nothing is guessed.
    """
    match = _FILENAME_RE.match(Path(filename).name)
    if match is None:
        raise FilenameParseError(f"filename does not match report contract: {filename!r}")

    try:
        period_start = date.fromisoformat(match.group("start"))
        period_end = date.fromisoformat(match.group("end"))
    except ValueError as exc:
        raise FilenameParseError(f"filename contains an invalid date: {filename!r}") from exc

    if period_end < period_start:
        raise FilenameParseError(
            f"period_end precedes period_start in filename: {filename!r}"
        )

    return match.group("venue"), period_start, period_end
