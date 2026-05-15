"""Tests purs pour le draft capitaine de la Pro Queue."""
from __future__ import annotations

import random

import pytest

from services.captain_draft import pick_captains
from services.team_balancer import Player


def _p(uid: int, elo: int) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=elo)


def test_pick_captains_top_two_elo():
    """Les 2 ELO les plus hauts sont designes capitaines."""
    players = [
        _p(1, 1000), _p(2, 1100), _p(3, 1200), _p(4, 1300), _p(5, 1400),
        _p(6, 1500), _p(7, 1600), _p(8, 1700), _p(9, 1800), _p(10, 1900),
    ]
    rng = random.Random(42)
    cap_a, cap_b = pick_captains(players, rng=rng)
    assert cap_a.id == 10  # ELO 1900
    assert cap_b.id == 9   # ELO 1800


def test_pick_captains_tiebreak_random_seeded():
    """Avec 4 joueurs a ELO max identique, la seed RNG determine les capitaines."""
    players = [_p(i, 1500) for i in range(1, 5)]  # 4 joueurs tous a 1500
    cap_a_seed1, cap_b_seed1 = pick_captains(players, rng=random.Random(1))
    cap_a_seed2, cap_b_seed2 = pick_captains(players, rng=random.Random(2))
    # Reproductible : meme seed -> meme resultat
    cap_a_again, cap_b_again = pick_captains(players, rng=random.Random(1))
    assert (cap_a_seed1.id, cap_b_seed1.id) == (cap_a_again.id, cap_b_again.id)
    # Deux seeds donnent generalement des resultats differents (sur 4!=24 perms)
    assert (cap_a_seed1.id, cap_b_seed1.id) != (cap_a_seed2.id, cap_b_seed2.id)


def test_pick_captains_tiebreak_position_2():
    """1 joueur clairement top, 3 a egalite pour position 2 -> RNG entre les 3."""
    players = [_p(1, 2000)] + [_p(i, 1500) for i in range(2, 11)]
    cap_a, cap_b = pick_captains(players, rng=random.Random(7))
    assert cap_a.id == 1            # le top ELO unique
    assert cap_b.id in {2, 3, 4, 5, 6, 7, 8, 9, 10}  # un des tied


def test_pick_captains_raises_if_too_few_players():
    with pytest.raises(ValueError, match="au moins 2 joueurs"):
        pick_captains([_p(1, 1500)], rng=random.Random(0))
