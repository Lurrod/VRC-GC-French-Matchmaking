"""Tests du cog queue_v2 + repository queue."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.queue_v2 import (
    QueueView,
    QueueCog,
    build_queue_embed,
)
from services import repository


def _fake_member(member_id: int, name: str = "User"):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.roles = []
    m.voice = None
    # Methodes async explicites — necessaires car __class__=discord.Member
    # ci-dessous fige le type et empeche l'auto-creation d'AsyncMock.
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    m.move_to = AsyncMock()
    # Marque comme discord.Member pour passer le fail-safe isinstance check
    # dans _join_callback (gate de role).
    m.__class__ = discord.Member
    return m


def _fake_guild(guild_id: int = 42, name: str = "TestGuild"):
    g = MagicMock()
    g.id = guild_id
    g.name = name
    g.roles = []
    g.voice_channels = []
    g.get_channel = MagicMock(return_value=None)
    return g


def _fake_interaction(
    user, guild_id: int = 42, channel_name: str = "open-queue",
):
    inter = MagicMock()
    inter.user = user
    inter.guild = _fake_guild(guild_id)
    inter.guild_id = guild_id
    inter.channel_id = 100
    inter.channel = MagicMock()
    inter.channel.id = 100
    inter.channel.name = channel_name
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
    # Compound _id pour matcher la nouvelle architecture per-queue.
    repository.get_elo_col(db, guild_id).insert_one({
        "_id": f"{user_id}:open",
        "name": f"P{user_id}",
        "elo": elo, "wins": 0, "losses": 0,
        "queue_type": "open", "user_id": str(user_id),
    })


def _seed_active_queue(db, guild_id: int = 42, queue_type: str = "open"):
    repository.setup_active_queue(
        db, guild_id=guild_id, queue_type=queue_type,
        channel_id=100, message_id=999,
    )


# ── Repository : add/remove ───────────────────────────────────────
def test_repo_add_player_to_no_queue():
    import bot as bot_module
    res = repository.add_player_to_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )
    assert not res.success
    assert res.reason == "no_queue"


def test_repo_add_player_success():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    res = repository.add_player_to_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )
    assert res.success
    assert res.reason == "added"
    assert "1" in res.queue["players"]


def test_repo_add_player_already_in():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    repository.add_player_to_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )
    res = repository.add_player_to_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )
    assert not res.success
    assert res.reason == "already_in"


def test_repo_add_player_queue_full():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    for i in range(10):
        repository.add_player_to_queue(
            bot_module.db, guild_id=42, queue_type="open", user_id=i,
        )
    res = repository.add_player_to_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=99,
    )
    assert not res.success
    assert res.reason == "queue_full"


def test_repo_add_player_when_closed():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    repository.close_active_queue(bot_module.db, guild_id=42, queue_type="open")
    res = repository.add_player_to_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )
    assert not res.success
    assert res.reason == "queue_closed"


def test_repo_remove_player_not_in():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    res = repository.remove_player_from_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )
    assert not res.success
    assert res.reason == "not_in"


def test_repo_remove_player_success():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    repository.add_player_to_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )
    res = repository.remove_player_from_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )
    assert res.success
    assert res.reason == "removed"
    assert "1" not in res.queue["players"]


def test_repo_delete_active_queue():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    assert repository.delete_active_queue(
        bot_module.db, guild_id=42, queue_type="open",
    ) is True
    assert repository.get_active_queue(
        bot_module.db, guild_id=42, queue_type="open",
    ) is None


# ── Embed ─────────────────────────────────────────────────────────
def test_embed_empty_queue():
    embed = build_queue_embed(None, _fake_guild(), "open")
    assert "0/10" in embed.title
    assert any("Personne" in f.value for f in embed.fields)


def test_embed_with_players():
    doc = {"players": ["1", "2", "3"], "status": "open"}
    embed = build_queue_embed(doc, _fake_guild(), "open")
    assert "3/10" in embed.title
    field_value = next(f.value for f in embed.fields if f.name == "Joueurs")
    assert "<@1>" in field_value
    assert "<@2>" in field_value


def test_embed_full_queue():
    doc = {"players": [str(i) for i in range(10)], "status": "open"}
    embed = build_queue_embed(doc, _fake_guild(), "open")
    assert "10/10" in embed.title
    assert "pleine" in embed.description.lower()


def test_embed_forming_queue():
    doc = {"players": [str(i) for i in range(10)], "status": "forming"}
    embed = build_queue_embed(doc, _fake_guild(), "open")
    assert "formation" in embed.description.lower()


def test_embed_title_per_queue_type():
    """Chaque queue_type affiche son label dans le titre."""
    g = _fake_guild()
    pro_embed  = build_queue_embed(None, g, "pro")
    open_embed = build_queue_embed(None, g, "open")
    gc_embed   = build_queue_embed(None, g, "gc")
    assert "Pro Queue"  in pro_embed.title
    assert "Open Queue" in open_embed.title
    assert "GC Queue"   in gc_embed.title


# ── Bouton Rejoindre ──────────────────────────────────────────────
async def test_join_without_riot_account_refuses():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    view = QueueView(bot_module.db, queue_type="open")
    inter = _fake_interaction(_fake_member(1))

    await view._join_callback(inter)

    inter.followup.send.assert_awaited_once()
    args, kwargs = inter.followup.send.call_args
    assert "Riot" in args[0]
    assert kwargs.get("ephemeral") is True
    inter.edit_original_response.assert_not_awaited()


async def test_join_no_active_queue_refuses():
    import bot as bot_module
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    view = QueueView(bot_module.db, queue_type="open")
    inter = _fake_interaction(_fake_member(1))

    await view._join_callback(inter)
    args, _ = inter.followup.send.call_args
    assert "Aucune queue" in args[0]


async def test_join_success_updates_message():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    view = QueueView(bot_module.db, queue_type="open")
    inter = _fake_interaction(_fake_member(1, "Jet"))

    await view._join_callback(inter)

    inter.edit_original_response.assert_awaited_once()
    embed = inter.edit_original_response.call_args.kwargs["embed"]
    assert "1/10" in embed.title


async def test_join_success_sends_ephemeral_confirmation():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    view = QueueView(bot_module.db, queue_type="open")
    inter = _fake_interaction(_fake_member(1, "Jet"))

    await view._join_callback(inter)

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
    repository.add_player_to_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )

    view = QueueView(bot_module.db, queue_type="open")
    inter = _fake_interaction(_fake_member(1))
    await view._join_callback(inter)

    args, _ = inter.followup.send.call_args
    assert "deja dans la queue" in args[0]


async def test_join_10th_player_triggers_on_full():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    for i in range(10):
        _seed_riot_link(bot_module.db, guild_id=42, user_id=i, elo=1500 + i * 50)
    # 9 deja en queue
    for i in range(9):
        repository.add_player_to_queue(
            bot_module.db, guild_id=42, queue_type="open", user_id=i,
        )

    triggered = []
    async def on_full(inter, queue_doc, queue_type):
        triggered.append((queue_doc, queue_type))

    view = QueueView(bot_module.db, queue_type="open", on_full=on_full)
    inter = _fake_interaction(_fake_member(9))
    await view._join_callback(inter)

    # Laisse une chance a la task de tourner
    import asyncio
    await asyncio.sleep(0)

    assert len(triggered) == 1
    queue_doc, queue_type = triggered[0]
    assert len(queue_doc["players"]) == 10
    assert queue_type == "open"
    # La queue est passee en status "forming"
    queue = repository.get_active_queue(
        bot_module.db, guild_id=42, queue_type="open",
    )
    assert queue["status"] == "forming"


async def test_join_when_queue_forming_refuses():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.close_active_queue(bot_module.db, guild_id=42, queue_type="open")

    view = QueueView(bot_module.db, queue_type="open")
    inter = _fake_interaction(_fake_member(1))
    await view._join_callback(inter)

    args, _ = inter.followup.send.call_args
    assert "fermee" in args[0]


# ── Bouton Quitter ────────────────────────────────────────────────
async def test_leave_when_not_in_queue_refuses():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    view = QueueView(bot_module.db, queue_type="open")
    inter = _fake_interaction(_fake_member(1))

    await view._leave_callback(inter)
    args, _ = inter.followup.send.call_args
    assert "n'es pas dans la queue" in args[0]


async def test_leave_success_updates_message():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.add_player_to_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )

    view = QueueView(bot_module.db, queue_type="open")
    inter = _fake_interaction(_fake_member(1))
    await view._leave_callback(inter)

    inter.edit_original_response.assert_awaited_once()
    embed = inter.edit_original_response.call_args.kwargs["embed"]
    assert "0/10" in embed.title


# ── /setup-queue ─────────────────────────────────────────────────
async def test_setup_queue_creates_active_queue():
    import bot as bot_module
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))

    await cog.setup_queue.callback(cog, inter, queue="open")

    inter.channel.send.assert_awaited_once()
    queue = repository.get_active_queue(
        bot_module.db, guild_id=42, queue_type="open",
    )
    assert queue is not None
    assert queue["channel_id"] == 100
    assert queue["message_id"] == 999
    assert queue["status"] == "open"
    assert queue["players"] == []


async def test_setup_queue_replaces_existing():
    import bot as bot_module
    _seed_active_queue(bot_module.db)

    # Add a player to old queue
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.add_player_to_queue(
        bot_module.db, guild_id=42, queue_type="open", user_id=1,
    )
    old = repository.get_active_queue(
        bot_module.db, guild_id=42, queue_type="open",
    )
    assert "1" in old["players"]

    # Re-setup
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))
    await cog.setup_queue.callback(cog, inter, queue="open")

    new = repository.get_active_queue(
        bot_module.db, guild_id=42, queue_type="open",
    )
    assert new["players"] == []  # reset


# ── /close-queue ─────────────────────────────────────────────────
async def test_close_queue_when_active():
    import bot as bot_module
    _seed_active_queue(bot_module.db)
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))
    await cog.close_queue.callback(cog, inter, queue="open")

    args, _ = inter.response.send_message.call_args
    assert "supprimee" in args[0]
    assert repository.get_active_queue(
        bot_module.db, guild_id=42, queue_type="open",
    ) is None


async def test_close_queue_when_no_queue():
    import bot as bot_module
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))
    await cog.close_queue.callback(cog, inter, queue="open")

    args, _ = inter.response.send_message.call_args
    assert "Aucune" in args[0]


async def test_setup_queue_rejects_wrong_channel():
    """/setup-queue open dans un salon autre que #open-queue est refuse."""
    import bot as bot_module
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(
        _fake_member(99), channel_name="general",
    )

    await cog.setup_queue.callback(cog, inter, queue="open")

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert "open-queue" in args[0]
    assert kwargs.get("ephemeral") is True
    inter.channel.send.assert_not_awaited()
    assert repository.get_active_queue(
        bot_module.db, guild_id=42, queue_type="open",
    ) is None


