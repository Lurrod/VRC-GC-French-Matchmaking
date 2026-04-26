"""Tests du cog riot_link : /link-riot, /unlink-riot, /refresh-elo."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.riot_api import (
    Account,
    CurrentMMR,
    HistoricalMatch,
    PlayerNotFound,
    RateLimited,
)
from cogs.riot_link import RiotLinkCog


def _fake_member(member_id: int, name: str = "TestUser"):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    return m


def _fake_interaction(user, guild_id: int = 42):
    inter = MagicMock()
    inter.user = user
    inter.guild_id = guild_id
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _fake_riot_client(*, account=None, mmr=None, history=None, raises=None):
    client = MagicMock()
    if raises:
        client.get_account.side_effect       = raises
        client.get_current_mmr.side_effect   = raises
        client.get_mmr_history.side_effect   = raises
    else:
        client.get_account.return_value     = account or Account(puuid="p1", name="X", tag="EUW", region="eu")
        client.get_current_mmr.return_value = mmr or CurrentMMR(elo=1500, tier=14, tier_name="Platinum 3", ranking_in_tier=50, mmr_change_last=0)
        client.get_mmr_history.return_value = history or []
    return client


def _now() -> datetime:
    return datetime(2026, 4, 25, tzinfo=timezone.utc)


# ── /link-riot ────────────────────────────────────────────────────
async def test_link_riot_invalid_format():
    import bot as bot_module
    cog = RiotLinkCog(bot_module.bot, bot_module.db, _fake_riot_client())

    user  = _fake_member(1)
    inter = _fake_interaction(user)

    await cog.link_riot.callback(cog, inter, riot_id="no-tag-here")

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert "Format invalide" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_link_riot_player_not_found():
    import bot as bot_module
    client = _fake_riot_client(raises=PlayerNotFound("nope"))
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)

    user  = _fake_member(1)
    inter = _fake_interaction(user)

    await cog.link_riot.callback(cog, inter, riot_id="Ghost#404")

    inter.response.defer.assert_awaited_once()
    inter.followup.send.assert_awaited_once()
    args, kwargs = inter.followup.send.call_args
    assert "introuvable" in args[0]


async def test_link_riot_rate_limited():
    import bot as bot_module
    cog = RiotLinkCog(bot_module.bot, bot_module.db, _fake_riot_client(raises=RateLimited()))

    inter = _fake_interaction(_fake_member(1))
    await cog.link_riot.callback(cog, inter, riot_id="X#1")

    args, _ = inter.followup.send.call_args
    assert "rate-limited" in args[0]


async def test_link_riot_success_persists_and_uses_peak_6m():
    import bot as bot_module
    # Peak recent Immortal 1+ (>= 2400) requis sur ce serveur
    history = [
        HistoricalMatch(elo=2500, tier=24, date=_now() - timedelta(days=10), mmr_change=15),
        HistoricalMatch(elo=2450, tier=24, date=_now() - timedelta(days=20), mmr_change=-10),
    ]
    client = _fake_riot_client(
        account=Account(puuid="abc", name="Player", tag="EUW", region="eu"),
        mmr=CurrentMMR(elo=2450, tier=24, tier_name="Immortal 1", ranking_in_tier=50, mmr_change_last=0),
        history=history,
    )
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    user  = _fake_member(1, "Jet")
    inter = _fake_interaction(user, guild_id=42)

    await cog.link_riot.callback(cog, inter, riot_id="Player#EUW")

    # Verifie metadata Riot persistee
    from services import repository
    doc = repository.get_riot_account(bot_module.db, 42, 1)
    assert doc is not None
    assert doc["riot_name"]   == "Player"
    assert doc["riot_tag"]    == "EUW"
    assert doc["riot_region"] == "eu"
    assert doc["puuid"]       == "abc"
    assert doc["peak_elo"]    == 2500
    assert doc["source"]      == "peak_6m"

    # ELO serveur seedee dans elo_<guild>
    elo_doc = repository.get_elo_col(bot_module.db, 42).find_one({"_id": "1"})
    assert elo_doc["elo"]          == 2500   # peak recent
    assert elo_doc["linked_once"]  is True

    inter.followup.send.assert_awaited_once()
    embed = inter.followup.send.call_args.kwargs["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Riot ID"] == "**Player#EUW**"
    assert "2500" in fields["ELO serveur"]


async def test_link_riot_accepts_any_rank():
    """Aucun gate-keeping rang : meme un Iron peut lier son compte."""
    import bot as bot_module
    history = [
        HistoricalMatch(elo=300, tier=3, date=_now() - timedelta(days=10), mmr_change=15),
    ]
    client = _fake_riot_client(
        account=Account(puuid="abc", name="Iron", tag="EUW", region="eu"),
        mmr=CurrentMMR(elo=300, tier=3, tier_name="Iron 3", ranking_in_tier=0, mmr_change_last=0),
        history=history,
    )
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    inter = _fake_interaction(_fake_member(1, "IronPlayer"), guild_id=42)

    await cog.link_riot.callback(cog, inter, riot_id="Iron#EUW")

    from services import repository
    assert repository.get_riot_account(bot_module.db, 42, 1) is not None
    elo_doc = repository.get_elo_col(bot_module.db, 42).find_one({"_id": "1"})
    assert elo_doc["elo"] == 300


async def test_link_riot_old_peak_ignored_uses_recent_peak():
    """Un peak vieux >6 mois est ignore : seul le peak des 6 derniers mois compte."""
    import bot as bot_module
    history = [
        HistoricalMatch(elo=2600, tier=25, date=_now() - timedelta(days=400), mmr_change=15),  # ignore (>6 mois)
        HistoricalMatch(elo=2400, tier=24, date=_now() - timedelta(days=10),  mmr_change=-10),
        HistoricalMatch(elo=2500, tier=24, date=_now() - timedelta(days=20),  mmr_change=10),  # peak recent
        HistoricalMatch(elo=2450, tier=24, date=_now() - timedelta(days=30),  mmr_change=20),
    ]
    client = _fake_riot_client(history=history)
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    inter = _fake_interaction(_fake_member(1), guild_id=42)

    await cog.link_riot.callback(cog, inter, riot_id="Player#EUW")

    from services import repository
    doc = repository.get_riot_account(bot_module.db, 42, 1)
    assert doc["peak_elo"] == 2500    # peak des 6 derniers mois, pas 2600
    assert doc["source"]   == "peak_6m"
    elo_doc = repository.get_elo_col(bot_module.db, 42).find_one({"_id": "1"})
    assert elo_doc["elo"] == 2500


async def test_link_riot_adds_prior_bot_elo():
    """Si le joueur a deja une ELO bot (matches avant link), elle s'ajoute a l'effective ELO Riot."""
    import bot as bot_module
    from services import repository

    # Seed : 200 ELO bot accumulees avant le link
    repository.get_elo_col(bot_module.db, 42).insert_one({
        "_id": "1", "name": "Jet", "elo": 200, "wins": 5, "losses": 2,
    })

    history = [
        HistoricalMatch(elo=2500, tier=24, date=_now() - timedelta(days=10), mmr_change=15),
        HistoricalMatch(elo=2450, tier=24, date=_now() - timedelta(days=20), mmr_change=-10),
    ]
    client = _fake_riot_client(
        account=Account(puuid="abc", name="Player", tag="EUW", region="eu"),
        mmr=CurrentMMR(elo=2450, tier=24, tier_name="Immortal 1", ranking_in_tier=50, mmr_change_last=0),
        history=history,
    )
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    inter = _fake_interaction(_fake_member(1, "Jet"), guild_id=42)

    await cog.link_riot.callback(cog, inter, riot_id="Player#EUW")

    doc = repository.get_riot_account(bot_module.db, 42, 1)
    assert doc is not None
    assert doc["peak_elo"] == 2500

    # ELO serveur = 200 (avant link) + 2500 (peak Riot)
    elo_doc = repository.get_elo_col(bot_module.db, 42).find_one({"_id": "1"})
    assert elo_doc["elo"]         == 2700
    assert elo_doc["linked_once"] is True
    assert elo_doc["wins"]        == 5     # stats preservees
    assert elo_doc["losses"]      == 2

    embed = inter.followup.send.call_args.kwargs["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert "2700" in fields["ELO serveur"]


async def test_link_riot_no_prior_bot_elo_seeds_riot_value():
    """Sans ELO bot prealable, l'ELO serveur seedee = peak Riot."""
    import bot as bot_module
    from services import repository

    # Aucune entree dans elo_<guild_id>
    history = [
        HistoricalMatch(elo=2500, tier=24, date=_now() - timedelta(days=10), mmr_change=15),
    ]
    client = _fake_riot_client(history=history)
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    inter = _fake_interaction(_fake_member(1, "Jet"), guild_id=42)

    await cog.link_riot.callback(cog, inter, riot_id="Player#EUW")

    elo_doc = repository.get_elo_col(bot_module.db, 42).find_one({"_id": "1"})
    assert elo_doc["elo"]         == 2500
    assert elo_doc["wins"]        == 0
    assert elo_doc["losses"]      == 0
    assert elo_doc["linked_once"] is True


