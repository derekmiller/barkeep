"""Venue alias parsing, with no database and no I/O."""

from __future__ import annotations

import pytest

from barkeep.loader import LoaderError
from barkeep.loader.venues import (
    ALIAS_ENV,
    canonical_venue,
    parse_venue_aliases,
    venue_aliases_from_env,
)


def test_empty_or_missing_configuration_means_no_aliases() -> None:
    assert parse_venue_aliases(None) == {}
    assert parse_venue_aliases("") == {}
    assert parse_venue_aliases("  ,  \n ") == {}


def test_pairs_are_parsed_and_trimmed() -> None:
    assert parse_venue_aliases(" 54011 = Bay_Horse_Tavern , 54012=Other ") == {
        "54011": "Bay_Horse_Tavern",
        "54012": "Other",
    }


def test_an_entry_without_an_equals_sign_is_rejected() -> None:
    with pytest.raises(LoaderError, match="expected 'from=to'"):
        parse_venue_aliases("54011")


def test_an_entry_with_an_empty_side_is_rejected() -> None:
    with pytest.raises(LoaderError, match="expected 'from=to'"):
        parse_venue_aliases("54011=")
    with pytest.raises(LoaderError, match="expected 'from=to'"):
        parse_venue_aliases("=Bay_Horse_Tavern")


def test_mapping_a_venue_to_itself_is_rejected() -> None:
    with pytest.raises(LoaderError, match="mapped to itself"):
        parse_venue_aliases("Bay_Horse_Tavern=Bay_Horse_Tavern")


def test_canonical_venue_follows_chains() -> None:
    aliases = {"a": "b", "b": "c"}

    assert canonical_venue("a", aliases) == "c"
    assert canonical_venue("c", aliases) == "c"


def test_canonical_venue_rejects_a_cycle() -> None:
    with pytest.raises(LoaderError, match="alias cycle"):
        canonical_venue("a", {"a": "b", "b": "a"})


def test_aliases_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(ALIAS_ENV, "54011=Bay_Horse_Tavern")

    assert venue_aliases_from_env() == {"54011": "Bay_Horse_Tavern"}