async def test_setup_queue_rejects_pro_in_open_channel():
    """/setup-queue pro dans #open-queue est refuse (par type de queue)."""
    import bot as bot_module
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(
        _fake_member(99), channel_name="open-queue",
    )

    await cog.setup_queue.callback(cog, inter, queue="pro")

    args, _ = inter.response.send_message.call_args
    assert "pro-queue" in args[0]
    inter.channel.send.assert_not_awaited()


async def test_close_queue_deletes_persistent_message():
    """/close-queue supprime le message Rejoindre/Quitter dans Discord."""
    import bot as bot_module
    _seed_active_queue(bot_module.db)

    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))

    fake_msg = MagicMock()
    fake_msg.delete = AsyncMock()
    fake_channel = MagicMock()
    fake_channel.fetch_message = AsyncMock(return_value=fake_msg)
    inter.guild.get_channel = MagicMock(return_value=fake_channel)

    await cog.close_queue.callback(cog, inter, queue="open")

    inter.guild.get_channel.assert_called_once_with(100)
    fake_channel.fetch_message.assert_awaited_once_with(999)
    fake_msg.delete.assert_awaited_once()
    assert repository.get_active_queue(
        bot_module.db, guild_id=42, queue_type="open",
    ) is None


async def test_close_queue_tolerates_missing_message():
    """Si le message a deja ete supprime cote Discord, /close-queue
    ne plante pas et retire quand meme la queue de la DB."""
    import discord as _discord
    import bot as bot_module
    _seed_active_queue(bot_module.db)

    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))

    fake_channel = MagicMock()
    fake_channel.fetch_message = AsyncMock(
        side_effect=_discord.NotFound(MagicMock(status=404), "gone"),
    )
    inter.guild.get_channel = MagicMock(return_value=fake_channel)

    await cog.close_queue.callback(cog, inter, queue="open")

    args, _ = inter.response.send_message.call_args
    assert "supprimee" in args[0]
    assert repository.get_active_queue(
        bot_module.db, guild_id=42, queue_type="open",
    ) is None