async def test_link_riot_seed_is_idempotent_after_unlink_relink():
    """Apres /link, /unlink, /link, l'ELO de depart n'est ajoutee qu'une seule fois."""
    import bot as bot_module
    from services import repository

    # Seed initial bot ELO
    repository.get_elo_col(bot_module.db, 42).insert_one({
        "_id": "1", "name": "Jet", "elo": 200, "wins": 5, "losses": 2,
    })

    history = [HistoricalMatch(elo=2500, tier=24, date=_now() - timedelta(days=10), mmr_change=15)]
    client = _fake_riot_client(history=history)
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    inter = _fake_interaction(_fake_member(1, "Jet"), guild_id=42)

    # Premier link : 200 (existant) + 2500 (Riot) = 2700
    await cog.link_riot.callback(cog, inter, riot_id="Player#EUW")
    elo_doc = repository.get_elo_col(bot_module.db, 42).find_one({"_id": "1"})
    assert elo_doc["elo"]         == 2700
    assert elo_doc["linked_once"] is True

    # Unlink (supprime metadata Riot, ne touche pas elo_<guild>)
    await cog.unlink_riot.callback(cog, _fake_interaction(_fake_member(1), guild_id=42))
    elo_doc = repository.get_elo_col(bot_module.db, 42).find_one({"_id": "1"})
    assert elo_doc["elo"] == 2700  # ELO preservee

    # Re-link : seed inactif, ELO inchangee
    inter2 = _fake_interaction(_fake_member(1, "Jet"), guild_id=42)
    await cog.link_riot.callback(cog, inter2, riot_id="Player#EUW")
    elo_doc = repository.get_elo_col(bot_module.db, 42).find_one({"_id": "1"})
    assert elo_doc["elo"] == 2700  # toujours 2700, pas 2700+2500

    # L'embed signale le no-op
    embed = inter2.followup.send.call_args.kwargs["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert "ℹ️ Note" in fields


async def test_link_riot_empty_history_uses_current_mmr_fallback():
    import bot as bot_module
    # Peak/MMR Immortal+ pour passer le check
    client = _fake_riot_client(
        mmr=CurrentMMR(elo=2450, tier=24, tier_name="Immortal 1", ranking_in_tier=50, mmr_change_last=0),
        history=[],
    )
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    inter = _fake_interaction(_fake_member(1), guild_id=42)

    await cog.link_riot.callback(cog, inter, riot_id="X#1")

    from services import repository
    doc = repository.get_riot_account(bot_module.db, 42, 1)
    assert doc["source"] == "empty"
    elo_doc = repository.get_elo_col(bot_module.db, 42).find_one({"_id": "1"})
    assert elo_doc["elo"] == 2450     # fallback = mmr courant


# ── /unlink-riot ──────────────────────────────────────────────────
async def test_unlink_riot_when_linked():
    import bot as bot_module
    from services import repository
    repository.link_riot_account(
        bot_module.db, guild_id=42, user_id=1,
        riot_name="X", riot_tag="1", riot_region="eu",
        puuid="abc", peak_elo=1500, source="peak_recent",
    )

    cog = RiotLinkCog(bot_module.bot, bot_module.db, _fake_riot_client())
    inter = _fake_interaction(_fake_member(1), guild_id=42)
    await cog.unlink_riot.callback(cog, inter)

    args, _ = inter.response.send_message.call_args
    assert "delie" in args[0]
    assert repository.get_riot_account(bot_module.db, 42, 1) is None


async def test_unlink_riot_when_not_linked():
    import bot as bot_module
    cog = RiotLinkCog(bot_module.bot, bot_module.db, _fake_riot_client())
    inter = _fake_interaction(_fake_member(99), guild_id=42)
    await cog.unlink_riot.callback(cog, inter)

    args, _ = inter.response.send_message.call_args
    assert "Aucun" in args[0]
