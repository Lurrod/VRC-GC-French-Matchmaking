"""Tests des helpers Riot ID."""

import pytest

from services.riot_id import parse_riot_id


# ── parse_riot_id ─────────────────────────────────────────────────
@pytest.mark.parametrize("raw,name,tag", [
    ("Player#EUW",      "Player",      "EUW"),
    ("Player#1234",     "Player",      "1234"),
    ("Some Player#FR",  "Some Player", "FR"),
    ("  Pad #fr ",      "Pad",         "fr"),
])
def test_parse_riot_id_valid(raw, name, tag):
    assert parse_riot_id(raw) == (name, tag)


@pytest.mark.parametrize("raw", [
    "",
    "no-tag",
    "#tag-only",
    "name#",
    "  #  ",
    "name#way-too-long-tag",
    None,
    123,
])
def test_parse_riot_id_invalid_raises(raw):
    with pytest.raises(ValueError):
        parse_riot_id(raw)