# ── Custom IDs des boutons (pour persistance) ──────────────────────
async def test_button_custom_ids_per_queue_type():
    """Les custom_ids portent le queue_type pour permettre la cohabitation
    des 3 messages persistants apres restart du bot."""
    db = MagicMock()
    pro    = QueueView(db, queue_type="pro")
    open_v = QueueView(db, queue_type="open")
    gc     = QueueView(db, queue_type="gc")
    assert pro.join_btn.custom_id    == "queue_v2:join:pro"
    assert pro.leave_btn.custom_id   == "queue_v2:leave:pro"
    assert open_v.join_btn.custom_id == "queue_v2:join:open"
    assert open_v.leave_btn.custom_id == "queue_v2:leave:open"
    assert gc.join_btn.custom_id     == "queue_v2:join:gc"
    assert gc.leave_btn.custom_id    == "queue_v2:leave:gc"


# ── Tests Task 9 : 3-queue system ────────────────────────────────
async def test_join_pro_queue_requires_role():
    """Sans role 'Rank S | Pro Queue', refus de rejoindre Pro Queue."""
    import discord
    import bot as bot_module
    from cogs.queue_v2 import QueueView
    db = bot_module.db
    repository.setup_active_queue(
        db, guild_id=42, queue_type="pro", channel_id=100, message_id=999,
    )
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    member.roles = []  # pas de role Pro
    member.__class__ = discord.Member
    inter = _fake_interaction(member)
    inter.user = member

    view = QueueView(db, queue_type="pro")
    await view._join_callback(inter)

    inter.followup.send.assert_called()
    msg = inter.followup.send.call_args[0][0]
    assert "Rank S" in msg or "Pro Queue" in msg


