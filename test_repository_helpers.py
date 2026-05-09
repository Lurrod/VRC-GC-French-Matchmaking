"""Tests for compound _id helpers in services/repository.py."""

import pytest

from services.repository import (
    QUEUE_TYPES,
    player_doc_id,
    active_queue_id,
    leaderboard_state_id,
    is_valid_queue_type,
)


def test_queue_types_constant():
    assert QUEUE_TYPES == ("pro", "open", "gc")


def test_is_valid_queue_type():
    assert is_valid_queue_type("pro")
    assert is_valid_queue_type("open")
    assert is_valid_queue_type("gc")
    assert not is_valid_queue_type("PRO")
    assert not is_valid_queue_type("")
    assert not is_valid_queue_type("ranked")


def test_player_doc_id():
    assert player_doc_id(123, "pro") == "123:pro"
    assert player_doc_id("456", "open") == "456:open"
    assert player_doc_id(789, "gc") == "789:gc"


def test_active_queue_id():
    assert active_queue_id("pro") == "active:pro"
    assert active_queue_id("open") == "active:open"
    assert active_queue_id("gc") == "active:gc"


def test_leaderboard_state_id():
    assert leaderboard_state_id("pro") == "current:pro"
    assert leaderboard_state_id("open") == "current:open"
    assert leaderboard_state_id("gc") == "current:gc"


def test_leaderboard_state_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="queue_type"):
        leaderboard_state_id("ranked")


def test_player_doc_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="queue_type"):
        player_doc_id(123, "ranked")


def test_active_queue_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="queue_type"):
        active_queue_id("ranked")


import mongomock
from services.repository import get_or_create_player


def test_get_or_create_player_uses_compound_id():
    db = mongomock.MongoClient(tz_aware=True).db
    col = db["elo_42"]

    doc = get_or_create_player(col, user_id=1, queue_type="pro",
                                display_name="Alice", initial_elo=2000)
    assert doc["_id"] == "1:pro"
    assert doc["elo"] == 2000
    assert doc["wins"] == 0
    assert doc["queue_type"] == "pro"
    assert doc["name"] == "Alice"


def test_get_or_create_player_isolates_queue_types():
    db = mongomock.MongoClient(tz_aware=True).db
    col = db["elo_42"]
    get_or_create_player(col, user_id=1, queue_type="pro",
                          display_name="Alice", initial_elo=2000)
    get_or_create_player(col, user_id=1, queue_type="open",
                          display_name="Alice", initial_elo=2000)
    docs = list(col.find())
    assert len(docs) == 2
    assert {d["_id"] for d in docs} == {"1:pro", "1:open"}
