"""Tests de la logique pure de formation de match."""

import random
from unittest.mock import MagicMock

import pytest

from services.match_service import (
    build_players,
    plan_match,
    serialize_team,
    find_free_match_category,
)
from services.team_balancer import Player


def _riot_doc(elo: int, name: str = "X") -> dict:
    return {
        "riot_name": name, "riot_tag": "EUW", "riot_region": "eu",
        "puuid": "p", "effective_elo": elo, "peak_elo": elo, "source": "peak_recent",
    }


# ── build_players ─────────────────────────────────────────────────
def test_build_players_uses_member_display_name():
    players = build_players(
        player_ids=["1", "2"],
        riot_accounts={"1": _riot_doc(1500), "2": _riot_doc(2000)},
        member_names={"1": "Jet", "2": "Sage"},
    )
    assert len(players) == 2
    assert players[0].id == 1 and players[0].name == "Jet" and players[0].elo == 1500
    assert players[1].id == 2 and players[1].name == "Sage" and players[1].elo == 2000


def test_build_players_skips_unlinked():
    players = build_players(
        player_ids=["1", "2", "3"],
        riot_accounts={"1": _riot_doc(1500), "3": _riot_doc(1700)},
        member_names={"1": "A", "2": "B", "3": "C"},
    )
    # Joueur 2 sans compte Riot -> ignore
    assert len(players) == 2
    assert {p.id for p in players} == {1, 3}


def test_build_players_falls_back_to_riot_name():
    players = build_players(
        player_ids=["1"],
        riot_accounts={"1": _riot_doc(1500, name="RiotName")},
        member_names={},  # aucun member resolu
    )
    assert players[0].name == "RiotName"


# ── plan_match ────────────────────────────────────────────────────
def test_plan_match_rejects_wrong_size():
    players = [Player(id=i, name=f"P{i}", elo=1500) for i in range(9)]
    with pytest.raises(ValueError, match="10"):
        plan_match(players, free_category="Match #1")


def test_plan_match_returns_balanced_teams_and_random_choices():
    players = [Player(id=i, name=f"P{i}", elo=1500 + i*50) for i in range(10)]
    rng = random.Random(42)  # deterministe pour le test

    plan = plan_match(players, free_category="Match #1", rng=rng)

    assert len(plan.teams.team_a) == 5
    assert len(plan.teams.team_b) == 5
    # avec rng seede : on peut verifier la stabilite
    assert plan.map_name in ("Breeze", "Bind", "Lotus", "Fracture", "Split", "Haven", "Pearl")
    assert plan.lobby_leader in players
    assert plan.category_name == "Match #1"


def test_plan_match_with_no_free_category():
    players = [Player(id=i, name=f"P{i}", elo=1500) for i in range(10)]
    plan = plan_match(players, free_category=None)
    assert plan.category_name is None


def test_plan_match_lobby_leader_is_one_of_the_players():
    players = [Player(id=i, name=f"P{i}", elo=1000 + i) for i in range(10)]
    for seed in range(20):
        plan = plan_match(players, free_category="Match #1", rng=random.Random(seed))
        leader_ids = {p.id for p in players}
        assert plan.lobby_leader.id in leader_ids


def test_plan_match_balance_optimal():
    """Cas connu : avec 10 elos varies, l'algo brute force trouve le mieux."""
    players = [Player(id=i, name=f"P{i}", elo=elo)
               for i, elo in enumerate([3000, 2500, 2000, 1800, 1500, 1300, 1200, 900, 500, 300])]
    plan = plan_match(players, free_category=None)
    assert plan.teams.elo_diff <= 200


# ── serialize_team ────────────────────────────────────────────────
def test_serialize_team_returns_list_of_dicts():
    team = (Player(id=1, name="A", elo=1500), Player(id=2, name="B", elo=1600))
    out = serialize_team(team)
    assert out == [
        {"id": 1, "name": "A", "elo": 1500},
        {"id": 2, "name": "B", "elo": 1600},
    ]


# ── find_free_match_category ──────────────────────────────────────
def _fake_category(name: str, t1_members: int, t2_members: int):
    cat = MagicMock()
    cat.name = name
    t1 = MagicMock(name=f"{name}-t1")
    t1.name = "Team 1"
    t1.members = list(range(t1_members))
    t2 = MagicMock(name=f"{name}-t2")
    t2.name = "Team 2"
    t2.members = list(range(t2_members))
    cat.voice_channels = [t1, t2]
    return cat


def _fake_guild_with_categories(*categories):
    g = MagicMock()
    g.categories = list(categories)
    return g


def test_find_free_category_first_empty():
    cat1 = _fake_category("Match #1", t1_members=0, t2_members=0)
    cat2 = _fake_category("Match #2", t1_members=2, t2_members=0)
    guild = _fake_guild_with_categories(cat1, cat2)
    assert find_free_match_category(guild) == "Match #1"


def test_find_free_category_skips_occupied():
    cat1 = _fake_category("Match #1", t1_members=2, t2_members=2)
    cat2 = _fake_category("Match #2", t1_members=0, t2_members=0)
    guild = _fake_guild_with_categories(cat1, cat2)
    assert find_free_match_category(guild) == "Match #2"


def test_find_free_category_none_when_all_occupied():
    cat1 = _fake_category("Match #1", t1_members=5, t2_members=5)
    cat2 = _fake_category("Match #2", t1_members=5, t2_members=5)
    cat3 = _fake_category("Match #3", t1_members=5, t2_members=5)
    guild = _fake_guild_with_categories(cat1, cat2, cat3)
    assert find_free_match_category(guild) is None


def test_find_free_category_none_when_no_match_categories():
    cat = _fake_category("General", t1_members=0, t2_members=0)
    guild = _fake_guild_with_categories(cat)
    assert find_free_match_category(guild) is None
