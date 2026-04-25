"""
Tests unitaires de la logique de pagination (sans Discord, sans MongoDB).

Ces tests valident UNIQUEMENT le calcul du nombre de pages et les bornes
de navigation. Ils ne peuvent pas detecter un bug dans l'API Discord.

Usage:
    pip install pytest
    pytest test_pagination.py -v
"""

import pytest


PAGE_SIZE = 15


def total_pages(n_players: int) -> int:
    """Reproduit le calcul du bot (ligne 215 du fichier original)."""
    return max(1, (n_players + PAGE_SIZE - 1) // PAGE_SIZE)


def is_prev_disabled(page: int) -> bool:
    return page == 0


def is_next_disabled(page: int, total: int) -> bool:
    return page >= total - 1


def clamp_page(new_page: int, total: int) -> int | None:
    """Renvoie None si le clic doit etre ignore (hors bornes)."""
    if new_page < 0 or new_page >= total:
        return None
    return new_page


# ── Tests : nombre de pages ───────────────────────────────────────
@pytest.mark.parametrize("n,expected", [
    (0,  1),    # liste vide -> 1 page (default)
    (1,  1),    # 1 joueur -> 1 page
    (15, 1),    # exactement PAGE_SIZE -> 1 page
    (16, 2),    # 1 de plus -> 2 pages
    (29, 2),
    (30, 2),
    (31, 3),
    (100, 7),
    (150, 10),
])
def test_total_pages(n, expected):
    assert total_pages(n) == expected


# ── Tests : etat des boutons ──────────────────────────────────────
def test_prev_disabled_on_first_page():
    assert is_prev_disabled(0) is True


def test_prev_enabled_on_other_pages():
    assert is_prev_disabled(1) is False
    assert is_prev_disabled(5) is False


def test_next_disabled_on_last_page():
    assert is_next_disabled(1, total=2) is True
    assert is_next_disabled(6, total=7) is True


def test_next_enabled_when_more_pages():
    assert is_next_disabled(0, total=2) is False
    assert is_next_disabled(3, total=7) is False


def test_both_disabled_when_only_one_page():
    assert is_prev_disabled(0) is True
    assert is_next_disabled(0, total=1) is True


# ── Tests : navigation (clamp) ────────────────────────────────────
def test_clamp_valid_page():
    assert clamp_page(2, total=5) == 2


def test_clamp_first_page():
    assert clamp_page(0, total=5) == 0


def test_clamp_last_page():
    assert clamp_page(4, total=5) == 4


def test_clamp_below_zero_rejected():
    assert clamp_page(-1, total=5) is None


def test_clamp_above_total_rejected():
    assert clamp_page(5, total=5) is None
    assert clamp_page(99, total=5) is None


# ── Tests : decoupage en chunks ───────────────────────────────────
def test_chunk_first_page():
    players = list(range(30))
    page = 0
    chunk = players[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    assert chunk == list(range(15))


def test_chunk_second_page():
    players = list(range(30))
    page = 1
    chunk = players[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    assert chunk == list(range(15, 30))


def test_chunk_partial_last_page():
    """Cas ou la derniere page n'est pas pleine - ATTENTION, peut casser
    generate_leaderboard si elle n'est pas tolerante aux chunks de taille variable."""
    players = list(range(16))
    chunk_p2 = players[15:30]
    assert len(chunk_p2) == 1, "La page 2 ne contient qu'1 joueur ici"


def test_chunk_empty_when_out_of_bounds():
    players = list(range(15))
    chunk = players[15:30]
    assert chunk == []