async def test_cannot_join_two_queues_simultaneously():
    """Si dans Pro Queue, refus de rejoindre Open Queue."""
    import discord
    import bot as bot_module
    from cogs.queue_v2 import QueueView
    db = bot_module.db
    repository.setup_active_queue(
        db, guild_id=42, queue_type="pro", channel_id=100, message_id=999,
    )
    repository.setup_active_queue(
        db, guild_id=42, queue_type="open", channel_id=200, message_id=888,
    )
    repository.add_player_to_queue(
        db, guild_id=42, queue_type="pro", user_id=1,
    )
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    member.roles = []
    member.__class__ = discord.Member
    inter = _fake_interaction(member)
    inter.user = member

    view_open = QueueView(db, queue_type="open")
    await view_open._join_callback(inter)

    inter.followup.send.assert_called()
    msg = inter.followup.send.call_args[0][0]
    assert "deja" in msg.lower() or "autre queue" in msg.lower()


@pytest.mark.asyncio
async def test_join_rejects_non_member_user():
    """Si inter.user n'est pas un Member, refus du join (fail-safe)."""
    import bot as bot_module
    from cogs.queue_v2 import QueueView
    db = bot_module.db
    repository.setup_active_queue(
        db, guild_id=42, queue_type="open",
        channel_id=100, message_id=999,
    )
    _seed_riot_link(db, 42, 1)

    user = MagicMock()  # Pas un discord.Member
    user.id = 1
    user.display_name = "User"
    user.mention = "<@1>"
    # IMPORTANT : ne PAS set user.__class__ = discord.Member
    inter = _fake_interaction(user)
    inter.user = user

    view = QueueView(db, queue_type="open")
    await view._join_callback(inter)

    # Le join doit etre refuse avec un message clair
    inter.followup.send.assert_called()
    msg = inter.followup.send.call_args[0][0]
    assert "invalide" in msg.lower() or "serveur" in msg.lower()


def test_waiting_room_name_per_queue_type():
    from cogs.queue_v2 import WAITING_ROOM_NAMES
    assert WAITING_ROOM_NAMES["pro"] == "Waiting Room Pro"
    assert WAITING_ROOM_NAMES["open"] == "Waiting Room Open"
    assert WAITING_ROOM_NAMES["gc"] == "Waiting Room GC"
