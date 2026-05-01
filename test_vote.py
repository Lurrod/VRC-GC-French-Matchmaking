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
    g.get_member = lambda uid: None  # par defaut : leader/players non resolus
    if channel is not None:
        channel.name = "elo-adding"
        g.text_channels = [channel]
    else:
        g.text_channels = []
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
    assert "reportez le vainqueur" in embed.title.lower()
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

    # Crée un match expiré (au-delà du timeout)
    match_id = _seed_match(bot_module.db)
    bot_module.db[f"matches_42"].update_one(
        {"_id": match_id},
        {"$set": {"created_at": datetime.now(timezone.utc) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5)}},
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


async def test_timeout_self_heals_pending_with_majority_a():
    """Si un match `pending` expire mais a deja 7+ votes A (transition
    perdue suite a crash / erreur), check_vote_timeouts doit le passer
    en `validated_a` au lieu de `contested`."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    bot_module.db["matches_42"].update_one(
        {"_id": match_id},
        {"$set": {
            "created_at": datetime.now(timezone.utc) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5),
            "votes": {str(i): "a" for i in range(MAJORITY_THRESHOLD)},
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

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match["status"] == "validated_a"
    assert match["validated_at"] is not None
    channel.send.assert_not_awaited()


async def test_timeout_self_heals_pending_with_majority_b():
    """Symetrique : 7+ votes B -> validated_b."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    bot_module.db["matches_42"].update_one(
        {"_id": match_id},
        {"$set": {
            "created_at": datetime.now(timezone.utc) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5),
            "votes": {str(i): "b" for i in range(MAJORITY_THRESHOLD)},
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

    match = repository.get_match(bot_module.db, 42, match_id)
    assert match["status"] == "validated_b"
    channel.send.assert_not_awaited()


async def test_timeout_still_marks_contested_when_no_majority():
    """Garde-fou : si total >= 7 mais reparti (ex 4-3), on contested."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    split_votes = {**{str(i): "a" for i in range(4)},
                   **{str(i): "b" for i in range(4, 7)}}
    bot_module.db["matches_42"].update_one(
        {"_id": match_id},
        {"$set": {
            "created_at": datetime.now(timezone.utc) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5),
            "votes": split_votes,
        }},
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
        {"$set": {"created_at": datetime.now(timezone.utc) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5)}},
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


def _seed_db_elos(db, guild_id: int = 42, baseline: int = 2000) -> None:
    """Seed elo_col pour 10 joueurs : reflete la situation production ou
    chaque joueur a au moins LINK_BASE_ELO=2000 via /link-riot, evitant
    le plancher zero-sum qui neutraliserait les gains gagnants."""
    col = repository.get_elo_col(db, guild_id)
    for i in range(10):
        col.insert_one({
            "_id": str(i), "name": f"P{i}",
            "elo": baseline, "wins": 0, "losses": 0,
        })


async def _vote_and_verify(cog, guild, match_id, *, choice: str, db, guild_id: int = 42):
    """Helper : 7 votes pour `choice` puis applique ELO via _verify_match
    (henrik_client=None -> fallback ELO plat, comme apres 10 min sans Henrik)."""
    view = cog.vote_view
    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), guild)
        if choice == "a":
            await view.vote_a.callback(inter)
        else:
            await view.vote_b.callback(inter)
    match_doc = repository.get_match(db, guild_id, match_id)
    # force_apply=True simule le passage du timeout Henrik (ELO plat)
    await cog._verify_match(guild, match_doc, force_apply=True)


async def test_validation_triggers_elo_update_in_db():
    """Apres _verify_match (sans Henrik) : 5 gagnants +15, 5 perdants -15."""
    import bot as bot_module
    from cogs.match import MatchCog

    match_id = _seed_match_with_avg_2400(bot_module.db)
    _seed_db_elos(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    await _vote_and_verify(cog, guild, match_id, choice="a", db=bot_module.db)

    elo_col = repository.get_elo_col(bot_module.db, 42)
    for i in range(5):
        doc = elo_col.find_one({"_id": str(i)})
        assert doc["elo"] == 2015, f"Winner {i}: ELO {doc['elo']}"  # 2000 + 15
        assert doc["wins"] == 1
    for i in range(5, 10):
        doc = elo_col.find_one({"_id": str(i)})
        assert doc["elo"] == 1985  # 2000 - 15
        assert doc["losses"] == 1


async def test_validation_sends_recap_embed():
    import bot as bot_module
    from cogs.match import MatchCog

    match_id = _seed_match_with_avg_2400(bot_module.db)
    _seed_db_elos(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    await _vote_and_verify(cog, guild, match_id, choice="a", db=bot_module.db)

    channel.send.assert_awaited()
    sent_embeds = [
        c.kwargs.get("embed") for c in channel.send.call_args_list if c.kwargs.get("embed")
    ]
    assert any("Team A l'emporte" in (e.title or "") for e in sent_embeds)
    recap = next(e for e in sent_embeds if "Team A l'emporte" in (e.title or ""))
    fields = {f.name: f.value for f in recap.fields}
    assert any("Gagnants" in n for n in fields)
    assert any("Perdants" in n for n in fields)
    assert "+15" in fields["🟢 Gagnants"]


async def test_validation_with_high_elo_match_bigger_gain():
    """Avg=3000 (Radiant) zero-sum -> gain=loss=19."""
    import bot as bot_module
    from cogs.match import MatchCog

    match_id = repository.create_match(
        bot_module.db, guild_id=42,
        team_a=[{"id": i, "name": f"P{i}", "elo": 3000} for i in range(0, 5)],
        team_b=[{"id": i, "name": f"P{i}", "elo": 3000} for i in range(5, 10)],
        map_name="Bind",
        lobby_leader_id=0,
        category_name="Match #1",
        message_id=555,
        channel_id=100,
    )
    _seed_db_elos(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)
    cog = MatchCog(bot_module.bot, bot_module.db)
    await _vote_and_verify(cog, guild, match_id, choice="a", db=bot_module.db)

    elo_col = repository.get_elo_col(bot_module.db, 42)
    assert elo_col.find_one({"_id": "0"})["elo"] == 2019    # 2000 + 19 (Radiant avg)


async def test_validated_b_distributes_correctly():
    """7 votes B -> team_b gagne, team_a perd (apres _verify_match)."""
    import bot as bot_module
    from cogs.match import MatchCog

    match_id = _seed_match_with_avg_2400(bot_module.db)
    _seed_db_elos(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)
    cog = MatchCog(bot_module.bot, bot_module.db)
    await _vote_and_verify(cog, guild, match_id, choice="b", db=bot_module.db)

    elo_col = repository.get_elo_col(bot_module.db, 42)
    # team_b (5..9) gagnent +15 -> 2015
    for i in range(5, 10):
        assert elo_col.find_one({"_id": str(i)})["elo"] == 2015
        assert elo_col.find_one({"_id": str(i)})["wins"] == 1
    # team_a (0..4) perdent -15 -> 1985
    for i in range(5):
        assert elo_col.find_one({"_id": str(i)})["elo"] == 1985
        assert elo_col.find_one({"_id": str(i)})["losses"] == 1


async def test_vote_validation_does_not_touch_elo():
    """Garde-fou : le vote seul ne touche plus a l'ELO ; il faut _verify_match."""
    import bot as bot_module
    from cogs.match import MatchCog

    _seed_match_with_avg_2400(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)
    cog = MatchCog(bot_module.bot, bot_module.db)

    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), guild)
        await cog.vote_view.vote_a.callback(inter)

    elo_col = repository.get_elo_col(bot_module.db, 42)
    # Aucun doc ELO cree : l'ELO sera applique uniquement par _verify_match.
    for i in range(10):
        assert elo_col.find_one({"_id": str(i)}) is None


