"""Test that the migration script copies docs and archives source collections."""
from __future__ import annotations

from unittest.mock import patch

import mongomock
import pytest


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://test/")
    monkeypatch.setenv("MIGRATE_SOURCE_GUILD_ID", "111")


def test_migration_copies_and_archives(env):
    fake_client = mongomock.MongoClient()
    fake_db = fake_client["elobot"]

    # Seed old guild-suffixed data (note historical name riot_accounts_<guild>)
    fake_db["elo_111"].insert_one({"_id": "1:pro", "elo": 2500})
    fake_db["riot_accounts_111"].insert_one({"_id": "1", "puuid": "abc"})
    fake_db["matches_111"].insert_one({"_id": "m1", "winner": "a"})

    with patch("scripts.migrate_shared_collections.MongoClient", return_value=fake_client):
        from scripts import migrate_shared_collections
        rc = migrate_shared_collections.main()

    assert rc == 0
    assert fake_db["elo"].find_one({"_id": "1:pro"})["elo"] == 2500
    assert fake_db["riot"].find_one({"_id": "1"})["puuid"] == "abc"
    assert fake_db["matches"].find_one({"_id": "m1"})["winner"] == "a"

    # Old collection names should be gone (renamed to archive)
    names = fake_db.list_collection_names()
    assert "elo_111" not in names
    assert "riot_accounts_111" not in names
    assert "matches_111" not in names
    # Archives should exist with archive_<stamp>_<src> prefix/suffix
    assert any(n.endswith("_elo_111") and n.startswith("archive_") for n in names)
    assert any(n.endswith("_riot_accounts_111") and n.startswith("archive_") for n in names)
    assert any(n.endswith("_matches_111") and n.startswith("archive_") for n in names)


def test_migration_idempotent_when_no_source(env):
    """Re-running migration when source collections are absent should no-op cleanly."""
    fake_client = mongomock.MongoClient()
    # No data seeded - all 3 source collections absent
    with patch("scripts.migrate_shared_collections.MongoClient", return_value=fake_client):
        from scripts import migrate_shared_collections
        rc = migrate_shared_collections.main()

    assert rc == 0
