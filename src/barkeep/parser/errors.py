"""Exception hierarchy for the barkeep parser."""

from __future__ import annotations


class ParseError(Exception):
    """Base class for all parser failures."""


class FilenameParseError(ParseError):
    """The filename does not match the report filename contract."""


class FormatError(ParseError):
    """The CSV header or overall structure is not the expected format."""


class IntegrityError(ParseError):
    """A row or report failed an arithmetic consistency check."""


class RowParseError(ParseError):
    """A single data row could not be parsed."""
