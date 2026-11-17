"""Public entry point for parsing SalesByMenuItem CSV exports."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal

from .errors import FormatError, IntegrityError, RowParseError
from .filename import parse_filename
from .models import ParseResult, SalesRow, SummaryRow
from .money import parse_decimal, parse_percentage
from .normalise import normalise_key


@dataclass(frozen=True)
class _Layout:
    """One accepted CSV column layout."""

    name: str
    header: tuple[str, ...]
    columns: dict[str, int]
    tax_columns: tuple[int, ...]
    combined_discounts: bool = False


_STANDARD_HEADER = (
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
)

_TAX_SPLIT_HEADER = _STANDARD_HEADER[:-1] + ("Tax 1", "Tax 2", "Tax 3")

_LEGACY_HEADER = (
    "Menu Item",
    "Sales Category",
    "Menu Group",
    "Menu Item Void Quantity",
    "Voids",
    "Menu Item Quantity",
    "Gross Sales",
    "Discounts",
    "Net Sales",
    "Menu Item Quantity (Refunds)",
    "Refund Amount (Net Sales)",
    "Sales Percentage",
    "Tax 1",
    "Tax 2",
    "Tax 3",
)

_STANDARD_COLUMNS = {
    "void_quantity": 3,
    "voids": 4,
    "units": 5,
    "gross_sales": 6,
    "item_discounts": 7,
    "bill_discounts": 8,
    "net_sales": 9,
    "refund_quantity": 10,
    "refund_amount": 11,
    "sales_percentage": 12,
}

_LEGACY_COLUMNS = {
    "void_quantity": 3,
    "voids": 4,
    "units": 5,
    "gross_sales": 6,
    "discounts": 7,
    "net_sales": 8,
    "refund_quantity": 9,
    "refund_amount": 10,
    "sales_percentage": 11,
}

_LAYOUTS = (
    _Layout(
        name="standard",
        header=_STANDARD_HEADER,
        columns=_STANDARD_COLUMNS,
        tax_columns=(13,),
    ),
    _Layout(
        name="tax-split",
        header=_TAX_SPLIT_HEADER,
        columns=_STANDARD_COLUMNS,
        tax_columns=(13, 14, 15),
    ),
    _Layout(
        name="legacy",
        header=_LEGACY_HEADER,
        columns=_LEGACY_COLUMNS,
        tax_columns=(12, 13, 14),
        combined_discounts=True,
    ),
)

#: The standard 14-column header (kept for callers that need one concrete list).
HEADER = list(_STANDARD_HEADER)

#: Every header accepted by :func:`parse_csv`, as tuples.
ACCEPTED_HEADERS = tuple(layout.header for layout in _LAYOUTS)


def _find_layout(header: list[str]) -> _Layout:
    """Return the layout matching ``header`` or raise :class:`FormatError`."""
    for layout in _LAYOUTS:
        if tuple(header) == layout.header:
            return layout
    raise FormatError(f"CSV header does not match an accepted layout: {header!r}")


def _is_summary_row(row: list[str]) -> bool:
    """Return whether ``row`` is the trailing ``REPORT SUMMARY`` totals row."""
    return row[0].startswith("REPORT SUMMARY") or (
        row[1].strip() == "" and row[2].strip() == ""
    )


def _parse_row_values(
    row: list[str], layout: _Layout, line_number: int
) -> dict[str, Decimal]:
    """Parse every numeric/percentage column for one row.

    Values not represented in the public model are still parsed so malformed
    data is never silently ignored.
    """
    values: dict[str, Decimal] = {}
    for name, index in layout.columns.items():
        try:
            if name == "sales_percentage":
                parse_percentage(row[index])
            else:
                values[name] = parse_decimal(row[index])
        except RowParseError as exc:
            raise RowParseError(f"row {line_number}, {name!r}: {exc}") from exc

    for index in layout.tax_columns:
        try:
            parse_decimal(row[index])
        except RowParseError as exc:
            raise RowParseError(f"row {line_number}, tax column {index}: {exc}") from exc

    return values


def _discount_values(
    values: dict[str, Decimal], layout: _Layout
) -> tuple[Decimal, Decimal]:
    """Return ``(item_discounts, bill_discounts)`` for a layout."""
    if layout.combined_discounts:
        return values["discounts"], Decimal("0.00")
    return values["item_discounts"], values["bill_discounts"]


def _parse_sales_row(row: list[str], layout: _Layout, line_number: int) -> SalesRow:
    """Build one :class:`SalesRow` and enforce the row arithmetic identity."""
    item_name = row[0]
    values = _parse_row_values(row, layout, line_number)
    item_discounts, bill_discounts = _discount_values(values, layout)

    gross_sales = values["gross_sales"]
    net_sales = values["net_sales"]

    if gross_sales - item_discounts - bill_discounts != net_sales:
        raise IntegrityError(
            f"row {line_number}: gross - discounts != net sales for {item_name!r}"
        )

    return SalesRow(
        item_name=item_name,
        item_key=normalise_key(item_name),
        sales_category=row[1].strip(),
        menu_group=row[2],
        units=values["units"],
        gross_sales=gross_sales,
        item_discounts=item_discounts,
        bill_discounts=bill_discounts,
        net_sales=net_sales,
    )


def _parse_summary_row(
    row: list[str], layout: _Layout, line_number: int
) -> SummaryRow:
    """Build the :class:`SummaryRow` from the trailing totals row."""
    values = _parse_row_values(row, layout, line_number)
    item_discounts, bill_discounts = _discount_values(values, layout)

    return SummaryRow(
        label=row[0],
        units=values["units"],
        gross_sales=values["gross_sales"],
        item_discounts=item_discounts,
        bill_discounts=bill_discounts,
        net_sales=values["net_sales"],
    )


def parse_csv(data: bytes, filename: str) -> ParseResult:
    """Parse a SalesByMenuItem CSV export into a :class:`ParseResult`.

    Pure function: the same ``data`` and ``filename`` always produce the same
    result, with no I/O, global state, or clock reads.

    Three header layouts are accepted: the standard 14-column export, a
    16-column export that splits the tax column into ``Tax 1/2/3``, and the
    legacy 15-column export with one combined ``Discounts`` column.
    """
    venue, period_start, period_end = parse_filename(filename)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormatError("CSV is not valid UTF-8") from exc

    reader = csv.reader(io.StringIO(text, newline=""))

    try:
        header = next(reader)
    except StopIteration as exc:
        raise FormatError("CSV is empty") from exc

    layout = _find_layout(header)
    warnings: list[str] = []
    if layout.combined_discounts:
        warnings.append(
            "legacy combined-discount layout: 'Discounts' stored as "
            "item_discounts with bill_discounts = 0.00"
        )

    rows: list[SalesRow] = []
    summary: SummaryRow | None = None
    seen_keys: dict[tuple[str, str, str], int] = {}

    for line_number, raw in enumerate(reader, start=2):
        if len(raw) != len(layout.header):
            raise RowParseError(
                f"row {line_number}: expected {len(layout.header)} columns, "
                f"got {len(raw)}"
            )

        if _is_summary_row(raw):
            if summary is not None:
                raise IntegrityError(
                    f"row {line_number}: more than one REPORT SUMMARY row"
                )
            summary = _parse_summary_row(raw, layout, line_number)
            continue

        row = _parse_sales_row(raw, layout, line_number)
        natural_key = (row.item_name, row.sales_category, row.menu_group)
        first_line = seen_keys.get(natural_key)
        if first_line is not None:
            raise IntegrityError(
                f"row {line_number}: duplicate natural key {natural_key!r} "
                f"(first seen on row {first_line})"
            )
        seen_keys[natural_key] = line_number
        rows.append(row)

    if summary is not None:
        total_units = sum((row.units for row in rows), Decimal("0"))
        if total_units != summary.units:
            raise IntegrityError(
                "summary units do not equal the sum of body-row units "
                f"({total_units} != {summary.units})"
            )

    days_covered = (period_end - period_start).days + 1

    return ParseResult(
        venue=venue,
        period_start=period_start,
        period_end=period_end,
        days_covered=days_covered,
        rows=rows,
        summary=summary,
        warnings=warnings,
    )
