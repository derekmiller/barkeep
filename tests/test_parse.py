"""Acceptance tests for ``barkeep.parse.parse_csv``.

These tests are written against the golden values in ``AGENTS.md`` section 10
and the coverage requirements in section 11, with additional guard tests for the
header, row-shape, filename, and legacy-layout paths.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path

import pytest

from barkeep.errors import (
    FilenameParseError,
    FormatError,
    IntegrityError,
    RowParseError,
)
from barkeep.models import ParseResult, SalesRow
from barkeep.parse import parse_csv

FIXTURES = Path(__file__).parent / "fixtures"

WEEKLY_FILENAME = "Example_Venue-SalesByMenuItem-2026-11-08-2026-11-14.csv"
DAILY_FILENAME = "Example_Venue-SalesByMenuItem-2026-11-15-2026-11-15.csv"
EDGE_FILENAME = "Example_Venue-SalesByMenuItem-2026-11-16-2026-11-16.csv"
LEGACY_FILENAME = "Test_Venue-SalesByMenuItem-2024-06-28-2024-07-04.csv"
TAX_SPLIT_FILENAME = "Test_Venue-SalesByMenuItem-2025-09-28-2025-10-04.csv"

HEADER = [
    "Menu Item",
    "Sales Category",
    "Menu Group",
    "Menu Item Void Quantity",
    "Voids",
    "Menu Item Quantity",
    "Gross Sales",
    "Item Discounts",
    "Bill Discounts",
    "Net Sales",
    "Menu Item Quantity (Refunds)",
    "Refund Amount (Net Sales)",
    "Sales Percentage",
    "Total Tax",
]


def _parse(filename: str) -> ParseResult:
    data = (FIXTURES / filename).read_bytes()
    return parse_csv(data, filename)


def _named(result: ParseResult, item_name: str) -> list[SalesRow]:
    return [row for row in result.rows if row.item_name == item_name]


def _csv_bytes(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


VALID_ROW = [
    "Test Item",
    "Alcohol",
    "Wells",
    "0.00",
    "0.00",
    "1.00",
    "10.00",
    "2.00",
    "0.00",
    "8.00",
    "0.00",
    "0.00",
    "100.00%",
    "0.00",
]

VALID_SUMMARY = [
    "REPORT SUMMARY (1 entries)",
    "",
    "",
    "0.00",
    "0.00",
    "1.00",
    "10.00",
    "2.00",
    "0.00",
    "8.00",
    "0.00",
    "0.00",
    "100.00%",
    "0.00",
]


# ---------------------------------------------------------------------------
# 1. Both fixtures parse; all golden values match.
# ---------------------------------------------------------------------------


def test_weekly_fixture_golden_values() -> None:
    result = _parse(WEEKLY_FILENAME)

    assert result.venue == "Example_Venue"
    assert result.period_start.isoformat() == "2026-11-08"
    assert result.period_end.isoformat() == "2026-11-14"
    assert result.days_covered == 7

    assert len(result.rows) == 10
    assert len({row.item_name for row in result.rows}) == 8

    assert sum(row.units for row in result.rows) == Decimal("55.00")
    assert sum(row.gross_sales for row in result.rows) == Decimal("1502.56")
    assert sum(row.item_discounts for row in result.rows) == Decimal("13.00")
    assert sum(row.bill_discounts for row in result.rows) == Decimal("0.00")
    assert sum(row.net_sales for row in result.rows) == Decimal("1489.56")

    assert result.summary is not None
    assert result.summary.label.startswith("REPORT SUMMARY")
    assert result.summary.units == Decimal("55.00")
    assert result.summary.gross_sales == Decimal("1502.56")
    assert result.summary.item_discounts == Decimal("13.00")
    assert result.summary.bill_discounts == Decimal("0.00")
    assert result.summary.net_sales == Decimal("1489.56")


def test_daily_fixture_golden_values() -> None:
    result = _parse(DAILY_FILENAME)

    assert result.venue == "Example_Venue"
    assert result.period_start.isoformat() == "2026-11-15"
    assert result.period_end.isoformat() == "2026-11-15"
    assert result.days_covered == 1

    assert len(result.rows) == 4
    assert len({row.item_name for row in result.rows}) == 3

    assert sum(row.units for row in result.rows) == Decimal("8.00")
    assert sum(row.gross_sales for row in result.rows) == Decimal("31.00")
    assert sum(row.item_discounts for row in result.rows) == Decimal("0.00")
    assert sum(row.bill_discounts for row in result.rows) == Decimal("0.00")
    assert sum(row.net_sales for row in result.rows) == Decimal("31.00")

    assert result.summary is not None
    assert result.summary.label.startswith("REPORT SUMMARY")
    assert result.summary.units == Decimal("8.00")


# ---------------------------------------------------------------------------
# 2. Summary row excluded (no exact double-count).
# ---------------------------------------------------------------------------


def test_summary_row_excluded_weekly() -> None:
    result = _parse(WEEKLY_FILENAME)

    assert len(result.rows) == 10
    assert sum(row.units for row in result.rows) == Decimal("55.00")
    # The summary row itself must never appear in rows.
    assert not any(row.item_name.startswith("REPORT SUMMARY") for row in result.rows)


def test_summary_row_excluded_daily() -> None:
    result = _parse(DAILY_FILENAME)

    assert len(result.rows) == 4
    assert sum(row.units for row in result.rows) == Decimal("8.00")
    assert not any(row.item_name.startswith("REPORT SUMMARY") for row in result.rows)


# ---------------------------------------------------------------------------
# 3. days_covered is derived and must not assume a week.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected_days"),
    [(WEEKLY_FILENAME, 7), (DAILY_FILENAME, 1)],
)
def test_days_covered(filename: str, expected_days: int) -> None:
    assert _parse(filename).days_covered == expected_days


# ---------------------------------------------------------------------------
# 4. Embedded newline inside a quoted menu group.
# ---------------------------------------------------------------------------


def test_embedded_newline_in_menu_group_is_one_field() -> None:
    result = _parse(WEEKLY_FILENAME)
    rows = _named(result, "Gamma Soda")

    assert rows, "expected at least one Gamma Soda row"
    assert any(row.menu_group == '"Important" Beers &\nSeltzers' for row in rows)


# ---------------------------------------------------------------------------
# 5. Thousands separators in the summary row.
# ---------------------------------------------------------------------------


def test_thousands_separators_in_summary() -> None:
    result = _parse(WEEKLY_FILENAME)

    assert result.summary is not None
    # These values are exported with commas, e.g. "1,502.56".
    assert result.summary.units == Decimal("55.00")
    assert result.summary.gross_sales == Decimal("1502.56")
    assert result.summary.net_sales == Decimal("1489.56")


# ---------------------------------------------------------------------------
# 6. Fully comped item: gross units, zero revenue.
# ---------------------------------------------------------------------------


def test_zero_net_item_keeps_gross_units_and_zero_net_sales() -> None:
    result = _parse(WEEKLY_FILENAME)
    rows = _named(result, "Zero Net Item")

    assert len(rows) == 1
    assert rows[0].units == Decimal("1.00")
    assert rows[0].net_sales == Decimal("0.00")


# ---------------------------------------------------------------------------
# 7. item_key normalisation: apostrophes folded, accents preserved.
# ---------------------------------------------------------------------------


def test_curly_apostrophe_is_folded_in_item_key() -> None:
    result = _parse(WEEKLY_FILENAME)
    rows = [
        row
        for row in result.rows
        if row.item_name == "Rye\u2019s - Pint"
    ]

    assert len(rows) == 1
    row = rows[0]
    assert "\u2019" in row.item_name
    assert row.item_key == "Rye's - Pint"


def test_accent_is_preserved_in_item_key() -> None:
    result = _parse(WEEKLY_FILENAME)
    rows = _named(result, "Caf\u00e9 Blanco")

    assert rows
    assert rows[0].item_key == "Caf\u00e9 Blanco"


# ---------------------------------------------------------------------------
# 8. Duplicate names are preserved, not merged.
# ---------------------------------------------------------------------------


def test_alpha_lager_is_not_merged() -> None:
    result = _parse(WEEKLY_FILENAME)
    rows = _named(result, "Alpha Lager")

    assert len(rows) == 2
    assert sum(row.units for row in rows) == Decimal("15.00")


@pytest.mark.parametrize(
    ("item_name", "expected_rows", "expected_units"),
    [
        ("Gamma Soda", 2, Decimal("12.00")),
        ("Alpha Lager", 2, Decimal("15.00")),
    ],
)
def test_grouped_by_name_aggregates(
    item_name: str, expected_rows: int, expected_units: Decimal
) -> None:
    result = _parse(WEEKLY_FILENAME)
    rows = _named(result, item_name)

    assert len(rows) == expected_rows
    assert sum(row.units for row in rows) == expected_units


# ---------------------------------------------------------------------------
# 9. Malformed filename.
# ---------------------------------------------------------------------------


def test_malformed_filename_raises() -> None:
    # A valid CSV body, so only the filename contract is at fault.
    data = (FIXTURES / WEEKLY_FILENAME).read_bytes()

    with pytest.raises(FilenameParseError):
        parse_csv(data, "nonsense.csv")


# ---------------------------------------------------------------------------
# 10. A row violating gross - item_discounts - bill_discounts == net_sales.
# ---------------------------------------------------------------------------


def test_row_arithmetic_violation_raises_integrity_error() -> None:
    # 10.00 - 2.00 - 0.00 == 8.00, but the row claims net_sales == 9.00.
    body_row = [
        "Bad Row",
        "Alcohol",
        "Wells",
        "0.00",
        "0.00",
        "1.00",
        "10.00",
        "2.00",
        "0.00",
        "9.00",
        "0.00",
        "0.00",
        "100.00",
        "0.00",
    ]
    summary_row = [
        "REPORT SUMMARY (1 entries)",
        "",
        "",
        "0.00",
        "0.00",
        "1.00",
        "10.00",
        "2.00",
        "0.00",
        "9.00",
        "0.00",
        "0.00",
        "100.00",
        "0.00",
    ]

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerow(body_row)
    writer.writerow(summary_row)
    data = buffer.getvalue().encode("utf-8")

    with pytest.raises(IntegrityError):
        parse_csv(data, WEEKLY_FILENAME)


# ---------------------------------------------------------------------------
# Additional guard tests: header, row shape, filename, and optional summary.
# ---------------------------------------------------------------------------


def test_reordered_header_raises_format_error() -> None:
    header = HEADER.copy()
    header[0], header[1] = header[1], header[0]

    with pytest.raises(FormatError):
        parse_csv(_csv_bytes([header, VALID_ROW, VALID_SUMMARY]), WEEKLY_FILENAME)


def test_missing_header_column_raises_format_error() -> None:
    with pytest.raises(FormatError):
        parse_csv(
            _csv_bytes([HEADER[:-1], VALID_ROW, VALID_SUMMARY]),
            WEEKLY_FILENAME,
        )


def test_empty_csv_raises_format_error() -> None:
    with pytest.raises(FormatError):
        parse_csv(b"", WEEKLY_FILENAME)


def test_wrong_row_width_raises_row_parse_error() -> None:
    with pytest.raises(RowParseError):
        parse_csv(
            _csv_bytes([HEADER, VALID_ROW[:-1], VALID_SUMMARY]),
            WEEKLY_FILENAME,
        )


def test_invalid_filename_date_raises() -> None:
    with pytest.raises(FilenameParseError):
        parse_csv(
            _csv_bytes([HEADER, VALID_ROW, VALID_SUMMARY]),
            "Example_Venue-SalesByMenuItem-2026-13-40-2026-11-14.csv",
        )


def test_missing_summary_row_yields_none() -> None:
    result = parse_csv(
        _csv_bytes([HEADER, VALID_ROW]),
        WEEKLY_FILENAME,
    )

    assert result.summary is None
    assert len(result.rows) == 1
    assert result.rows[0].units == Decimal("1.00")


# ---------------------------------------------------------------------------
# Edge cases observed in real exports, kept as a committed fixture so they do
# not depend on the gitignored corpus in data/raw.
# ---------------------------------------------------------------------------


def test_edge_case_fixture_parses() -> None:
    result = _parse(EDGE_FILENAME)

    assert result.venue == "Example_Venue"
    assert result.days_covered == 1
    assert len(result.rows) == 3

    assert sum(row.units for row in result.rows) == Decimal("1203.00")
    assert sum(row.gross_sales for row in result.rows) == Decimal("1274.00")
    assert sum(row.item_discounts for row in result.rows) == Decimal("19.50")
    assert sum(row.net_sales for row in result.rows) == Decimal("1254.50")

    assert result.summary is not None
    assert result.summary.units == Decimal("1203.00")
    assert result.summary.gross_sales == Decimal("1274.00")
    assert result.summary.net_sales == Decimal("1254.50")


def test_refunds_and_voids_do_not_reduce_gross_units() -> None:
    result = _parse(EDGE_FILENAME)
    row = _named(result, "Alpha Lager")[0]

    # Menu Item Quantity is gross units; the refund and void columns are
    # separate fields and must not change it.
    assert row.units == Decimal("24.00")
    assert row.gross_sales == Decimal("83.00")
    assert row.net_sales == Decimal("79.50")


def test_negative_net_sales_parses() -> None:
    result = _parse(EDGE_FILENAME)
    row = _named(result, "Beta Wine")[0]

    # Discounts can exceed gross sales, producing a negative net.
    assert row.item_discounts == Decimal("16.00")
    assert row.net_sales == Decimal("-2.00")


def test_thousands_separator_in_body_units() -> None:
    result = _parse(EDGE_FILENAME)
    row = _named(result, "Gamma Soda")[0]

    assert row.units == Decimal("1177.00")
    assert row.net_sales == Decimal("1177.00")


def test_sales_category_is_trimmed_and_unexpected_values_preserved() -> None:
    def _row(name: str, category: str) -> list[str]:
        return [
            name,
            category,
            "Misc",
            "0.00",
            "0.00",
            "1.00",
            "10.00",
            "0.00",
            "0.00",
            "10.00",
            "0.00",
            "0.00",
            "100.00%",
            "0.00",
        ]

    summary = [
        "REPORT SUMMARY (3 entries)",
        "",
        "",
        "0.00",
        "0.00",
        "3.00",
        "30.00",
        "0.00",
        "0.00",
        "30.00",
        "0.00",
        "0.00",
        "100.00%",
        "0.00",
    ]
    data = _csv_bytes(
        [
            HEADER,
            _row("Item A", "Alcohol "),
            _row("Item B", "UNCATEGORIZED"),
            _row("Item C", "Merch"),
            summary,
        ]
    )

    result = parse_csv(data, WEEKLY_FILENAME)

    assert [row.sales_category for row in result.rows] == [
        "Alcohol",
        "UNCATEGORIZED",
        "Merch",
    ]


# ---------------------------------------------------------------------------
# Legacy layouts: 16-column tax split and 15-column combined discounts.
# ---------------------------------------------------------------------------


def test_tax_split_layout_parses() -> None:
    result = _parse(TAX_SPLIT_FILENAME)

    assert result.venue == "Test_Venue"
    assert result.days_covered == 7
    assert len(result.rows) == 1
    assert result.warnings == []

    row = _named(result, "Split Item")[0]
    assert row.sales_category == "Alcohol"
    assert row.units == Decimal("2.00")
    assert row.gross_sales == Decimal("12.00")
    assert row.item_discounts == Decimal("1.00")
    assert row.bill_discounts == Decimal("0.00")
    assert row.net_sales == Decimal("11.00")

    assert result.summary is not None
    assert result.summary.units == Decimal("2.00")
    assert result.summary.net_sales == Decimal("11.00")


def test_legacy_combined_discount_layout_parses() -> None:
    result = _parse(LEGACY_FILENAME)

    assert result.venue == "Test_Venue"
    assert result.days_covered == 7
    assert len(result.rows) == 2

    row = _named(result, "Legacy Lager")[0]
    # Legacy category values have trailing whitespace; it is trimmed.
    assert row.sales_category == "Alcohol"
    assert row.units == Decimal("3.00")
    assert row.gross_sales == Decimal("15.00")
    assert row.item_discounts == Decimal("3.00")
    assert row.bill_discounts == Decimal("0.00")
    assert row.net_sales == Decimal("12.00")

    # Gross units are not reduced by the refund row.
    refund_row = _named(result, "Legacy Refund")[0]
    assert refund_row.units == Decimal("2.00")
    assert refund_row.net_sales == Decimal("10.00")

    assert result.summary is not None
    assert result.summary.units == Decimal("5.00")
    assert result.summary.gross_sales == Decimal("25.00")
    assert result.summary.item_discounts == Decimal("3.00")
    assert result.summary.bill_discounts == Decimal("0.00")
    assert result.summary.net_sales == Decimal("22.00")

    assert len(result.warnings) == 1
    assert "combined" in result.warnings[0].lower()
