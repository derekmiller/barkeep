"""Normalisation of display names into comparison keys."""

from __future__ import annotations

import re
import unicodedata

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'"})
_WHITESPACE = re.compile(r"\s+")


def normalise_key(value: str) -> str:
    """Return a stable comparison key for a display string.

    Applies Unicode NFC, maps curly apostrophes to ASCII ``'``, and collapses
    whitespace runs (including embedded newlines) to a single space. It does
    **not** lowercase, strip accents, remove punctuation, or otherwise alter the
    text, because doing so risks merging genuinely different menu items.
    """
    normalised = unicodedata.normalize("NFC", value).translate(_APOSTROPHES)
    return _WHITESPACE.sub(" ", normalised).strip()
