"""Verify that elo/riot/matches collections are shared across guilds (no guild_id suffix)."""
from __future__ import annotations

import mongomock
import pytest

from services import repository


@pytest.fixture
def db():
    return mongomock.MongoClient().db


def test_elo_collection_has_no_guild_suffix(db):
    """get_elo_col should return the same single collection regardless of caller."""
    col = repository.get_elo_col(db)
    assert col.name == "elo"


def test_elo_round_trip_via_shared_collection(db):
    """Round-trip insert/read on the shared elo collection.

    The cross-guild guarantee is encoded in the signature itself
    (`get_elo_col(db)` takes no guild_id), so any two callers necessarily
    target the same collection. This test verifies the round-trip works.
    """
    repository.get_elo_col(db).insert_one(
        {"_id": "1:pro", "user_id": "1", "queue_type": "pro", "elo": 2500}
    )
    doc = repository.get_elo_col(db).find_one({"_id": "1:pro"})
    assert doc is not None
    assert doc["elo"] == 2500


def test_riot_collection_has_no_guild_suffix(db):
    col = repository.get_riot_col(db)
    assert col.name == "riot"


def test_matches_collection_has_no_guild_suffix(db):
    col = repository.get_matches_col(db)
    assert col.name == "matches"


def test_queue_collection_remains_per_guild(db):
    """Queue collections must stay per-guild (this is the explicit non-sharing case)."""
    col_a = repository.get_queue_col(db, guild_id=100)
    col_b = repository.get_queue_col(db, guild_id=200)
    assert col_a.name == "queue_100"
    assert col_b.name == "queue_200"


def test_create_match_persists_origin_guild_id(db):
    """create_match writes int(origin_guild_id) to the doc and get_match reads it back."""
    match_id = repository.create_match(
        db,
        queue_type="pro",
        origin_guild_id=12345,
        team_a=[{"user_id": "1", "elo": 2000}],
        team_b=[{"user_id": "2", "elo": 2000}],
        map_name="Haven",
        lobby_leader_id=1,
        category_name=None,
        message_id=None,
        channel_id=None,
    )
    doc = repository.get_match(db, match_id)
    assert doc is not None
    assert doc["origin_guild_id"] == 12345


def test_find_pending_match_role_cleanups_filters_by_origin_guild_id(db):
    """Scan helpers must scope by origin_guild_id when provided.

    In a multi-guild deployment, each guild's background loop must only
    process its own matches, otherwise admin notifications / role
    revocations can land on the wrong guild. The CAS prevents data
    corruption but UX consequences still need this scoping.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    past = now - timedelta(hours=1)
    # Two matches, two origin guilds, both have a pending role-cleanup deadline
    db["matches"].insert_one({
        "_id": "m_a",
        "origin_guild_id": 100,
        "match_role_cleanup_at": past,
        "match_role_cleanup_done": False,
    })
    db["matches"].insert_one({
        "_id": "m_b",
        "origin_guild_id": 200,
        "match_role_cleanup_at": past,
        "match_role_cleanup_done": False,
    })
    res_a = repository.find_pending_match_role_cleanups(db, now, origin_guild_id=100)
    assert [d["_id"] for d in res_a] == ["m_a"]
    res_b = repository.find_pending_match_role_cleanups(db, now, origin_guild_id=200)
    assert [d["_id"] for d in res_b] == ["m_b"]
    # Unscoped: returns both (preserves single-guild / tests backward compat)
    res_all = repository.find_pending_match_role_cleanups(db, now)
    assert sorted(d["_id"] for d in res_all) == ["m_a", "m_b"]
