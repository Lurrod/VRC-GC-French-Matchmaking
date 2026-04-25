"""Tests de l'algo d'equilibrage des equipes (brute-force optimal)."""

import time

import pytest

from services.team_balancer import (
    Player,
    BalancedTeams,
    balance_teams,
    format_teams,
)


def _players(elos: list[int]) -> list[Player]:
    return [Player(id=i, name=f"P{i}", elo=e) for i, e in enumerate(elos)]


# ── Validation des entrees ────────────────────────────────────────
def test_rejects_less_than_10_players():
    with pytest.raises(ValueError, match="10"):
        balance_teams(_players([1500] * 9))


def test_rejects_more_than_10_players():
    with pytest.raises(ValueError, match="10"):
        balance_teams(_players([1500] * 11))


def test_rejects_duplicate_ids():
    players = [Player(id=1, name="A", elo=1500)] * 10
    with pytest.raises(ValueError, match="[Dd]oublon"):
        balance_teams(players)


# ── Cas trivial : tous egaux ──────────────────────────────────────
def test_all_equal_elo_returns_zero_diff():
    result = balance_teams(_players([1500] * 10))
    assert result.elo_diff == 0
    assert result.peak_diff == 0
    assert len(result.team_a) == 5
    assert len(result.team_b) == 5


# ── Cas evident : 5 forts + 5 faibles ─────────────────────────────
def test_two_clear_groups_get_split():
    """5 a 2000, 5 a 1000 -> chaque equipe doit avoir 2-3 forts."""
    elos = [2000] * 5 + [1000] * 5
    result = balance_teams(_players(elos))
    # Diff parfaite : (2*2000 + 3*1000) vs (3*2000 + 2*1000) = 7000 vs 8000 -> diff 1000
    # Mais l'optimum est (5000 vs 5000) ? Non impossible avec seulement 2 valeurs.
    # En fait : pour avoir diff 0, il faut 2.5 vs 2.5 -> impossible
    # Optimum : 2-3 split -> 7000 vs 8000 -> diff 1000
    assert result.elo_diff == 1000


def test_outlier_player_balanced():
    """Un joueur a 5000 + 9 a 1000 : l'outlier va d'un cote, on minimise."""
    elos = [5000] + [1000] * 9
    result = balance_teams(_players(elos))
    # team avec outlier : 5000 + 4*1000 = 9000
    # autre team : 5*1000 = 5000
    # diff = 4000 (incompressible)
    assert result.elo_diff == 4000


# ── Cas optimum non-trivial ───────────────────────────────────────
def test_finds_optimal_partition():
    """Cas connu : elos varies, l'algo doit trouver l'optimum exact."""
    # 10 joueurs : sum total = 15000 -> 7500 vs 7500 ideal
    elos = [3000, 2500, 2000, 1800, 1500, 1300, 1200, 900, 500, 300]
    # sum = 15000
    result = balance_teams(_players(elos))
    assert result.elo_diff <= 200, f"diff trop grande : {result.elo_diff}"
    assert result.total_a + result.total_b == 15000


def test_brute_force_better_than_snake_draft():
    """Cas typique ou snake draft donne un resultat suboptimal."""
    # snake draft : pos 0,3,4,7,8 vs 1,2,5,6,9
    # elos : [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    # snake : [10, 7, 6, 3, 2] = 28 vs [9, 8, 5, 4, 1] = 27 -> diff 1
    # brute-force fait au moins aussi bien
    elos = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    result = balance_teams(_players(elos))
    assert result.elo_diff <= 1


# ── Determinisme ──────────────────────────────────────────────────
def test_deterministic_same_input_same_output():
    elos    = [1500, 1700, 1200, 1900, 1400, 1600, 1800, 1300, 1500, 1500]
    players = _players(elos)
    r1 = balance_teams(players)
    r2 = balance_teams(players)
    r3 = balance_teams(players)

    assert r1.team_a == r2.team_a == r3.team_a
    assert r1.team_b == r2.team_b == r3.team_b


def test_each_player_appears_exactly_once():
    elos    = [1500, 1700, 1200, 1900, 1400, 1600, 1800, 1300, 1500, 1500]
    players = _players(elos)
    result  = balance_teams(players)
    all_ids = {p.id for p in result.team_a} | {p.id for p in result.team_b}
    assert all_ids == set(range(10))
    assert len(result.team_a) == 5
    assert len(result.team_b) == 5


# ── Tie-breaker peak_diff ─────────────────────────────────────────
def test_tiebreaker_minimizes_peak_difference():
    """
    A elo_diff egal, prefere repartir les meilleurs joueurs.
    Cas : 2 forts (3000) + 8 a 1000.
    Repartitions a elo_diff egal : on doit avoir 1 fort de chaque cote (pas les 2 ensemble).
    """
    elos    = [3000, 3000] + [1000] * 8
    result  = balance_teams(_players(elos))
    # Si les 2 forts sont du meme cote : diff = 4000. Si separes : diff = 0.
    # Donc l'algo va separer -> peak_diff = 0
    assert result.peak_diff == 0
    a_max = max(p.elo for p in result.team_a)
    b_max = max(p.elo for p in result.team_b)
    assert a_max == b_max == 3000


# ── Performance ───────────────────────────────────────────────────
def test_performance_under_50ms():
    import random
    random.seed(0)
    elos = [random.randint(500, 3500) for _ in range(10)]
    players = _players(elos)

    start = time.perf_counter()
    for _ in range(100):
        balance_teams(players)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5, f"100 iterations en {elapsed*1000:.1f}ms (>500ms)"


# ── format_teams (smoke) ──────────────────────────────────────────
def test_format_teams_returns_readable_string():
    result = balance_teams(_players([1500] * 10))
    out = format_teams(result)
    assert "Team A" in out
    assert "Team B" in out
    assert "diff=" in out
