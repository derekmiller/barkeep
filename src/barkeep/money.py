"""Decimal parsing for exported money and quantity fields."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from barkeep.errors import RowParseError


def parse_decimal(value: str) -> Decimal:
    """Parse an exported numeric field as :class:`Decimal`.

    Thousands separators and dollar signs are stripped unconditionally (only
    some layouts use them, and the parser must not special-case one row). The
    value is never converted through ``float``.
    """
    cleaned = value.strip().replace(",", "").replace("$", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise RowParseError(f"not a decimal value: {value!r}") from exc


def parse_percentage(value: str) -> Decimal:
    """Parse a percentage field such as ``"99.99%"`` into ``Decimal("99.99")``.

    The report pre-rounds this column, so callers must never validate or
    aggregate it.
    """
    cleaned = value.strip().replace(",", "")
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    return parse_decimal(cleaned)
