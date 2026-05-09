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


from services.repository import (
    setup_active_queue,
    get_active_queue,
    delete_active_queue,
    add_player_to_queue,
    remove_player_from_queue,
    close_active_queue,
    find_player_in_any_queue,
)


def test_setup_and_get_active_queue_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="pro",
                        channel_id=100, message_id=999)
    setup_active_queue(db, guild_id=42, queue_type="open",
                        channel_id=200, message_id=888)

    pro = get_active_queue(db, guild_id=42, queue_type="pro")
    open_q = get_active_queue(db, guild_id=42, queue_type="open")
    gc = get_active_queue(db, guild_id=42, queue_type="gc")

    assert pro["_id"] == "active:pro"
    assert pro["channel_id"] == 100
    assert pro["queue_type"] == "pro"
    assert open_q["_id"] == "active:open"
    assert open_q["channel_id"] == 200
    assert gc is None


def test_add_remove_player_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="pro",
                        channel_id=100, message_id=999)

    res = add_player_to_queue(db, guild_id=42, queue_type="pro", user_id=1)
    assert res.success
    assert res.queue["players"] == ["1"]
    assert res.queue["queue_type"] == "pro"

    res = remove_player_from_queue(db, guild_id=42, queue_type="pro", user_id=1)
    assert res.success
    assert res.queue["players"] == []


def test_find_player_in_any_queue():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="pro",
                        channel_id=100, message_id=999)
    setup_active_queue(db, guild_id=42, queue_type="open",
                        channel_id=200, message_id=888)
    add_player_to_queue(db, guild_id=42, queue_type="pro", user_id=1)

    assert find_player_in_any_queue(db, guild_id=42, user_id=1) == "pro"
    assert find_player_in_any_queue(db, guild_id=42, user_id=2) is None


def test_delete_active_queue_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="pro",
                        channel_id=100, message_id=999)
    setup_active_queue(db, guild_id=42, queue_type="open",
                        channel_id=200, message_id=888)

    assert delete_active_queue(db, guild_id=42, queue_type="pro") is True
    assert get_active_queue(db, guild_id=42, queue_type="pro") is None
    assert get_active_queue(db, guild_id=42, queue_type="open") is not None


def test_close_active_queue_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="pro",
                        channel_id=100, message_id=999)
    close_active_queue(db, guild_id=42, queue_type="pro")
    pro = get_active_queue(db, guild_id=42, queue_type="pro")
    assert pro["status"] == "forming"


from services.repository import (
    get_leaderboard_message_id,
    set_leaderboard_message_id,
    clear_leaderboard_message_id,
)


def test_leaderboard_message_id_per_queue_type():
    db = mongomock.MongoClient(tz_aware=True).db
    set_leaderboard_message_id(db, guild_id=42, queue_type="pro", message_id=111)
    set_leaderboard_message_id(db, guild_id=42, queue_type="open", message_id=222)

    assert get_leaderboard_message_id(db, guild_id=42, queue_type="pro") == 111
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="open") == 222
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="gc") is None

    clear_leaderboard_message_id(db, guild_id=42, queue_type="pro")
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="pro") is None
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="open") == 222


from services.repository import create_match, get_match


def test_create_match_persists_queue_type():
    db = mongomock.MongoClient(tz_aware=True).db
    match_id = create_match(
        db, guild_id=42, queue_type="pro",
        team_a=[{"id": "1", "name": "A", "elo": 2000}],
        team_b=[{"id": "2", "name": "B", "elo": 2000}],
        map_name="Ascent",
        lobby_leader_id=1,
        category_name="Match #1",
        message_id=999,
        channel_id=100,
    )
    doc = get_match(db, guild_id=42, match_id=match_id)
    assert doc["queue_type"] == "pro"
