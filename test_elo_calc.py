"""Tests unitaires de services.elo_calc — logique pure."""

import pytest
from services import elo_calc


# ── gain_for_rank / loss_for_rank ─────────────────────────────────
@pytest.mark.parametrize("rank,expected", [(0, 20), (1, 18), (2, 17), (3, 16), (4, 15)])
def test_gain_for_rank_returns_expected_values(rank, expected):
    assert elo_calc.gain_for_rank(rank) == expected


@pytest.mark.parametrize("rank,expected", [(0, 10), (1, 10), (2, 12), (3, 13), (4, 15)])
def test_loss_for_rank_returns_expected_values(rank, expected):
    assert elo_calc.loss_for_rank(rank) == expected


def test_gain_for_rank_rejects_out_of_range():
    with pytest.raises(ValueError):
        elo_calc.gain_for_rank(5)
    with pytest.raises(ValueError):
        elo_calc.gain_for_rank(-1)


# ── apply_win / apply_loss ────────────────────────────────────────
def test_apply_win_returns_correct_delta_and_new():
    r = elo_calc.apply_win(current_elo=100, rank=0)
    assert r.old_elo == 100
    assert r.new_elo == 120
    assert r.delta == 20


def test_apply_loss_floors_at_zero():
    r = elo_calc.apply_loss(current_elo=5, rank=0)
    assert r.new_elo == 0
    assert r.delta == -5


def test_apply_loss_normal_case():
    r = elo_calc.apply_loss(current_elo=100, rank=4)
    assert r.new_elo == 85
    assert r.delta == -15


# ── apply_elo_modification ────────────────────────────────────────
def test_apply_modification_add():
    r = elo_calc.apply_elo_modification(50, "add", 30)
    assert r.new_elo == 80


def test_apply_modification_remove_floors_at_zero():
    r = elo_calc.apply_elo_modification(20, "remove", 100)
    assert r.new_elo == 0
    assert r.delta == -20


def test_apply_modification_rejects_negative_amount():
    with pytest.raises(ValueError):
        elo_calc.apply_elo_modification(50, "add", -10)


def test_apply_modification_rejects_huge_amount():
    with pytest.raises(ValueError):
        elo_calc.apply_elo_modification(50, "add", 999_999)


def test_apply_modification_rejects_unknown_action():
    with pytest.raises(ValueError):
        elo_calc.apply_elo_modification(50, "set", 10)


# ── winrate ───────────────────────────────────────────────────────
@pytest.mark.parametrize("wins,losses,expected", [
    (0, 0, 0.0),
    (10, 0, 100.0),
    (0, 10, 0.0),
    (7, 3, 70.0),
    (1, 2, 33.3),
    (1, 3, 25.0),
])
def test_winrate(wins, losses, expected):
    assert elo_calc.winrate(wins, losses) == expected


# ── Constantes ────────────────────────────────────────────────────
def test_win_lose_arrays_have_5_entries():
    assert len(elo_calc.WIN_ELO) == 5
    assert len(elo_calc.LOSE_ELO) == 5


def test_maps_list_not_empty():
    assert len(elo_calc.MAPS) >= 5
    assert "Bind" in elo_calc.MAPS
