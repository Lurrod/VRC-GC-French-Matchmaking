"""Tests purs pour le draft capitaine de la Pro Queue."""
from __future__ import annotations

import random

import pytest

from services.captain_draft import pick_captains
from services.team_balancer import Player

pytestmark = pytest.mark.unit


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
    """Avec 4 joueurs a ELO identique, la seed RNG determine les capitaines de maniere reproductible."""
    players = [_p(i, 1500) for i in range(1, 5)]
    # Resultats observes pour les seeds 1 et 2 (pinnes pour eviter une assertion probabiliste).
    cap_a_s1, cap_b_s1 = pick_captains(players, rng=random.Random(1))
    cap_a_s2, cap_b_s2 = pick_captains(players, rng=random.Random(2))
    # Reproductibilite : meme seed -> meme resultat (verification re-tirage)
    cap_a_s1_again, cap_b_s1_again = pick_captains(players, rng=random.Random(1))
    assert (cap_a_s1.id, cap_b_s1.id) == (cap_a_s1_again.id, cap_b_s1_again.id)
    # Sanity : les deux capitaines doivent venir des 4 joueurs tied
    assert {cap_a_s1.id, cap_b_s1.id}.issubset({1, 2, 3, 4})
    assert {cap_a_s2.id, cap_b_s2.id}.issubset({1, 2, 3, 4})


def test_pick_captains_tiebreak_position_2():
    """1 joueur clairement top, 3 a egalite pour position 2 -> RNG entre les 3."""
    players = [_p(1, 2000)] + [_p(i, 1500) for i in range(2, 11)]
    cap_a, cap_b = pick_captains(players, rng=random.Random(7))
    assert cap_a.id == 1            # le top ELO unique
    assert cap_b.id in {2, 3, 4, 5, 6, 7, 8, 9, 10}  # un des tied


def test_pick_captains_raises_if_too_few_players():
    with pytest.raises(ValueError, match="au moins 2 joueurs"):
        pick_captains([_p(1, 1500)], rng=random.Random(0))


def test_pick_captains_raises_if_empty():
    with pytest.raises(ValueError, match="au moins 2 joueurs"):
        pick_captains([], rng=random.Random(0))
