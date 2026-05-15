"""Tests d'integration pour CaptainDraftSession (UI Discord avec fakes)."""
from __future__ import annotations

import asyncio
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
