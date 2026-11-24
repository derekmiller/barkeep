"""Idempotent loading of parsed weekly reports into Postgres."""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from barkeep import __version__ as PARSER_VERSION
from barkeep.parser import ParseError, ParseResult, parse_csv, parse_filename

from .errors import LoaderError, ReportRejected
from .venues import canonical_venue, venue_aliases_from_env

Status = str


@dataclass(frozen=True)
class LoadResult:
    """Outcome of loading one CSV file."""

    filename: str
    status: Status
    venue: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    message: str | None = None


def _log(
    conn: psycopg.Connection,
    *,
    filename: str,
    status: Status,
    venue: str | None,
    period_start: date | None,
    period_end: date | None,
    content_sha256: str | None = None,
    message: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO core.ingest_log (
            venue, period_start, period_end, filename,
            content_sha256, parser_version, status, message
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            venue,
            period_start,
            period_end,
            filename,
            content_sha256,
            PARSER_VERSION,
            status,
            message,
        ),
    )


def _require_week(result: ParseResult, filename: str, digest: str) -> None:
    if result.days_covered != 7 or result.period_start.weekday() != 6:
        raise ReportRejected(
            f"{filename}: expected a full Sunday-Saturday week, got "
            f"{result.period_start} -> {result.period_end} "
            f"({result.days_covered} days)",
            venue=result.venue,
            period_start=result.period_start,
            period_end=result.period_end,
            content_sha256=digest,
        )


def _require_summary(result: ParseResult, filename: str, digest: str) -> None:
    # The parser accepts a file without its REPORT SUMMARY row, but every real
    # export ends with one. A file cut off cleanly between rows still parses, and
    # only the summary cross-check could notice the missing tail, so loading it
    # would publish a short week as a complete one.
    if result.summary is None:
        raise ReportRejected(
            f"{filename}: no REPORT SUMMARY row; the file may be truncated",
            venue=result.venue,
            period_start=result.period_start,
            period_end=result.period_end,
            content_sha256=digest,
        )


def load_file(
    conn: psycopg.Connection,
    data: bytes,
    filename: str,
    *,
    require_week: bool = True,
    venue_aliases: Mapping[str, str] | None = None,
) -> LoadResult:
    """Parse and load one report, replacing any existing report for its period.

    The file is keyed by ``(venue, period_start, period_end)``, not by filename.
    Re-loading identical bytes is a no-op; loading different bytes for the same
    period replaces that period's facts atomically.

    A report without its ``REPORT SUMMARY`` row is rejected with
    :class:`ReportRejected`: the summary is the only cross-check that can detect
    a file truncated between rows.

    ``venue_aliases`` maps the venue recorded in the filename onto its canonical
    name (see :mod:`barkeep.loader.venues`). It defaults to
    ``BARKEEP_VENUE_ALIASES``; pass an explicit mapping to bypass the environment.
    """
    parsed = parse_csv(data, filename)
    aliases = (
        venue_aliases_from_env() if venue_aliases is None else venue_aliases
    )
    canonical = canonical_venue(parsed.venue, aliases)
    if canonical != parsed.venue:
        parsed = replace(parsed, venue=canonical)

    digest = hashlib.sha256(data).hexdigest()
    _require_summary(parsed, filename, digest)
    if require_week:
        _require_week(parsed, filename, digest)

    try:
        status, message = _apply_report(conn, parsed, digest, filename)
    except psycopg.Error as exc:
        raise LoaderError(f"{filename}: database error: {exc}") from exc

    return LoadResult(
        filename=filename,
        status=status,
        venue=parsed.venue,
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        message=message,
    )


