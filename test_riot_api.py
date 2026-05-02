"""
Tests du client HenrikDev avec mocks de requests.Session.
On NE fait JAMAIS d'appel reseau reel ici.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from services.riot_api import (
    HenrikDevClient,
    PlayerNotFound,
    RateLimited,
    RiotApiError,
    VALID_REGIONS,
)


def _mock_response(status: int, json_data: dict | None = None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


def _make_session(response):
    session = MagicMock()
    session.get.return_value = response
    return session


# ── get_account ───────────────────────────────────────────────────
def test_get_account_returns_parsed_data():
    session = _make_session(_mock_response(200, {
        "status": 200,
        "data": {"puuid": "abc-123", "name": "Player", "tag": "EUW", "region": "eu"},
    }))
    client = HenrikDevClient(session=session)

    account = client.get_account("Player", "EUW")
    assert account.puuid == "abc-123"
    assert account.name == "Player"
    assert account.tag == "EUW"
    assert account.region == "eu"


def test_get_account_404_raises_player_not_found():
    session = _make_session(_mock_response(404, text="Not Found"))
    client = HenrikDevClient(session=session)
    with pytest.raises(PlayerNotFound):
        client.get_account("Ghost", "404")


def test_get_account_429_raises_rate_limited():
    session = _make_session(_mock_response(429))
    client = HenrikDevClient(session=session)
    with pytest.raises(RateLimited):
        client.get_account("X", "1")


def test_network_error_wrapped_as_riot_api_error():
    import requests as _requests
    session = MagicMock()
    session.get.side_effect = _requests.ConnectionError("DNS fail")
    client = HenrikDevClient(session=session)
    with pytest.raises(RiotApiError, match="reseau"):
        client.get_account("X", "1")


# ── get_current_mmr ───────────────────────────────────────────────
def test_get_current_mmr_parses_fields():
    session = _make_session(_mock_response(200, {
        "status": 200,
        "data": {
            "current_data": {
                "elo": 2080,
                "currenttier": 24,
                "currenttierpatched": "Immortal 1",
                "ranking_in_tier": 80,
                "mmr_change_to_last_game": -23,
            },
        },
    }))
    client = HenrikDevClient(session=session)

    mmr = client.get_current_mmr("eu", "Player", "EUW")
    assert mmr.elo == 2080
    assert mmr.tier == 24
    assert mmr.tier_name == "Immortal 1"
    assert mmr.ranking_in_tier == 80
    assert mmr.mmr_change_last == -23


def test_get_current_mmr_handles_missing_fields():
    session = _make_session(_mock_response(200, {"status": 200, "data": {"current_data": {}}}))
    client = HenrikDevClient(session=session)
    mmr = client.get_current_mmr("eu", "X", "1")
    assert mmr.elo == 0
    assert mmr.tier_name == "Unrated"


def test_get_current_mmr_rejects_invalid_region():
    session = _make_session(_mock_response(200))
    client = HenrikDevClient(session=session)
    with pytest.raises(ValueError, match="Region"):
        client.get_current_mmr("middle-earth", "X", "1")


def test_valid_regions_includes_eu():
    assert "eu" in VALID_REGIONS
    assert "na" in VALID_REGIONS


# ── get_mmr_history ───────────────────────────────────────────────
def test_get_mmr_history_parses_entries():
    session = _make_session(_mock_response(200, {
        "status": 200,
        "data": [
            {"elo": 2080, "currenttier": 24, "date_raw": 1750_000_000, "mmr_change_to_last_game": -10},
            {"elo": 2090, "currenttier": 24, "date_raw": 1749_900_000, "mmr_change_to_last_game": 15},
        ],
    }))
    client = HenrikDevClient(session=session)

    history = client.get_mmr_history("eu", "Player", "EUW")
    assert len(history) == 2
    assert history[0].elo == 2080
    assert history[0].tier == 24
    assert history[0].date == datetime.fromtimestamp(1750_000_000, tz=timezone.utc)
    assert history[1].mmr_change == 15


def test_get_mmr_history_skips_entries_without_date():
    session = _make_session(_mock_response(200, {
        "status": 200,
        "data": [
            {"elo": 1500, "date_raw": None},
            {"elo": 1600, "date_raw": 1750_000_000},
        ],
    }))
    client = HenrikDevClient(session=session)
    history = client.get_mmr_history("eu", "X", "1")
    assert len(history) == 1
    assert history[0].elo == 1600


def test_get_mmr_history_empty_data():
    session = _make_session(_mock_response(200, {"status": 200, "data": []}))
    client = HenrikDevClient(session=session)
    assert client.get_mmr_history("eu", "X", "1") == []


# ── Cache ─────────────────────────────────────────────────────────
def test_cache_avoids_double_api_calls():
    session = _make_session(_mock_response(200, {
        "status": 200, "data": {"puuid": "abc"},
    }))
    client = HenrikDevClient(session=session)

    client.get_account("Player", "EUW")
    client.get_account("Player", "EUW")
    client.get_account("Player", "EUW")

    # 1 seul appel HTTP, les 2 suivants viennent du cache
    assert session.get.call_count == 1


def test_cache_distinct_keys():
    """Pseudo different = cle different = pas de hit."""
    session = MagicMock()
    session.get.side_effect = [
        _mock_response(200, {"status": 200, "data": {"puuid": "alice"}}),
        _mock_response(200, {"status": 200, "data": {"puuid": "bob"}}),
    ]
    client = HenrikDevClient(session=session)

    a = client.get_account("Alice", "FR")
    b = client.get_account("Bob",   "FR")
    assert a.puuid == "alice"
    assert b.puuid == "bob"
    assert session.get.call_count == 2


def test_clear_cache_forces_refresh():
    session = _make_session(_mock_response(200, {"status": 200, "data": {"puuid": "abc"}}))
    client = HenrikDevClient(session=session)

    client.get_account("Player", "EUW")
    client.clear_cache()
    client.get_account("Player", "EUW")

    assert session.get.call_count == 2


def test_get_match_history_bypasses_cache():
    """Le polling de match_history doit toujours faire un appel reseau frais.
    Sans bypass, le 1er retry renverrait stale 'pas encore indexe' pendant 1h."""
    session = MagicMock()
    session.get.side_effect = [
        _mock_response(200, {"status": 200, "data": []}),
        _mock_response(200, {"status": 200, "data": []}),
        _mock_response(200, {"status": 200, "data": [{
            "metadata": {"matchid": "x", "mode": "Custom Game", "map": "Ascent",
                         "game_start": 0, "rounds_played": 0},
            "teams": {}, "players": {"all_players": []},
        }]}),
    ]
    client = HenrikDevClient(session=session)
    client.get_match_history("eu", "Player", "EUW", mode="custom")
    client.get_match_history("eu", "Player", "EUW", mode="custom")
    res = client.get_match_history("eu", "Player", "EUW", mode="custom")

    assert session.get.call_count == 3
    assert len(res) == 1


def test_get_match_history_does_not_pollute_cache():
    """Apres un appel match_history, aucune entree n'est ajoutee au cache."""
    session = _make_session(_mock_response(200, {"status": 200, "data": []}))
    client = HenrikDevClient(session=session)
    client.get_match_history("eu", "Player", "EUW", mode="custom")
    assert all("/matches/" not in k for k in client._cache._store.keys())


# ── Headers ───────────────────────────────────────────────────────
def test_api_key_added_to_headers_when_provided():
    session = _make_session(_mock_response(200, {"status": 200, "data": {}}))
    client = HenrikDevClient(api_key="HDEV-test-key", session=session)
    client.get_account("X", "1")

    _, kwargs = session.get.call_args
    assert kwargs["headers"]["Authorization"] == "HDEV-test-key"


def test_no_authorization_header_without_key(monkeypatch):
    monkeypatch.delenv("HENRIK_API_KEY", raising=False)
    session = _make_session(_mock_response(200, {"status": 200, "data": {}}))
    client = HenrikDevClient(api_key=None, session=session)
    client.get_account("X", "1")

    _, kwargs = session.get.call_args
    assert "Authorization" not in kwargs["headers"]
