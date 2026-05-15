"""Tests d'integration pour CaptainDraftSession (UI Discord avec fakes)."""
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.captain_draft import (
    CaptainDraftSession,
    DraftCancelledError,
)
from services.team_balancer import Player


pytestmark = pytest.mark.integration

ADMIN_ROLES = ("Admin", "Match Staff")


def _p(uid: int, elo: int) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=elo)


def _fake_role(name: str):
    r = MagicMock()
    r.name = name
    return r


def _fake_user(user_id: int, role_names: tuple[str, ...] = ()):
    u = MagicMock()
    u.id = user_id
    u.mention = f"<@{user_id}>"
    u.roles = [_fake_role(n) for n in role_names]
    return u


def _fake_interaction(user, custom_id: str, values: list[str] | None = None):
    inter = MagicMock()
    inter.user = user
    inter.data = {"custom_id": custom_id}
    if values is not None:
        inter.data["values"] = values
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.message = MagicMock()
    inter.message.edit = AsyncMock()
    return inter


def _fake_prep_channel():
    ch = MagicMock()
    msg = MagicMock()
    msg.edit = AsyncMock()
    msg.id = 12345
    ch.send = AsyncMock(return_value=msg)
    return ch, msg


async def test_session_happy_path_8_picks_complete():
    """Simule 8 picks consecutifs : session.run() termine avec un DraftResult."""
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    prep_channel, draft_msg = _fake_prep_channel()

    session = CaptainDraftSession(
        prep_channel=prep_channel,
        cap_a=cap_a,
        cap_b=cap_b,
        pool=pool,
        admin_role_names=ADMIN_ROLES,
    )

    run_task = asyncio.create_task(session.run())

    # Attend que le message soit poste (initialisation)
    for _ in range(50):
        if session.message is not None:
            break
        await asyncio.sleep(0.01)
    assert session.message is not None, "Le message draft doit etre poste au demarrage"

    # Ordre snake : A, B, B, A, A, B, B, A
    pick_users = [cap_a, cap_b, cap_b, cap_a, cap_a, cap_b, cap_b, cap_a]
    for i, picker in enumerate(pick_users):
        inter = _fake_interaction(picker, "pro_draft_pick", values=[str(pool[i].id)])
        await session._on_pick(inter)

    result = await asyncio.wait_for(run_task, timeout=1.0)
    assert len(result.team_a) == 5
    assert len(result.team_b) == 5
    assert result.cap_a is cap_a
    assert result.cap_b is cap_b


async def test_session_admin_cancel_raises():
    """Un admin clique Cancel -> run() leve DraftCancelledError."""
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    prep_channel, _ = _fake_prep_channel()

    session = CaptainDraftSession(
        prep_channel=prep_channel,
        cap_a=cap_a,
        cap_b=cap_b,
        pool=pool,
        admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    for _ in range(50):
        if session.message is not None:
            break
        await asyncio.sleep(0.01)

    admin = _fake_user(99, role_names=("Admin",))
    inter = _fake_interaction(admin, "pro_draft_cancel")
    await session._on_cancel(inter)

    with pytest.raises(DraftCancelledError) as exc_info:
        await asyncio.wait_for(run_task, timeout=1.0)
    assert exc_info.value.reason == "admin"


async def test_session_non_admin_cancel_rejected_by_interaction_check():
    """Un non-admin qui clique Cancel : interaction_check renvoie False (ephemeral)."""
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    prep_channel, _ = _fake_prep_channel()
    session = CaptainDraftSession(
        prep_channel=prep_channel,
        cap_a=cap_a,
        cap_b=cap_b,
        pool=pool,
        admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    for _ in range(50):
        if session.message is not None:
            break
        await asyncio.sleep(0.01)

    rando = _fake_user(500, role_names=())  # pas de role admin
    inter = _fake_interaction(rando, "pro_draft_cancel")
    ok = await session._interaction_check(inter)
    assert ok is False
    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    # Le draft est toujours en picking
    assert session.state.status == "picking"

    # Cleanup : annuler la task avant qu'elle finisse pas (sinon warning)
    run_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run_task


async def test_session_pick_by_wrong_captain_rejected_by_interaction_check():
    """Cap B clique pendant tour de Cap A : interaction_check renvoie False."""
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    prep_channel, _ = _fake_prep_channel()
    session = CaptainDraftSession(
        prep_channel=prep_channel,
        cap_a=cap_a,
        cap_b=cap_b,
        pool=pool,
        admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    for _ in range(50):
        if session.message is not None:
            break
        await asyncio.sleep(0.01)

    # Tour 0 == cap_a, cap_b ne doit pas pouvoir pick
    inter = _fake_interaction(cap_b, "pro_draft_pick", values=[str(pool[0].id)])
    ok = await session._interaction_check(inter)
    assert ok is False
    inter.response.send_message.assert_awaited_once()
    assert session.state.turn_index == 0

    # Cleanup
    run_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run_task


async def test_session_double_pick_same_player_is_idempotent():
    """2 _on_pick concurrents sur le meme player -> 1 pick applique."""
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    prep_channel, _ = _fake_prep_channel()
    session = CaptainDraftSession(
        prep_channel=prep_channel,
        cap_a=cap_a,
        cap_b=cap_b,
        pool=pool,
        admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    for _ in range(50):
        if session.message is not None:
            break
        await asyncio.sleep(0.01)

    target = pool[0]
    inter1 = _fake_interaction(cap_a, "pro_draft_pick", values=[str(target.id)])
    inter2 = _fake_interaction(cap_a, "pro_draft_pick", values=[str(target.id)])
    # Lance les deux callbacks en parallele
    await asyncio.gather(session._on_pick(inter1), session._on_pick(inter2))
    # Apres : 1 seul pick applique
    assert session.state.turn_index == 1
    assert target in session.state.team_a
    assert target not in session.state.pool
    # Un des 2 a recu un ephemeral "deja drafte"
    n_ephemeral = sum(
        1 for i in (inter1, inter2)
        if i.response.send_message.await_count > 0
    )
    assert n_ephemeral == 1

    # Cleanup
    run_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run_task
