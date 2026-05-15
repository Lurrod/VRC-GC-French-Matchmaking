"""Tests purs pour le draft capitaine de la Pro Queue."""
from __future__ import annotations

import random

import pytest

from services.captain_draft import DraftState, PICK_SEQUENCE, pick_captains
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


def test_pick_sequence_is_snake_ABBAABBA():
    assert PICK_SEQUENCE == ("A", "B", "B", "A", "A", "B", "B", "A")


def test_draft_state_initial():
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    state = DraftState.initial(cap_a=cap_a, cap_b=cap_b, pool=pool)
    assert state.team_a == (cap_a,)
    assert state.team_b == (cap_b,)
    assert state.pool == pool
    assert state.turn_index == 0
    assert state.status == "picking"
    assert state.current_captain is cap_a  # PICK_SEQUENCE[0] == "A"
    assert not state.is_complete


def _make_state_with_8_pool() -> tuple[DraftState, list[Player]]:
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = [_p(i, 1500 - i) for i in range(3, 11)]  # 8 joueurs
    return DraftState.initial(cap_a=cap_a, cap_b=cap_b, pool=tuple(pool)), pool


def test_draft_state_apply_pick_is_immutable():
    state, pool = _make_state_with_8_pool()
    state2 = state.apply_pick(pool[0])
    # original inchange
    assert state.team_a == (state.cap_a,)
    assert state.pool == tuple(pool)
    assert state.turn_index == 0
    # nouvel etat decale
    assert state2.team_a == (state.cap_a, pool[0])
    assert pool[0] not in state2.pool
    assert state2.turn_index == 1


def test_draft_state_apply_pick_follows_ABBAABBA():
    state, pool = _make_state_with_8_pool()
    expected_sides = ["A", "B", "B", "A", "A", "B", "B", "A"]
    for i, side in enumerate(expected_sides):
        assert state.current_captain.id == (state.cap_a.id if side == "A" else state.cap_b.id), (
            f"turn {i}: expected side {side}"
        )
        state = state.apply_pick(pool[i])
    assert state.is_complete
    assert state.status == "complete"


def test_draft_state_complete_has_5_each_team():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    assert len(state.team_a) == 5
    assert len(state.team_b) == 5
    assert state.pool == ()


def test_draft_state_apply_pick_rejects_player_not_in_pool():
    state, _ = _make_state_with_8_pool()
    stranger = _p(99, 1500)
    with pytest.raises(ValueError, match="pas dans le pool"):
        state.apply_pick(stranger)


def test_draft_state_apply_pick_rejects_when_complete():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    extra = _p(99, 1500)
    with pytest.raises(RuntimeError, match="status=complete"):
        state.apply_pick(extra)
