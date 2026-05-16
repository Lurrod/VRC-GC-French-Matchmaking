"""Tests purs pour le draft capitaine de la Pro Queue."""
from __future__ import annotations

import random

import pytest

from types import SimpleNamespace

from services.captain_draft import DraftState, PICK_SEQUENCE, pick_captains, DraftResult, _is_admin
from services.match_service import build_plan_from_draft
from services.team_balancer import Player

pytestmark = pytest.mark.unit


def _p(uid: int, elo: int) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=elo)


def test_pick_captains_returns_two_distinct_players_from_pool():
    """Les capitaines sont 2 joueurs distincts tires de la liste, peu importe l'ELO."""
    players = [
        _p(1, 1000), _p(2, 1100), _p(3, 1200), _p(4, 1300), _p(5, 1400),
        _p(6, 1500), _p(7, 1600), _p(8, 1700), _p(9, 1800), _p(10, 1900),
    ]
    rng = random.Random(42)
    cap_a, cap_b = pick_captains(players, rng=rng)
    ids = {p.id for p in players}
    assert cap_a.id != cap_b.id
    assert cap_a.id in ids
    assert cap_b.id in ids


def test_pick_captains_is_reproducible_with_same_seed():
    """Meme seed -> meme paire de capitaines."""
    players = [_p(i, 1000 + i * 50) for i in range(1, 11)]
    cap_a_1, cap_b_1 = pick_captains(players, rng=random.Random(1))
    cap_a_2, cap_b_2 = pick_captains(players, rng=random.Random(1))
    assert (cap_a_1.id, cap_b_1.id) == (cap_a_2.id, cap_b_2.id)


def test_pick_captains_ignores_elo():
    """Le top ELO n'est PAS garanti d'etre capitaine (selection purement aleatoire).

    On verifie qu'au moins sur quelques seeds, le joueur top ELO n'est pas pris
    comme capitaine — preuve que l'ELO n'influence plus la selection.
    """
    players = [_p(1, 5000)] + [_p(i, 1000) for i in range(2, 11)]
    top_elo_id = 1
    seen_without_top = False
    for seed in range(50):
        cap_a, cap_b = pick_captains(players, rng=random.Random(seed))
        if top_elo_id not in (cap_a.id, cap_b.id):
            seen_without_top = True
            break
    assert seen_without_top, "Le top ELO devrait pouvoir ne pas etre capitaine"


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


def test_draft_result_from_state_when_complete():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    result = DraftResult.from_state(state)
    assert result.cap_a is state.cap_a
    assert result.cap_b is state.cap_b
    assert len(result.team_a) == 5 and len(result.team_b) == 5


def test_draft_result_rejects_incomplete_state():
    state, _ = _make_state_with_8_pool()
    with pytest.raises(ValueError, match="non termine"):
        DraftResult.from_state(state)


ADMIN_ROLE_NAMES = ("Admin", "Match Staff", "Administrateur")


def _fake_user(*, role_names: tuple[str, ...] = (), manage_guild: bool = False):
    """Mime un discord.Member pour `_is_admin` : `roles` + `guild_permissions`."""
    return SimpleNamespace(
        roles=[SimpleNamespace(name=n) for n in role_names],
        guild_permissions=SimpleNamespace(manage_guild=manage_guild),
    )


def test_is_admin_accepts_manage_guild_permission():
    """Un admin Discord (manage_guild=True) doit pouvoir annuler le draft,
    meme sans role nomme 'Admin'/'Match Staff'/'Administrateur'."""
    user = _fake_user(role_names=("Administrator",), manage_guild=True)
    assert _is_admin(user, ADMIN_ROLE_NAMES) is True


def test_is_admin_accepts_named_admin_role_as_fallback():
    """Compat : un user avec role 'Match Staff' mais sans manage_guild
    reste autorise (cas d'un staff sans permissions elevees)."""
    user = _fake_user(role_names=("Match Staff",), manage_guild=False)
    assert _is_admin(user, ADMIN_ROLE_NAMES) is True


def test_is_admin_rejects_regular_user():
    user = _fake_user(role_names=("Member",), manage_guild=False)
    assert _is_admin(user, ADMIN_ROLE_NAMES) is False


def test_is_admin_handles_missing_attributes():
    """Robustesse : un objet sans `guild_permissions` ni `roles` ne crashe pas."""
    bare = SimpleNamespace()
    assert _is_admin(bare, ADMIN_ROLE_NAMES) is False


def test_build_plan_from_draft_uses_capA_as_leader():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    result = DraftResult.from_state(state)
    plan = build_plan_from_draft(
        result, free_category="Match #1", rng=random.Random(42),
    )
    assert plan.category_name == "Match #1"
    assert plan.lobby_leader is state.cap_a
    assert plan.teams.team_a == result.team_a
    assert plan.teams.team_b == result.team_b
    # map_name est choisi par rng parmi elo_calc.MAPS, non vide
    assert plan.map_name
