"""Tests du cog queue_v2 + repository queue."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.queue_v2 import (
    QueueView,
    QueueCog,
    build_queue_embed,
    JOIN_BTN_ID,
    LEAVE_BTN_ID,
)
from services import repository


def _fake_member(member_id: int, name: str = "User"):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    return m


def _fake_guild(guild_id: int = 42, name: str = "TestGuild"):
    g = MagicMock()
    g.id = guild_id
    g.name = name
    return g


def _fake_interaction(user, guild_id: int = 42):
    inter = MagicMock()
    inter.user = user
    inter.guild = _fake_guild(guild_id)
    inter.guild_id = guild_id
    inter.channel_id = 100
    inter.channel = MagicMock()
    inter.channel.id = 100
    inter.channel.guild = inter.guild
    inter.channel.send = AsyncMock(return_value=MagicMock(id=999))
    inter.channel.mention = "#general"
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()
    inter.message = MagicMock()
    inter.message.id = 999
    return inter


def _seed_riot_link(db, guild_id: int, user_id: int, elo: int = 1500):
    repository.link_riot_account(
        db, guild_id=guild_id, user_id=user_id,
        riot_name=f"P{user_id}", riot_tag="EUW", riot_region="eu",
        puuid=f"pu-{user_id}", peak_elo=elo, source="peak_recent",
    )
    repository.get_elo_col(db, guild_id).insert_one({
        "_id": str(user_id), "name": f"P{user_id}",
        "elo": elo, "wins": 0, "losses": 0, "linked_once": True,
    })


def _seed_active_queue(db, guild_id: int = 42):
    repository.setup_active_queue(db, guild_id=guild_id, channel_id=100, message_id=999)


# ── Repository : add/remove ───────────────────────────────────────
def test_repo_add_player_to_no_queue():
    import bot as bot_module
    res = repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=1)
    assert not res.success
    assert res.reason == "no_queue"


def test_repo_add_player_success():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    res = repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=1)
    assert res.success
    assert res.reason == "added"
    assert "1" in res.queue["players"]


def test_repo_add_player_already_in():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=1)
    res = repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=1)
    assert not res.success
    assert res.reason == "already_in"


def test_repo_add_player_queue_full():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    for i in range(10):
        repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=i)
    res = repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=99)
    assert not res.success
    assert res.reason == "queue_full"


def test_repo_add_player_when_closed():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    repository.close_active_queue(bot_module.db, guild_id=42)
    res = repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=1)
    assert not res.success
    assert res.reason == "queue_closed"


def test_repo_remove_player_not_in():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    res = repository.remove_player_from_queue(bot_module.db, guild_id=42, user_id=1)
    assert not res.success
    assert res.reason == "not_in"


def test_repo_remove_player_success():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=1)
    res = repository.remove_player_from_queue(bot_module.db, guild_id=42, user_id=1)
    assert res.success
    assert res.reason == "removed"
    assert "1" not in res.queue["players"]


def test_repo_delete_active_queue():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    assert repository.delete_active_queue(bot_module.db, guild_id=42) is True
    assert repository.get_active_queue(bot_module.db, guild_id=42) is None


# ── Embed ─────────────────────────────────────────────────────────
def test_embed_empty_queue():
    embed = build_queue_embed(None, _fake_guild())
    assert "0/10" in embed.title
    assert any("Personne" in f.value for f in embed.fields)


def test_embed_with_players():
    doc = {"players": ["1", "2", "3"], "status": "open"}
    embed = build_queue_embed(doc, _fake_guild())
    assert "3/10" in embed.title
    field_value = next(f.value for f in embed.fields if f.name == "Joueurs")
    assert "<@1>" in field_value
    assert "<@2>" in field_value


def test_embed_full_queue():
    doc = {"players": [str(i) for i in range(10)], "status": "open"}
    embed = build_queue_embed(doc, _fake_guild())
    assert "10/10" in embed.title
    assert "pleine" in embed.description.lower()


def test_embed_forming_queue():
    doc = {"players": [str(i) for i in range(10)], "status": "forming"}
    embed = build_queue_embed(doc, _fake_guild())
    assert "formation" in embed.description.lower()


# ── Bouton Rejoindre ──────────────────────────────────────────────
async def test_join_without_riot_account_refuses():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    view = QueueView(bot_module.db)
    inter = _fake_interaction(_fake_member(1))

    await view.join_btn.callback(inter)

    inter.followup.send.assert_awaited_once()
    args, kwargs = inter.followup.send.call_args
    assert "Riot" in args[0]
    assert kwargs.get("ephemeral") is True
    inter.edit_original_response.assert_not_awaited()


async def test_join_no_active_queue_refuses():
    import bot as bot_module
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    view = QueueView(bot_module.db)
    inter = _fake_interaction(_fake_member(1))

    await view.join_btn.callback(inter)
    args, _ = inter.followup.send.call_args
    assert "Aucune queue" in args[0]


async def test_join_success_updates_message():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    view = QueueView(bot_module.db)
    inter = _fake_interaction(_fake_member(1, "Jet"))

    await view.join_btn.callback(inter)

    inter.edit_original_response.assert_awaited_once()
    embed = inter.edit_original_response.call_args.kwargs["embed"]
    assert "1/10" in embed.title


async def test_join_success_sends_ephemeral_confirmation():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    view = QueueView(bot_module.db)
    inter = _fake_interaction(_fake_member(1, "Jet"))

    await view.join_btn.callback(inter)

    inter.followup.send.assert_awaited_once()
    args, kwargs = inter.followup.send.call_args
    msg = args[0]
    assert "rejoint" in msg.lower()
    assert "1/10" in msg
    assert kwargs.get("ephemeral") is True
    inter.channel.send.assert_not_awaited()


async def test_join_already_in_refuses():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=1)

    view = QueueView(bot_module.db)
    inter = _fake_interaction(_fake_member(1))
    await view.join_btn.callback(inter)

    args, _ = inter.followup.send.call_args
    assert "deja dans la queue" in args[0]


async def test_join_10th_player_triggers_on_full():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    for i in range(10):
        _seed_riot_link(bot_module.db, guild_id=42, user_id=i, elo=1500 + i * 50)
    # 9 deja en queue
    for i in range(9):
        repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=i)

    triggered = []
    async def on_full(inter, queue_doc):
        triggered.append(queue_doc)

    view = QueueView(bot_module.db, on_full=on_full)
    inter = _fake_interaction(_fake_member(9))
    await view.join_btn.callback(inter)

    # Laisse une chance a la task de tourner
    import asyncio
    await asyncio.sleep(0)

    assert len(triggered) == 1
    assert len(triggered[0]["players"]) == 10
    # La queue est passee en status "forming"
    queue = repository.get_active_queue(bot_module.db, guild_id=42)
    assert queue["status"] == "forming"


async def test_join_when_queue_forming_refuses():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.close_active_queue(bot_module.db, guild_id=42)

    view = QueueView(bot_module.db)
    inter = _fake_interaction(_fake_member(1))
    await view.join_btn.callback(inter)

    args, _ = inter.followup.send.call_args
    assert "fermee" in args[0]


# ── Bouton Quitter ────────────────────────────────────────────────
async def test_leave_when_not_in_queue_refuses():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    view = QueueView(bot_module.db)
    inter = _fake_interaction(_fake_member(1))

    await view.leave_btn.callback(inter)
    args, _ = inter.followup.send.call_args
    assert "n'es pas dans la queue" in args[0]


async def test_leave_success_updates_message():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=1)

    view = QueueView(bot_module.db)
    inter = _fake_interaction(_fake_member(1))
    await view.leave_btn.callback(inter)

    inter.edit_original_response.assert_awaited_once()
    embed = inter.edit_original_response.call_args.kwargs["embed"]
    assert "0/10" in embed.title


# ── /setup-queue ─────────────────────────────────────────────────
async def test_setup_queue_creates_active_queue():
    import bot as bot_module
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))

    await cog.setup_queue.callback(cog, inter)

    inter.channel.send.assert_awaited_once()
    queue = repository.get_active_queue(bot_module.db, guild_id=42)
    assert queue is not None
    assert queue["channel_id"] == 100
    assert queue["message_id"] == 999
    assert queue["status"] == "open"
    assert queue["players"] == []


async def test_setup_queue_replaces_existing():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=1) \
        if False else None  # only seed_active_queue, players still empty

    # Add a player to old queue
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.add_player_to_queue(bot_module.db, guild_id=42, user_id=1)
    old = repository.get_active_queue(bot_module.db, guild_id=42)
    assert "1" in old["players"]

    # Re-setup
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))
    await cog.setup_queue.callback(cog, inter)

    new = repository.get_active_queue(bot_module.db, guild_id=42)
    assert new["players"] == []  # reset


# ── /close-queue ─────────────────────────────────────────────────
async def test_close_queue_when_active():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))
    await cog.close_queue.callback(cog, inter)

    args, _ = inter.response.send_message.call_args
    assert "supprimee" in args[0]
    assert repository.get_active_queue(bot_module.db, guild_id=42) is None


async def test_close_queue_when_no_queue():
    import bot as bot_module
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))
    await cog.close_queue.callback(cog, inter)

    args, _ = inter.response.send_message.call_args
    assert "Aucune" in args[0]


# ── Custom IDs des boutons (pour persistance) ──────────────────────
def test_button_custom_ids_are_stable():
    """Les custom_ids ne doivent JAMAIS changer (persistance apres restart)."""
    assert JOIN_BTN_ID == "queue_v2:join"
    assert LEAVE_BTN_ID == "queue_v2:leave"
