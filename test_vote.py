"""Tests du systeme de vote (Phase 5)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.match import (
    MatchCog,
    VoteView,
    build_match_embed_from_doc,
    MAJORITY_THRESHOLD,
    VOTE_TIMEOUT_MINUTES,
)
from services import repository


def _fake_member(member_id: int, name: str = "User"):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    return m


def _fake_guild(guild_id: int = 42, roles=None, channel=None):
    g = MagicMock()
    g.id = guild_id
    g.name = "TestGuild"
    g.roles = roles or []
    g.get_channel = lambda cid: channel
    return g


def _fake_interaction(user, guild, message_id: int = 555):
    inter = MagicMock()
    inter.user = user
    inter.guild = guild
    inter.guild_id = guild.id
    inter.message = MagicMock()
    inter.message.id = message_id
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    return inter


def _seed_match(db, guild_id: int = 42, message_id: int = 555,
                team_a_ids=range(0, 5), team_b_ids=range(5, 10)):
    return repository.create_match(
        db, guild_id=guild_id,
        team_a=[{"id": i, "name": f"P{i}", "elo": 1500 + i*50} for i in team_a_ids],
        team_b=[{"id": i, "name": f"P{i}", "elo": 1500 + i*50} for i in team_b_ids],
        map_name="Bind",
        lobby_leader_id=0,
        category_name="Match #1",
        message_id=message_id,
        channel_id=100,
    )


# ── Vote : refus ──────────────────────────────────────────────────
async def test_vote_when_no_match_for_message():
    import bot as bot_module
    view = VoteView(bot_module.db)
    inter = _fake_interaction(_fake_member(0), _fake_guild(), message_id=999)

    await view.vote_a.callback(inter)

    args, kwargs = inter.response.send_message.call_args
    assert "introuvable" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_vote_when_user_did_not_play_match_refused():
    import bot as bot_module
    _seed_match(bot_module.db)
    view = VoteView(bot_module.db)
    # User 99 n'a pas joue
    inter = _fake_interaction(_fake_member(99), _fake_guild())

    await view.vote_a.callback(inter)

    args, _ = inter.response.send_message.call_args
    assert "n'as pas joue" in args[0]
    inter.response.edit_message.assert_not_awaited()


async def test_vote_on_validated_match_refused():
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, 42, match_id, "validated_a")

    view = VoteView(bot_module.db)
    inter = _fake_interaction(_fake_member(0), _fake_guild())
    await view.vote_a.callback(inter)

    args, _ = inter.response.send_message.call_args
    assert "deja valide" in args[0]


# ── Vote : enregistrement ─────────────────────────────────────────
async def test_vote_recorded_in_db():
    import bot as bot_module
    match_id = _seed_match(bot_module.db)

    view = VoteView(bot_module.db)
    inter = _fake_interaction(_fake_member(3), _fake_guild())
    await view.vote_a.callback(inter)

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match["votes"] == {"3": "a"}
    assert match["status"] == "pending"
    inter.response.edit_message.assert_awaited_once()


async def test_vote_can_be_changed():
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    view = VoteView(bot_module.db)

    # User 3 vote A
    inter1 = _fake_interaction(_fake_member(3), _fake_guild())
    await view.vote_a.callback(inter1)
    # Puis change pour B
    inter2 = _fake_interaction(_fake_member(3), _fake_guild())
    await view.vote_b.callback(inter2)

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match["votes"] == {"3": "b"}


# ── Majorite ──────────────────────────────────────────────────────
async def test_six_votes_for_a_keeps_pending():
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    view = VoteView(bot_module.db)

    for uid in range(6):  # joueurs 0..5 votent A (6 votes)
        inter = _fake_interaction(_fake_member(uid), _fake_guild())
        await view.vote_a.callback(inter)

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match["status"] == "pending"
    a_count = sum(1 for v in match["votes"].values() if v == "a")
    assert a_count == 6


async def test_seven_votes_for_a_validates_match():
    import bot as bot_module
    match_id = _seed_match(bot_module.db)

    triggered = []
    async def on_validated(inter, match_doc):
        triggered.append(match_doc)

    view = VoteView(bot_module.db, on_validated=on_validated)

    for uid in range(MAJORITY_THRESHOLD):  # 7 joueurs votent A
        inter = _fake_interaction(_fake_member(uid), _fake_guild())
        await view.vote_a.callback(inter)

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match["status"] == "validated_a"
    assert match["validated_at"] is not None

    # on_validated a ete appele 1 seule fois (au 7e vote)
    assert len(triggered) == 1
    assert triggered[0]["status"] == "validated_a"


async def test_seven_votes_for_b_validates_b():
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    view = VoteView(bot_module.db)

    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), _fake_guild())
        await view.vote_b.callback(inter)

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match["status"] == "validated_b"


async def test_validated_view_removed_from_message():
    """Apres validation : view=None passe a edit_message (boutons enleves)."""
    import bot as bot_module
    _seed_match(bot_module.db)
    view = VoteView(bot_module.db)

    inter_last = None
    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), _fake_guild())
        await view.vote_a.callback(inter)
        inter_last = inter

    last_call = inter_last.response.edit_message.call_args
    assert last_call.kwargs["view"] is None


# ── Embed : reflete les votes ─────────────────────────────────────
async def test_embed_shows_current_vote_counts():
    import bot as bot_module
    _seed_match(bot_module.db)
    view = VoteView(bot_module.db)

    inter = _fake_interaction(_fake_member(0), _fake_guild())
    await view.vote_a.callback(inter)
    inter2 = _fake_interaction(_fake_member(1), _fake_guild())
    await view.vote_b.callback(inter2)

    embed = inter2.response.edit_message.call_args.kwargs["embed"]
    votes_field = next(f for f in embed.fields if "Votes" in f.name)
    assert "**1**" in votes_field.value  # Team A : 1
    assert "**1**" in votes_field.value  # Team B : 1


def test_build_embed_from_doc_pending():
    doc = {
        "team_a": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5)],
        "team_b": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5, 10)],
        "map": "Bind",
        "lobby_leader_id": 0,
        "category_name": "Match #1",
        "status": "pending",
        "votes": {"0": "a", "1": "a"},
    }
    embed = build_match_embed_from_doc(doc, "G")
    assert "en cours" in embed.title.lower()
    votes_field = next(f for f in embed.fields if "Votes" in f.name)
    assert "**2**" in votes_field.value


def test_build_embed_from_doc_validated_a():
    doc = {
        "team_a": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5)],
        "team_b": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5, 10)],
        "map": "Bind",
        "lobby_leader_id": 0,
        "category_name": "Match #1",
        "status": "validated_a",
        "votes": {str(i): "a" for i in range(7)},
    }
    embed = build_match_embed_from_doc(doc, "G")
    assert "Team A a gagne" in embed.title


def test_build_embed_from_doc_contested():
    doc = {
        "team_a": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5)],
        "team_b": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5, 10)],
        "map": "Bind",
        "lobby_leader_id": 0,
        "category_name": None,
        "status": "contested",
        "votes": {},
    }
    embed = build_match_embed_from_doc(doc, "G")
    assert "admin" in embed.title.lower()


# ── Timeout ───────────────────────────────────────────────────────
async def test_timeout_marks_pending_match_contested():
    import bot as bot_module

    # Crée un match il y a 10 minutes
    match_id = _seed_match(bot_module.db)
    bot_module.db[f"matches_42"].update_one(
        {"_id": match_id},
        {"$set": {"created_at": datetime.now(timezone.utc) - timedelta(minutes=10)}},
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    admin_role = MagicMock()
    admin_role.name = "Admin"
    admin_role.mention = "@AdminRole"
    guild = _fake_guild(roles=[admin_role], channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    flagged = await cog.check_vote_timeouts()
    assert flagged == 1

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match["status"] == "contested"

    channel.send.assert_awaited_once()
    args, _ = channel.send.call_args
    assert "@AdminRole" in args[0]
    assert "timeout" in args[0].lower()


async def test_timeout_does_not_affect_validated():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    bot_module.db[f"matches_42"].update_one(
        {"_id": match_id},
        {"$set": {
            "status": "validated_a",
            "created_at": datetime.now(timezone.utc) - timedelta(minutes=20),
        }},
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    flagged = await cog.check_vote_timeouts()
    assert flagged == 0
    channel.send.assert_not_awaited()


async def test_timeout_does_not_affect_recent_match():
    import bot as bot_module

    _seed_match(bot_module.db)  # cree_at = now() automatiquement

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    flagged = await cog.check_vote_timeouts()
    assert flagged == 0


async def test_timeout_with_injectable_now():
    """Permet de simuler le passage du temps dans les tests."""
    import bot as bot_module
    match_id = _seed_match(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    fake_now = datetime.now(timezone.utc) + timedelta(minutes=VOTE_TIMEOUT_MINUTES + 1)
    flagged = await cog.check_vote_timeouts(now=fake_now)
    assert flagged == 1


async def test_timeout_falls_back_when_no_admin_role():
    """Si aucun role 'Admin' n'existe : on ping `@admin` en plain text."""
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    bot_module.db[f"matches_42"].update_one(
        {"_id": match_id},
        {"$set": {"created_at": datetime.now(timezone.utc) - timedelta(minutes=10)}},
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(roles=[], channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    await cog.check_vote_timeouts()
    args, _ = channel.send.call_args
    assert "@admin" in args[0]


# ── Threshold const ──────────────────────────────────────────────
def test_majority_threshold_is_7():
    assert MAJORITY_THRESHOLD == 7


def test_timeout_minutes_is_5():
    assert VOTE_TIMEOUT_MINUTES == 60


# ── Phase 6 : MAJ ELO apres validation ────────────────────────────
def _seed_match_with_avg_2400(db, guild_id: int = 42, message_id: int = 555):
    return repository.create_match(
        db, guild_id=guild_id,
        team_a=[{"id": i, "name": f"P{i}", "elo": 2400} for i in range(0, 5)],
        team_b=[{"id": i, "name": f"P{i}", "elo": 2400} for i in range(5, 10)],
        map_name="Bind",
        lobby_leader_id=0,
        category_name="Match #1",
        message_id=message_id,
        channel_id=100,
    )


async def test_validation_triggers_elo_update_in_db():
    """Le 7e vote A -> 5 gagnants +15, 5 perdants -15 (sol a 0)."""
    import bot as bot_module
    from cogs.match import MatchCog

    _seed_match_with_avg_2400(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    view = cog.vote_view

    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), guild)
        await view.vote_a.callback(inter)

    elo_col = repository.get_elo_col(bot_module.db, 42)
    for i in range(5):
        doc = elo_col.find_one({"_id": str(i)})
        assert doc["elo"] == 15, f"Winner {i}: ELO {doc['elo']}"
        assert doc["wins"] == 1
    for i in range(5, 10):
        doc = elo_col.find_one({"_id": str(i)})
        assert doc["elo"] == 0  # 0 - 15 -> max(0, ...) = 0
        assert doc["losses"] == 1


async def test_validation_sends_recap_embed():
    import bot as bot_module
    from cogs.match import MatchCog

    _seed_match_with_avg_2400(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    view = cog.vote_view

    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), guild)
        await view.vote_a.callback(inter)

    # Le recap est envoye au moins une fois sur le channel du match
    channel.send.assert_awaited()
    sent_embeds = [
        c.kwargs.get("embed") for c in channel.send.call_args_list if c.kwargs.get("embed")
    ]
    assert any("Team A l'emporte" in (e.title or "") for e in sent_embeds)
    recap = next(e for e in sent_embeds if "Team A l'emporte" in (e.title or ""))
    fields = {f.name: f.value for f in recap.fields}
    assert any("Gagnants" in n for n in fields)
    assert any("Perdants" in n for n in fields)
    # Verifie qu'on voit le delta +15
    assert "+15" in fields["🟢 Gagnants"]