# ── Atomicite : transition_match_status (fix audit #2) ────────────
def test_transition_match_status_succeeds_from_pending():
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    res = repository.transition_match_status(
        bot_module.db, 42, match_id,
        from_status="pending", to_status="validated_a",
    )
    assert res is not None
    assert res["status"] == "validated_a"
    assert res["validated_at"] is not None


def test_transition_match_status_fails_when_already_validated():
    """Garantie d'atomicite : si un autre vote concurrent a deja valide,
    une seconde transition ne reussit pas (renvoie None)."""
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, 42, match_id, "validated_a")

    res = repository.transition_match_status(
        bot_module.db, 42, match_id,
        from_status="pending", to_status="validated_b",
    )
    assert res is None


async def test_concurrent_votes_only_fire_on_validated_once():
    """Deux votes votant simultanement pour des camps opposes au seuil
    ne doivent declencher `on_validated` qu'une seule fois."""
    import bot as bot_module
    _seed_match(bot_module.db)

    fired = []
    async def on_validated(inter, match_doc):
        fired.append(match_doc.get("status"))

    view = VoteView(bot_module.db, on_validated=on_validated)
    guild = _fake_guild()

    # 6 votes 'a', 6 votes 'b' (10 joueurs, vote modifiable pas necessaire ici).
    # On atteint la majorite via 7 votes 'a' d'abord ; un 8e vote arrive ensuite
    # pour 'b' alors que le match est deja valide -> ne doit pas re-tirer.
    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), guild)
        await view.vote_a.callback(inter)

    # Vote tardif pour 'b' (le match est deja validated_a)
    inter = _fake_interaction(_fake_member(7), guild)
    await view.vote_b.callback(inter)

    assert fired == ["validated_a"]


# ── Idempotence ELO : claim_match_for_elo (fix audit #3) ──────────
def test_claim_match_for_elo_succeeds_first_time():
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, 42, match_id, "validated_a")

    claim = repository.claim_match_for_elo(bot_module.db, 42, match_id)
    assert claim is not None
    assert claim["elo_applied"] is True


def test_claim_match_for_elo_returns_none_when_already_claimed():
    """Empeche la double-application d'ELO : seul le premier claim passe."""
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, 42, match_id, "validated_a")

    first = repository.claim_match_for_elo(bot_module.db, 42, match_id)
    second = repository.claim_match_for_elo(bot_module.db, 42, match_id)
    assert first is not None
    assert second is None


def test_claim_match_for_elo_rejects_non_validated_match():
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    # Status reste 'pending', pas de claim possible
    claim = repository.claim_match_for_elo(bot_module.db, 42, match_id)
    assert claim is None


def test_release_elo_claim_allows_retry():
    """Si l'application ELO leve, on relache le claim pour re-essayer."""
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, 42, match_id, "validated_a")

    repository.claim_match_for_elo(bot_module.db, 42, match_id)
    repository.release_elo_claim(bot_module.db, 42, match_id)
    retry = repository.claim_match_for_elo(bot_module.db, 42, match_id)
    assert retry is not None


def test_find_validated_unverified_excludes_elo_applied():
    """Un match dont l'ELO est deja applique ne doit pas re-apparaitre dans
    la queue de verification (eviter le double credit)."""
    import bot as bot_module
    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, 42, match_id, "validated_a")
    repository.claim_match_for_elo(bot_module.db, 42, match_id)

    cutoff = datetime.now(timezone.utc) + timedelta(minutes=1)
    matches = repository.find_validated_unverified(bot_module.db, 42, cutoff)
    assert all(m["_id"] != match_id for m in matches)
