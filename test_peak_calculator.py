"""Tests de la regle 6 mois — coeur de la V2."""

from datetime import datetime, timedelta, timezone

import pytest

from services.peak_calculator import (
    MatchEntry,
    compute_effective_elo,
    parse_riot_id,
    SIX_MONTHS,
)


NOW = datetime(2026, 4, 25, tzinfo=timezone.utc)


def _entry(elo: int, days_ago: int) -> MatchEntry:
    return MatchEntry(elo=elo, date=NOW - timedelta(days=days_ago))


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


# ── compute_effective_elo : cas evidents ──────────────────────────
def test_empty_history_returns_fallback():
    r = compute_effective_elo([], now=NOW, fallback=0)
    assert r.elo == 0
    assert r.source == "empty"


def test_empty_history_with_custom_fallback():
    r = compute_effective_elo([], now=NOW, fallback=1500)
    assert r.elo == 1500


# ── compute_effective_elo : peak sur 6 mois ────────────────────────
def test_peak_uses_max_of_6m_window():
    history = [
        _entry(2000, days_ago=30),   # peak dans la fenetre
        _entry(1800, days_ago=60),
        _entry(1700, days_ago=90),
    ]
    r = compute_effective_elo(history, now=NOW)
    assert r.elo == 2000
    assert r.source == "peak_6m"
    assert r.peak == 2000
    assert r.peak_age_days == 30


def test_peak_just_under_6_months_counts():
    """Edge case : match a 179 jours est dans la fenetre."""
    history = [
        _entry(2000, days_ago=179),
        _entry(1500, days_ago=10),
    ]
    r = compute_effective_elo(history, now=NOW)
    assert r.elo == 2000
    assert r.source == "peak_6m"


def test_peak_exactly_180_days_counts():
    """Edge case : match a exactement 180 jours est dans la limite."""
    history = [_entry(2000, days_ago=180)]
    r = compute_effective_elo(history, now=NOW)
    assert r.source == "peak_6m"


def test_old_peak_ignored_uses_recent_peak():
    """Un peak tres vieux est ignore : on prend le peak de la fenetre 6 mois."""
    history = [
        _entry(2500, days_ago=400),   # ignore (>6 mois)
        _entry(1800, days_ago=30),
        _entry(2000, days_ago=60),    # peak recent
        _entry(1900, days_ago=90),
        _entry(1700, days_ago=400),   # ignore
    ]
    r = compute_effective_elo(history, now=NOW)
    assert r.elo == 2000
    assert r.source == "peak_6m"
    assert r.peak == 2000


def test_no_recent_matches_uses_fallback():
    """Aucun match dans les 6 derniers mois -> fallback (MMR courant)."""
    history = [
        _entry(2500, days_ago=400),
        _entry(2000, days_ago=500),
    ]
    r = compute_effective_elo(history, now=NOW, fallback=1500)
    assert r.elo == 1500
    assert r.source == "no_recent_history"


# ── compute_effective_elo : robustesse ────────────────────────────
def test_history_in_arbitrary_order_works():
    history = [
        _entry(1700, days_ago=90),
        _entry(2000, days_ago=30),
        _entry(1800, days_ago=60),
    ]
    r = compute_effective_elo(history, now=NOW)
    assert r.elo == 2000


def test_single_match_recent():
    r = compute_effective_elo([_entry(1500, days_ago=10)], now=NOW)
    assert r.elo == 1500
    assert r.source == "peak_6m"


def test_single_match_old_uses_fallback():
    r = compute_effective_elo([_entry(1500, days_ago=300)], now=NOW, fallback=2000)
    assert r.elo == 2000
    assert r.source == "no_recent_history"
