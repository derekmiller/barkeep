"""Postgres loader tests. Run with ``pytest -m db`` and ``DATABASE_URL`` set."""

from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import psycopg.conninfo
import pytest

from barkeep.loader import (
    LoaderError,
    apply_migrations,
    load_directory,
    load_file,
)
from barkeep.loader.errors import ReportRejected

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WEEKLY_FILENAME = "Example_Venue-SalesByMenuItem-2026-11-08-2026-11-14.csv"
DAILY_FILENAME = "Example_Venue-SalesByMenuItem-2026-11-15-2026-11-15.csv"


@pytest.fixture()
def conn():
    base_dsn = os.environ.get("DATABASE_URL")
    if not base_dsn:
        pytest.skip("DATABASE_URL is not set")

    params = psycopg.conninfo.conninfo_to_dict(base_dsn)
    database = f"barkeep_test_{uuid4().hex[:10]}"
    admin_params = {**params, "dbname": "postgres"}

    admin = psycopg.connect(**admin_params, autocommit=True)
    admin.execute(f'CREATE DATABASE "{database}"')
    test_dsn = psycopg.conninfo.make_conninfo(**{**params, "dbname": database})
    connection = psycopg.connect(test_dsn)

    try:
        apply_migrations(connection)
        yield connection
    finally:
        connection.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        admin.close()


def _weekly_bytes() -> bytes:
    return (FIXTURES / WEEKLY_FILENAME).read_bytes()


def test_load_is_idempotent_and_replaces_revisions(conn: psycopg.Connection) -> None:
    data = _weekly_bytes()

    first = load_file(conn, data, WEEKLY_FILENAME)
    assert first.status == "loaded"

    second = load_file(conn, data, WEEKLY_FILENAME)
    assert second.status == "skipped"

    # Different bytes for the same period replace the report atomically.
    replacement = data.replace(b'"3.33%"', b'"3.34%"')
    third = load_file(conn, replacement, WEEKLY_FILENAME)
    assert third.status == "replaced"

    assert conn.execute("SELECT count(*) FROM core.report").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM core.sales_row").fetchone()[0] == 10
    assert (
        conn.execute("SELECT count(*) FROM core.report_summary").fetchone()[0]
        == 1
    )
    statuses = [
        row[0]
        for row in conn.execute(
            "SELECT status FROM core.ingest_log ORDER BY id"
        ).fetchall()
    ]
    assert statuses == ["loaded", "skipped", "replaced"]


def test_views_expose_expected_totals_and_clean_menu_group(
    conn: psycopg.Connection,
) -> None:
    load_file(conn, _weekly_bytes(), WEEKLY_FILENAME)

    units, gross, net = conn.execute(
        """
        SELECT SUM(units), SUM(gross_sales), SUM(net_sales)
        FROM analytics.v_sales_current
        """
    ).fetchone()
    assert units == Decimal("55.00")
    assert gross == Decimal("1502.56")
    assert net == Decimal("1489.56")

    raw_group, clean_group = conn.execute(
        """
        SELECT menu_group_raw, menu_group
        FROM analytics.v_sales_current
        WHERE item_name = 'Gamma Soda' AND menu_group_raw LIKE %s
        """,
        ("%Important%",),
    ).fetchone()
    assert "\n" in raw_group
    assert "\n" not in clean_group
    assert '"Important" Beers & Seltzers' == clean_group


def test_non_weekly_report_is_rejected(conn: psycopg.Connection) -> None:
    data = (FIXTURES / DAILY_FILENAME).read_bytes()

    with pytest.raises(LoaderError, match="full Sunday-Saturday week"):
        load_file(conn, data, DAILY_FILENAME)


def test_non_weekly_rejection_carries_the_report_identity(
    conn: psycopg.Connection,
) -> None:
    data = (FIXTURES / DAILY_FILENAME).read_bytes()

    with pytest.raises(ReportRejected) as excinfo:
        load_file(conn, data, DAILY_FILENAME)

    rejected = excinfo.value
    assert rejected.venue == "Example_Venue"
    assert str(rejected.period_start) == "2026-11-15"
    assert str(rejected.period_end) == "2026-11-15"
    assert rejected.content_sha256 == hashlib.sha256(data).hexdigest()


def test_failed_load_logs_hash_and_identity(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    daily = (FIXTURES / DAILY_FILENAME).read_bytes()
    (tmp_path / DAILY_FILENAME).write_bytes(daily)

    load_directory(conn, tmp_path)

    venue, period_start, period_end, digest, version = conn.execute(
        """
        SELECT venue, period_start, period_end, content_sha256, parser_version
        FROM core.ingest_log WHERE status = 'failed'
        """
    ).fetchone()

    assert venue == "Example_Venue"
    assert str(period_start) == "2026-11-15"
    assert str(period_end) == "2026-11-15"
    assert digest == hashlib.sha256(daily).hexdigest()
    assert version


def test_unparseable_load_still_logs_its_hash(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    (tmp_path / WEEKLY_FILENAME).write_bytes(b"not,a,report\n")

    load_directory(conn, tmp_path)

    digest, venue = conn.execute(
        "SELECT content_sha256, venue FROM core.ingest_log WHERE status = 'failed'"
    ).fetchone()

    # The bytes are always hashed; the identity falls back to the filename.
    assert digest == hashlib.sha256(b"not,a,report\n").hexdigest()
    assert venue == "Example_Venue"


def test_failure_with_unparseable_filename_logs_hash_but_no_identity(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    (tmp_path / "garbage.csv").write_bytes(b"not,a,report\n")

    load_directory(conn, tmp_path)

    digest, venue, period_start = conn.execute(
        """
        SELECT content_sha256, venue, period_start
        FROM core.ingest_log WHERE status = 'failed'
        """
    ).fetchone()

    assert digest == hashlib.sha256(b"not,a,report\n").hexdigest()
    assert venue is None
    assert period_start is None


def test_load_directory_continues_after_failure(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    (tmp_path / WEEKLY_FILENAME).write_bytes(_weekly_bytes())
    (tmp_path / DAILY_FILENAME).write_bytes(
        (FIXTURES / DAILY_FILENAME).read_bytes()
    )

    results = load_directory(conn, tmp_path)
    statuses = {result.filename: result.status for result in results}

    assert statuses[WEEKLY_FILENAME] == "loaded"
    assert statuses[DAILY_FILENAME] == "failed"
    assert conn.execute("SELECT count(*) FROM core.sales_row").fetchone()[0] == 10
    failed = conn.execute(
        "SELECT count(*) FROM core.ingest_log WHERE status = 'failed'"
    ).fetchone()[0]
    assert failed == 1
