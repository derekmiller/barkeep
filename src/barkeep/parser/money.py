"""Decimal parsing for exported money and quantity fields."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .errors import RowParseError


def parse_decimal(value: str) -> Decimal:
    """Parse an exported numeric field as :class:`Decimal`.

    Thousands separators and dollar signs are stripped unconditionally (only
    some layouts use them, and the parser must not special-case one row). The
    value is never converted through ``float``.

    Non-finite values (``NaN``, ``Infinity``, ``sNaN``) are rejected, and so are
    digit separators such as ``1_000`` that :class:`Decimal` would otherwise
    accept. A ``NaN`` slipping through would be stored by Postgres and silently
    poison every ``SUM`` built on the column.
    """
    cleaned = value.strip().replace(",", "").replace("$", "")
    if "_" in cleaned:
        raise RowParseError(f"not a decimal value: {value!r}")
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation as exc:
        raise RowParseError(f"not a decimal value: {value!r}") from exc
    if not parsed.is_finite():
        raise RowParseError(f"not a finite decimal value: {value!r}")
    return parsed


def parse_percentage(value: str) -> Decimal:
    """Parse a percentage field such as ``"99.99%"`` into ``Decimal("99.99")``.

    The report pre-rounds this column, so callers must never validate or
    aggregate it.
    """
    cleaned = value.strip().replace(",", "")
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    return parse_decimal(cleaned)
