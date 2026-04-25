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


# ── compute_effective_elo : peak recent ───────────────────────────
def test_peak_recent_used_when_within_6_months():
    history = [
        _entry(2000, days_ago=30),   # peak, recent
        _entry(1800, days_ago=60),
        _entry(1700, days_ago=90),
    ]
    r = compute_effective_elo(history, now=NOW)
    assert r.elo == 2000
    assert r.source == "peak_recent"
    assert r.peak == 2000
    assert r.peak_age_days == 30


def test_peak_just_under_6_months_still_recent():
    """Edge case : peak il y a 179 jours est encore 'recent'."""
    history = [
        _entry(2000, days_ago=179),
        _entry(1500, days_ago=10),
    ]
    r = compute_effective_elo(history, now=NOW)
    assert r.elo == 2000
    assert r.source == "peak_recent"


def test_peak_exactly_180_days_still_counts_as_recent():
    """Edge case : peak il y a exactement 180 jours est dans la limite."""
    history = [_entry(2000, days_ago=180)]
    r = compute_effective_elo(history, now=NOW)
    assert r.source == "peak_recent"


# ── compute_effective_elo : peak vieux, moyenne 6 mois ────────────
def test_old_peak_uses_average_of_last_6_months():
    history = [
        _entry(2500, days_ago=400),   # peak tres vieux
        _entry(1800, days_ago=30),    # recent
        _entry(2000, days_ago=60),    # recent
        _entry(1900, days_ago=90),    # recent
        _entry(1700, days_ago=400),   # vieux, ignore
    ]
    r = compute_effective_elo(history, now=NOW)
    # Moyenne des 3 recents : (1800 + 2000 + 1900) / 3 = 1900
    assert r.elo == 1900
    assert r.source == "average_6m"
    assert r.peak == 2500


def test_old_peak_no_recent_matches_falls_back_to_peak():
    """Cas limite : peak vieux + 0 match recent -> on prend le peak."""
    history = [
        _entry(2500, days_ago=400),
        _entry(2000, days_ago=500),
    ]
    r = compute_effective_elo(history, now=NOW)
    assert r.elo == 2500
    assert r.source == "peak_fallback"


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
    assert r.source == "peak_recent"


def test_single_match_old():
    r = compute_effective_elo([_entry(1500, days_ago=300)], now=NOW)
    assert r.elo == 1500
    assert r.source == "peak_fallback"


def test_average_rounded_to_integer():
    """Moyenne de 1500 + 1501 = 1500.5 -> arrondi a 1501 (banker's rounding) ou 1500."""
    history = [
        _entry(3000, days_ago=400),    # peak vieux
        _entry(1500, days_ago=10),
        _entry(1501, days_ago=20),
    ]
    r = compute_effective_elo(history, now=NOW)
    # 1500.5 -> 1500 (banker's) ou 1501 selon impl ; on accepte les deux
    assert r.elo in (1500, 1501)
    assert r.source == "average_6m"