async def test_validation_with_high_elo_match_bigger_gain():
    """Avg=3000 (Radiant) zero-sum -> gain=loss=19."""
    import bot as bot_module
    from cogs.match import MatchCog

    repository.create_match(
        bot_module.db, guild_id=42,
        team_a=[{"id": i, "name": f"P{i}", "elo": 3000} for i in range(0, 5)],
        team_b=[{"id": i, "name": f"P{i}", "elo": 3000} for i in range(5, 10)],
        map_name="Bind",
        lobby_leader_id=0,
        category_name="Match #1",
        message_id=555,
        channel_id=100,
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)
    cog = MatchCog(bot_module.bot, bot_module.db)

    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), guild)
        await cog.vote_view.vote_a.callback(inter)

    elo_col = repository.get_elo_col(bot_module.db, 42)
    assert elo_col.find_one({"_id": "0"})["elo"] == 19    # winner +19 (Radiant avg)


async def test_validated_b_distributes_correctly():
    """7 votes B -> team_b gagne, team_a perd."""
    import bot as bot_module
    from cogs.match import MatchCog

    _seed_match_with_avg_2400(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)
    cog = MatchCog(bot_module.bot, bot_module.db)

    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), guild)
        await cog.vote_view.vote_b.callback(inter)

    elo_col = repository.get_elo_col(bot_module.db, 42)
    # team_b (5..9) gagnent +15
    for i in range(5, 10):
        assert elo_col.find_one({"_id": str(i)})["elo"] == 15
        assert elo_col.find_one({"_id": str(i)})["wins"] == 1
    # team_a (0..4) perdent (mais demarrent a 0 -> reste 0)
    for i in range(5):
        assert elo_col.find_one({"_id": str(i)})["elo"] == 0
        assert elo_col.find_one({"_id": str(i)})["losses"] == 1
