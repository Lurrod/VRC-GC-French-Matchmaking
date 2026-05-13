"""
Tests du module services/match_verifier.py.

Couvre :
  - `find_henrik_custom_match` : recherche d'un match custom recent
    contenant les 10 puuids attendus.
  - `compute_acs_multipliers` : calcul des multiplicateurs ACS par
    joueur, clampes a [0.7, 1.3], avec gestion des cas degeneres
    (teams mixtes, tie, avg_acs=0).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from services.match_verifier import (
    DEFAULT_MULT_MAX,
    DEFAULT_MULT_MIN,
    compute_acs_multipliers,
    find_henrik_custom_match,
)
from services.riot_api import MatchPlayerStats, MatchSummary, RiotApiError


# ── Helpers ──────────────────────────────────────────────────────
def _stats(puuid: str, team: str, score: int = 100, name: str = "P") -> MatchPlayerStats:
    return MatchPlayerStats(
        puuid=puuid, name=name, tag="EUW", team=team,
        score=score, kills=0, deaths=0, assists=0,
    )


def _summary(*, matchid: str = "M1", mode: str = "Custom Game",
             started_at: datetime | None = None,
             rounds: int = 24, rounds_red: int = 13, rounds_blue: int = 11,
             players: tuple[MatchPlayerStats, ...] = (),
             ) -> MatchSummary:
    return MatchSummary(
        matchid=matchid,
        mode=mode,
        map_name="Ascent",
        started_at=started_at or datetime.now(UTC),
        rounds_played=rounds,
        players=players,
        rounds_red=rounds_red,
        rounds_blue=rounds_blue,
    )


# ── find_henrik_custom_match ──────────────────────────────────────
def test_find_custom_returns_match_when_puuids_match():
    started = datetime.now(UTC)
    expected = {"a", "b", "c"}
    target = _summary(
        matchid="M_OK",
        started_at=started,
        players=tuple(_stats(p, "Red" if p in ("a", "b") else "Blue") for p in "abc"),
    )
    client = MagicMock()
    client.get_match_history.return_value = [target]

    result = find_henrik_custom_match(
        client, region="eu", leader_name="L", leader_tag="T",
        expected_puuids=expected, after=started - timedelta(minutes=5),
    )
    assert result is not None
    assert result.matchid == "M_OK"


def test_find_custom_skips_non_custom_mode():
    started = datetime.now(UTC)
    expected = {"a", "b"}
    # Mode "Competitive" mais contient les bons puuids
    wrong_mode = _summary(
        matchid="M_COMP", mode="Competitive", started_at=started,
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    client = MagicMock()
    client.get_match_history.return_value = [wrong_mode]

    result = find_henrik_custom_match(
        client, region="eu", leader_name="L", leader_tag="T",
        expected_puuids=expected, after=started - timedelta(minutes=5),
    )
    assert result is None


def test_find_custom_skips_matches_before_after():
    expected = {"a", "b"}
    too_old = _summary(
        matchid="M_OLD",
        started_at=datetime.now(UTC) - timedelta(hours=2),
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    client = MagicMock()
    client.get_match_history.return_value = [too_old]

    result = find_henrik_custom_match(
        client, region="eu", leader_name="L", leader_tag="T",
        expected_puuids=expected, after=datetime.now(UTC) - timedelta(minutes=30),
    )
    assert result is None


def test_find_custom_skips_when_puuids_incomplete():
    started = datetime.now(UTC)
    expected = {"a", "b", "c"}  # 3 attendus
    # Le match n'a que 2 des 3 puuids
    partial = _summary(
        matchid="M_PARTIAL", started_at=started,
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    client = MagicMock()
    client.get_match_history.return_value = [partial]

    result = find_henrik_custom_match(
        client, region="eu", leader_name="L", leader_tag="T",
        expected_puuids=expected, after=started - timedelta(minutes=5),
    )
    assert result is None


def test_find_custom_returns_none_on_riot_error():
    client = MagicMock()
    client.get_match_history.side_effect = RiotApiError("HenrikDev 503")

    result = find_henrik_custom_match(
        client, region="eu", leader_name="L", leader_tag="T",
        expected_puuids={"a"}, after=datetime.now(UTC),
    )
    assert result is None


def test_find_custom_returns_first_matching_in_history():
    """Le client renvoie l'historique du plus recent au plus ancien.
    On doit prendre le premier qui matche, pas le dernier."""
    started = datetime.now(UTC)
    expected = {"a", "b"}
    newer = _summary(
        matchid="M_NEW", started_at=started,
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    older = _summary(
        matchid="M_OLD", started_at=started - timedelta(minutes=10),
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    client = MagicMock()
    client.get_match_history.return_value = [newer, older]

    result = find_henrik_custom_match(
        client, region="eu", leader_name="L", leader_tag="T",
        expected_puuids=expected, after=started - timedelta(hours=1),
    )
    assert result is not None
    assert result.matchid == "M_NEW"


# ── compute_acs_multipliers ───────────────────────────────────────
def test_acs_happy_path_team_a_wins():
    """Team A (Red) gagne 13-11 ; tous les joueurs ont meme score = mult ~1.0"""
    players = (
        # 5 sur Red (Team A)
        _stats("a1", "Red", score=2400),
        _stats("a2", "Red", score=2400),
        _stats("a3", "Red", score=2400),
        _stats("a4", "Red", score=2400),
        _stats("a5", "Red", score=2400),
        # 5 sur Blue (Team B)
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
        _stats("b3", "Blue", score=2400),
        _stats("b4", "Blue", score=2400),
        _stats("b5", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=13, rounds_blue=11, players=players)
    team_a = {f"a{i}": f"uid_a{i}" for i in range(1, 6)}
    team_b = {f"b{i}": f"uid_b{i}" for i in range(1, 6)}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    assert result.winning_team == "Red"
    assert len(result.performances) == 10
    # Tous mults = 1.0 puisque acs egal a avg_acs
    for p in result.performances:
        assert p.multiplier == pytest.approx(1.0, abs=0.01)
    # Team A (Red) gagne
    team_a_perfs = [p for p in result.performances if p.user_id.startswith("uid_a")]
    assert all(p.win for p in team_a_perfs)
    team_b_perfs = [p for p in result.performances if p.user_id.startswith("uid_b")]
    assert not any(p.win for p in team_b_perfs)


def test_acs_top_frag_gets_higher_multiplier():
    """Un joueur avec ACS double doit avoir un mult plus haut (clampe a 1.3)."""
    players = (
        _stats("a1", "Red", score=4800),  # top frag : 2x la moyenne
        _stats("a2", "Red", score=2400),
        _stats("a3", "Red", score=2400),
        _stats("a4", "Red", score=2400),
        _stats("a5", "Red", score=2400),
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
        _stats("b3", "Blue", score=2400),
        _stats("b4", "Blue", score=2400),
        _stats("b5", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=13, rounds_blue=11, players=players)
    team_a = {f"a{i}": f"uid_a{i}" for i in range(1, 6)}
    team_b = {f"b{i}": f"uid_b{i}" for i in range(1, 6)}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    top = next(p for p in result.performances if p.user_id == "uid_a1")
    assert top.multiplier == DEFAULT_MULT_MAX  # clampe a 1.3


def test_acs_bottom_frag_clamped_to_min():
    """Un joueur avec ACS quasi-nul doit etre clampe a 0.7."""
    players = (
        _stats("a1", "Red", score=0),     # bottom frag
        _stats("a2", "Red", score=3000),
        _stats("a3", "Red", score=3000),
        _stats("a4", "Red", score=3000),
        _stats("a5", "Red", score=3000),
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
        _stats("b3", "Blue", score=2400),
        _stats("b4", "Blue", score=2400),
        _stats("b5", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=13, rounds_blue=11, players=players)
    team_a = {f"a{i}": f"uid_a{i}" for i in range(1, 6)}
    team_b = {f"b{i}": f"uid_b{i}" for i in range(1, 6)}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    bottom = next(p for p in result.performances if p.user_id == "uid_a1")
    assert bottom.multiplier == DEFAULT_MULT_MIN  # clampe a 0.7


def test_acs_mixed_team_labels_skipped():
    """Si les joueurs Team A du bot sont eparpilles entre Red et Blue cote
    Henrik (lobby ou les joueurs ont switche A/D), on skip cette equipe."""
    players = (
        _stats("a1", "Red", score=2400),    # 3 Red
        _stats("a2", "Red", score=2400),
        _stats("a3", "Red", score=2400),
        _stats("a4", "Blue", score=2400),   # mais 2 Blue !
        _stats("a5", "Blue", score=2400),
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
        _stats("b3", "Blue", score=2400),
        _stats("b4", "Red", score=2400),
        _stats("b5", "Red", score=2400),
    )
    match = _summary(rounds=24, rounds_red=13, rounds_blue=11, players=players)
    team_a = {f"a{i}": f"uid_a{i}" for i in range(1, 6)}
    team_b = {f"b{i}": f"uid_b{i}" for i in range(1, 6)}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    # Aucune perf calculee car les deux teams sont mixtes
    assert len(result.performances) == 0


def test_acs_handles_tie_with_empty_winning_team():
    """Si les 2 teams ont le meme nombre de rounds, winning_team = ''."""
    players = (
        _stats("a1", "Red", score=2400),
        _stats("b1", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=12, rounds_blue=12, players=players)
    team_a = {"a1": "uid_a1"}
    team_b = {"b1": "uid_b1"}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    assert result.winning_team == ""
    # Personne ne gagne
    for p in result.performances:
        assert p.win is False


def test_acs_zero_avg_falls_back_to_one():
    """Si toute l'equipe a un score de 0 (avg=0), pas de division par zero."""
    players = (
        _stats("a1", "Red", score=0),
        _stats("a2", "Red", score=0),
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=13, rounds_blue=11, players=players)
    team_a = {"a1": "uid_a1", "a2": "uid_a2"}
    team_b = {"b1": "uid_b1", "b2": "uid_b2"}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    # Team A : avg_acs=0 → fallback 1.0, acs=0/1.0=0 → clamp 0.7
    team_a_perfs = [p for p in result.performances if p.user_id.startswith("uid_a")]
    assert len(team_a_perfs) == 2
    for p in team_a_perfs:
        assert p.multiplier == DEFAULT_MULT_MIN  # clampe a 0.7


def test_acs_team_b_wins_correctly_labeled():
    """Quand Blue gagne, les joueurs Blue sont marques win=True."""
    players = (
        _stats("a1", "Red", score=2400),
        _stats("a2", "Red", score=2400),
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=11, rounds_blue=13, players=players)
    team_a = {"a1": "uid_a1", "a2": "uid_a2"}
    team_b = {"b1": "uid_b1", "b2": "uid_b2"}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    assert result.winning_team == "Blue"
    for p in result.performances:
        if p.user_id.startswith("uid_b"):
            assert p.win is True
        else:
            assert p.win is False