def _apply_report(
    conn: psycopg.Connection,
    parsed: ParseResult,
    digest: str,
    filename: str,
) -> tuple[str, str | None]:
    """Insert, replace, or skip one report; return ``(status, message)``."""
    with conn.transaction():
        # Serialise concurrent loads of the same period. SELECT ... FOR UPDATE
        # cannot lock a row that does not exist yet, so two concurrent
        # first-loads would otherwise race into report_period_unique.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{parsed.venue}|{parsed.period_start}|{parsed.period_end}",),
        )

        existing = conn.execute(
            """
            SELECT id, content_sha256
            FROM core.report
            WHERE venue = %s AND period_start = %s AND period_end = %s
            FOR UPDATE
            """,
            (parsed.venue, parsed.period_start, parsed.period_end),
        ).fetchone()

        if existing is not None and existing[1] == digest:
            _log(
                conn,
                filename=filename,
                status="skipped",
                venue=parsed.venue,
                period_start=parsed.period_start,
                period_end=parsed.period_end,
                content_sha256=digest,
                message="identical report already loaded",
            )
            return "skipped", "identical report already loaded"

        if existing is not None:
            report_id = existing[0]
            conn.execute(
                "DELETE FROM core.sales_row WHERE report_id = %s", (report_id,)
            )
            conn.execute(
                "DELETE FROM core.report_summary WHERE report_id = %s",
                (report_id,),
            )
            conn.execute(
                """
                UPDATE core.report
                SET days_covered = %s,
                    filename = %s,
                    content_sha256 = %s,
                    parser_version = %s,
                    ingested_at = now()
                WHERE id = %s
                """,
                (
                    parsed.days_covered,
                    filename,
                    digest,
                    PARSER_VERSION,
                    report_id,
                ),
            )
            status = "replaced"
        else:
            inserted = conn.execute(
                """
                INSERT INTO core.report (
                    venue, period_start, period_end, days_covered,
                    filename, content_sha256, parser_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    parsed.venue,
                    parsed.period_start,
                    parsed.period_end,
                    parsed.days_covered,
                    filename,
                    digest,
                    PARSER_VERSION,
                ),
            ).fetchone()
            report_id = inserted[0]
            status = "loaded"

        if parsed.summary is not None:
            summary = parsed.summary
            conn.execute(
                """
                INSERT INTO core.report_summary (
                    report_id, label, units, gross_sales,
                    item_discounts, bill_discounts, net_sales
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    report_id,
                    summary.label,
                    summary.units,
                    summary.gross_sales,
                    summary.item_discounts,
                    summary.bill_discounts,
                    summary.net_sales,
                ),
            )

        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO core.sales_row (
                    report_id, venue, period_start, period_end,
                    item_name, item_key, sales_category, menu_group,
                    units, gross_sales, item_discounts, bill_discounts, net_sales
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        report_id,
                        parsed.venue,
                        parsed.period_start,
                        parsed.period_end,
                        row.item_name,
                        row.item_key,
                        row.sales_category,
                        row.menu_group,
                        row.units,
                        row.gross_sales,
                        row.item_discounts,
                        row.bill_discounts,
                        row.net_sales,
                    )
                    for row in parsed.rows
                ],
            )

        _log(
            conn,
            filename=filename,
            status=status,
            venue=parsed.venue,
            period_start=parsed.period_start,
            period_end=parsed.period_end,
            content_sha256=digest,
        )

    return status, None


def _failure_details(
    data: bytes,
    filename: str,
    exc: Exception,
    aliases: Mapping[str, str],
) -> dict[str, Any]:
    """Return log columns for a failed attempt, derived from bytes and filename.

    The hash is always available; the report identity is only filled in when a
    :class:`ReportRejected` supplies it (the file parsed but was refused) or when
    the filename itself matches the report contract. The venue is canonicalised
    so the log agrees with what a successful load would have written.
    """
    details: dict[str, Any] = {"content_sha256": hashlib.sha256(data).hexdigest()}

    venue = getattr(exc, "venue", None)
    period_start = getattr(exc, "period_start", None)
    period_end = getattr(exc, "period_end", None)

    if venue is None:
        try:
            venue, period_start, period_end = parse_filename(filename)
        except ParseError:
            return details

    details["venue"] = canonical_venue(venue, aliases)
    details["period_start"] = period_start
    details["period_end"] = period_end
    return details


def record_failure(
    conn: psycopg.Connection,
    filename: str,
    message: str,
    *,
    venue: str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    content_sha256: str | None = None,
) -> None:
    """Record a failed ingest without aborting the rest of a directory load."""
    with conn.transaction():
        _log(
            conn,
            filename=filename,
            status="failed",
            venue=venue,
            period_start=period_start,
            period_end=period_end,
            content_sha256=content_sha256,
            message=message,
        )


def record_file_failure(
    conn: psycopg.Connection,
    filename: str,
    data: bytes,
    exc: Exception,
    *,
    venue_aliases: Mapping[str, str] | None = None,
) -> None:
    """Record a failed attempt, deriving the hash and identity from ``data``."""
    aliases = (
        venue_aliases_from_env() if venue_aliases is None else venue_aliases
    )
    record_failure(
        conn,
        filename,
        str(exc),
        **_failure_details(data, filename, exc, aliases),
    )


def _matching_files(directory: Path, pattern: str) -> list[Path]:
    """Return files matching ``pattern``, ignoring case on the suffix.

    Email ingestion accepts ``*.CSV`` (``attachments.is_sales_report`` lowercases
    the name), so a directory scan that only matched ``*.csv`` would silently
    skip the same file dropped in ``data/raw/``.
    """
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and fnmatch.fnmatch(path.name.lower(), pattern.lower())
    )


def load_directory(
    conn: psycopg.Connection,
    directory: str | Path,
    *,
    pattern: str = "*.csv",
    require_week: bool = True,
    venue_aliases: Mapping[str, str] | None = None,
) -> list[LoadResult]:
    """Load every matching CSV in ``directory``, continuing after failures."""
    aliases = (
        venue_aliases_from_env() if venue_aliases is None else venue_aliases
    )
    results: list[LoadResult] = []
    for path in _matching_files(Path(directory), pattern):
        data = path.read_bytes()
        try:
            results.append(
                load_file(
                    conn,
                    data,
                    path.name,
                    require_week=require_week,
                    venue_aliases=aliases,
                )
            )
        except Exception as exc:  # noqa: BLE001 - report and continue
            record_file_failure(
                conn, path.name, data, exc, venue_aliases=aliases
            )
            results.append(
                LoadResult(filename=path.name, status="failed", message=str(exc))
            )
    return results
