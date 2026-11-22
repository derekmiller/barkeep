"""Canonical venue identities for reports whose filenames disagree.

A manual export from the TouchBistro web UI names the venue
(``Bay_Horse_Tavern-SalesByMenuItem-...``), while the scheduled email attachment
names the location id (``54011-SalesByMenuItem-...``). Both describe one bar, so
the loader maps the id onto the name before it derives the report identity.

The mapping is configuration, not inference: nothing here guesses that two names
are the same venue.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from .errors import LoaderError

#: Environment variable holding ``"from=to[,from=to]"`` pairs.
ALIAS_ENV = "BARKEEP_VENUE_ALIASES"

_ENTRY_SPLIT = re.compile(r"[,;\n]")


def parse_venue_aliases(value: str | None) -> dict[str, str]:
    """Parse ``"54011=Bay_Horse_Tavern,..."`` into a mapping.

    Entries are whitespace-trimmed; empty entries are ignored. Anything that is
    not ``from=to`` with both sides non-empty raises :class:`LoaderError`, so a
    typo fails loudly instead of silently duplicating a venue.
    """
    aliases: dict[str, str] = {}
    if not value:
        return aliases

    for raw in _ENTRY_SPLIT.split(value):
        entry = raw.strip()
        if not entry:
            continue
        source, separator, target = entry.partition("=")
        source, target = source.strip(), target.strip()
        if not separator or not source or not target:
            raise LoaderError(
                f"{ALIAS_ENV}: expected 'from=to', got {entry!r}"
            )
        if source == target:
            raise LoaderError(
                f"{ALIAS_ENV}: {source!r} is mapped to itself"
            )
        aliases[source] = target

    return aliases


def canonical_venue(venue: str, aliases: Mapping[str, str]) -> str:
    """Follow ``aliases`` from ``venue`` to its canonical name.

    Chains are resolved (``a=b, b=c`` yields ``c``); a cycle raises
    :class:`LoaderError` rather than looping.
    """
    current = venue
    seen = {venue}

    while current in aliases:
        current = aliases[current]
        if current in seen:
            raise LoaderError(
                f"{ALIAS_ENV}: alias cycle involving {current!r}"
            )
        seen.add(current)

    return current


def venue_aliases_from_env(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the configured venue aliases, or an empty mapping."""
    env = os.environ if environ is None else environ
    return parse_venue_aliases(env.get(ALIAS_ENV))
