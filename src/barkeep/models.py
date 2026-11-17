"""Typed data model produced by :func:`barkeep.parse.parse_csv`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class SalesRow:
    """One menu-item row from a SalesByMenuItem export.

    ``units`` holds the exported ``Menu Item Quantity``, which is **gross
    units** — units sold before refunds. Refund quantities are deliberately not
    subtracted. ``net_sales`` is a revenue figure (after discounts), not a unit
    count; units and revenue legitimately diverge and neither may be inferred
    from the other.

    ``item_name`` is the display string exactly as exported. It is not a stable
    identifier: menu items can be renamed over time. ``item_key`` is a
    normalised comparison key (NFC, curly apostrophes folded to ASCII, runs of
    whitespace collapsed) and is not intended as a replacement identity.

    ``menu_group`` is stored raw, including any embedded newline. It describes
    how staff rang the item and is unreliable for classification.
    """

    item_name: str
    item_key: str
    sales_category: str
    menu_group: str
    units: Decimal
    gross_sales: Decimal
    item_discounts: Decimal
    bill_discounts: Decimal
    net_sales: Decimal


@dataclass(frozen=True)
class SummaryRow:
    """The trailing ``REPORT SUMMARY (N entries)`` totals row.

    Kept for cross-checking only. It must never be included in
    :attr:`ParseResult.rows`.
    """

    label: str
    units: Decimal
    gross_sales: Decimal
    item_discounts: Decimal
    bill_discounts: Decimal
    net_sales: Decimal


@dataclass(frozen=True)
class ParseResult:
    """The complete parsed report, including its filename-derived period."""

    venue: str
    period_start: date
    period_end: date
    days_covered: int
    rows: list[SalesRow]
    summary: SummaryRow | None
    warnings: list[str]
