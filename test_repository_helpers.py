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


def test_active_queue_id():
    assert active_queue_id("pro") == "active:pro"
    assert active_queue_id("open") == "active:open"
    assert active_queue_id("gc") == "active:gc"


def test_leaderboard_state_id():
    assert leaderboard_state_id("pro") == "current:pro"


def test_player_doc_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="queue_type"):
        player_doc_id(123, "ranked")


def test_active_queue_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="queue_type"):
        active_queue_id("ranked")
