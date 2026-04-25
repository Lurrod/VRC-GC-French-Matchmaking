"""Tests d'integration du cog match (formation + persistance + reset queue)."""

import random
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.match import MatchCog, VoteView, build_match_embed, VOTE_A_BTN_ID, VOTE_B_BTN_ID
from services import repository
from services.team_balancer import Player


def _fake_member(member_id: int, name: str = "User"):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    return m


def _fake_category(name: str, t1_empty: bool = True, t2_empty: bool = True):
    cat = MagicMock()
    cat.name = name
    t1 = MagicMock(); t1.name = "Team 1"; t1.members = [] if t1_empty else [object()]
    t2 = MagicMock(); t2.name = "Team 2"; t2.members = [] if t2_empty else [object()]
    cat.voice_channels = [t1, t2]
    return cat


def _fake_channel(channel_id: int = 100):
    ch = MagicMock()
    ch.id = channel_id
    ch.send = AsyncMock(return_value=MagicMock(id=555, channel=ch))
    return ch


def _fake_guild(guild_id: int = 42, members=None, categories=None, channel=None):
    g = MagicMock()
    g.id = guild_id
    g.name = "TestGuild"
    g.members = members or []
    g.categories = categories or []
    g.get_member = lambda mid: next((m for m in g.members if m.id == int(mid)), None)
    g.get_channel = lambda cid: channel
    return g


def _fake_interaction(guild, user=None):
    inter = MagicMock()
    inter.guild = guild
    inter.user = user or _fake_member(1)
    inter.guild_id = guild.id
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _seed_full_queue(db, guild_id: int, channel_id: int = 100):
    """Cree la queue active + 10 comptes Riot lies."""
    repository.setup_active_queue(db, guild_id=guild_id, channel_id=channel_id, message_id=999)
    for i in range(10):
        repository.link_riot_account(
            db, guild_id=guild_id, user_id=i,
            riot_name=f"P{i}", riot_tag="EUW", riot_region="eu",
            puuid=f"pu{i}",
            effective_elo=1500 + i * 50,
            peak_elo=1500 + i * 50,
            source="peak_recent",
        )
        repository.add_player_to_queue(db, guild_id=guild_id, user_id=i)
    return repository.get_active_queue(db, guild_id=guild_id)


# ── on_queue_full : succes ────────────────────────────────────────
async def test_on_queue_full_posts_message_with_view():
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members,
                        categories=[_fake_category("Match #1")],
                        channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    match_id = await cog.on_queue_full(inter, queue_doc)

    # Message envoye
    channel.send.assert_awaited_once()
    args, kwargs = channel.send.call_args
    assert "Match trouve" in kwargs["content"]
    # Tous les joueurs mentionnes
    for i in range(10):
        assert f"<@{i}>" in kwargs["content"]

    # Embed bien forme
    embed = kwargs["embed"]
    assert "Map" in embed.description
    assert any("Team A" in f.name for f in embed.fields)
    assert any("Team B" in f.name for f in embed.fields)

    # View attache
    assert isinstance(kwargs["view"], VoteView)


async def test_on_queue_full_persists_match():
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members,
                        categories=[_fake_category("Match #1")], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc)

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match is not None
    assert match["status"] == "pending"
    assert match["map"] in ("Breeze", "Bind", "Lotus", "Fracture", "Split", "Haven", "Pearl")
    assert match["category_name"] == "Match #1"
    assert match["message_id"] == 555
    assert match["channel_id"] == 100
    assert len(match["team_a"]) == 5
    assert len(match["team_b"]) == 5
    assert match["votes"] == {}
    assert int(match["lobby_leader_id"]) in range(10)


async def test_on_queue_full_resets_queue():
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members,
                        categories=[_fake_category("Match #1")], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc)

    assert repository.get_active_queue(bot_module.db, guild_id=42) is None


async def test_on_queue_full_uses_no_category_when_none_free():
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members,
                        # toutes les categories occupees
                        categories=[
                            _fake_category("Match #1", t1_empty=False),
                            _fake_category("Match #2", t2_empty=False),
                            _fake_category("Match #3", t1_empty=False),
                        ],
                        channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc)

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match["category_name"] is None
    embed = channel.send.call_args.kwargs["embed"]
    assert any("Aucune categorie libre" in f.value for f in embed.fields)


async def test_on_queue_full_balanced_teams_in_persistence():
    import bot as bot_module
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members,
                        categories=[_fake_category("Match #1")], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc)

    match = repository.get_match(bot_module.db, 42, match_id)
    sum_a = sum(p["elo"] for p in match["team_a"])
    sum_b = sum(p["elo"] for p in match["team_b"])
    # Les elos ont ete distribues comme 1500..1950 step 50, total 17250 -> ideal 8625
    diff = abs(sum_a - sum_b)
    assert diff <= 100, f"diff={diff}, attendu <=100 sur cet ensemble"


# ── on_queue_full : echec gracieux ────────────────────────────────
async def test_on_queue_full_aborts_if_player_unlinked():
    import bot as bot_module
    # Setup queue avec 10 joueurs, mais on retire le compte Riot du joueur 5
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    repository.unlink_riot_account(bot_module.db, guild_id=42, user_id=5)

    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members,
                        categories=[_fake_category("Match #1")], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    result = await cog.on_queue_full(inter, queue_doc)

    assert result is None  # echec
    # Le message d'erreur a ete envoye, pas le match
    channel.send.assert_awaited()
    err_call = channel.send.call_args
    # 2 cas : un seul appel (l'erreur) ou deux (erreur + autre)
    # On verifie qu'au moins un message contient "annule"
    sent_contents = " ".join(
        str(call.kwargs.get("content", "")) + " " + str(call.args[0] if call.args else "")
        for call in channel.send.call_args_list
    )
    assert "annule" in sent_contents.lower() or "annul" in sent_contents.lower()
    # La queue est supprimee
    assert repository.get_active_queue(bot_module.db, guild_id=42) is None


# ── VoteView stub (Phase 4 — Phase 5 implementera) ────────────────
async def test_vote_view_buttons_have_stable_custom_ids():
    import bot as bot_module
    view = VoteView(bot_module.db)
    # Cherche les custom_ids dans les children
    custom_ids = {c.custom_id for c in view.children}
    assert VOTE_A_BTN_ID in custom_ids
    assert VOTE_B_BTN_ID in custom_ids


# ── build_match_embed ─────────────────────────────────────────────
def test_build_match_embed_shows_all_players_and_map():
    from services.match_service import MatchPlan
    from services.team_balancer import balance_teams, Player

    players = [Player(id=i, name=f"P{i}", elo=1500 + i*50) for i in range(10)]
    teams = balance_teams(players)
    plan = MatchPlan(teams=teams, map_name="Bind", lobby_leader=players[0], category_name="Match #1")

    embed = build_match_embed(plan, "MyGuild")
    assert "Bind" in embed.description
    assert "<@0>" in embed.description  # leader
    fields_str = " ".join(f.value for f in embed.fields)
    for i in range(10):
        assert f"<@{i}>" in fields_str
    assert "Match #1" in fields_str
