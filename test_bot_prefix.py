"""
Tests d'integration des PREFIX commands (!leaderboard, !stats, !win, !lose).

Utilise dpytest pour simuler un environnement Discord complet :
  - Guild factice
  - Membres factices
  - Bot reel mais sans connexion Discord

Pour lancer :
    pip install -r requirements-test.txt
    pytest test_bot_prefix.py -v
"""

import discord
import discord.ext.test as dpytest
import pytest


# ── !leaderboard ──────────────────────────────────────────────────
async def test_leaderboard_empty_says_no_player(discord_bot, fake_guild):
    await dpytest.message("!leaderboard")
    assert dpytest.verify().message().content("Aucun joueur enregistre.")


async def test_leaderboard_shows_one_player(discord_bot, fake_guild):
    import bot as bot_module
    col = bot_module.get_elo_col(fake_guild.id)
    member = fake_guild.members[0]
    col.insert_one({
        "_id": str(member.id),
        "name": member.display_name,
        "elo": 100,
        "wins": 5,
        "losses": 2,
    })

    await dpytest.message("!leaderboard")
    msg = dpytest.get_message()
    assert msg.embeds, "Aucun embed dans la reponse"
    embed = msg.embeds[0]
    assert "Classement" in embed.title
    assert member.display_name in embed.description
    assert "100" in embed.description  # ELO


async def test_leaderboard_orders_by_elo_desc(discord_bot, fake_guild):
    import bot as bot_module
    col = bot_module.get_elo_col(fake_guild.id)

    members = fake_guild.members[:3]
    elos    = [50, 200, 100]
    for m, e in zip(members, elos):
        col.insert_one({"_id": str(m.id), "name": m.display_name, "elo": e, "wins": 0, "losses": 0})

    await dpytest.message("!leaderboard")
    embed = dpytest.get_message().embeds[0]
    desc  = embed.description

    # Le joueur a 200 doit apparaitre avant celui a 100, qui apparait avant celui a 50
    pos_200 = desc.find(members[1].display_name)
    pos_100 = desc.find(members[2].display_name)
    pos_50  = desc.find(members[0].display_name)
    assert pos_200 < pos_100 < pos_50, "L'ordre par ELO desc n'est pas respecte"


# ── !stats ────────────────────────────────────────────────────────
async def test_stats_for_unknown_player(discord_bot, fake_member):
    await dpytest.message(f"!stats")
    assert dpytest.verify().message().contains().content("n'a pas encore joue")


async def test_stats_shows_winrate(discord_bot, fake_guild, fake_member):
    import bot as bot_module
    col = bot_module.get_elo_col(fake_guild.id)
    col.insert_one({
        "_id": str(fake_member.id),
        "name": fake_member.display_name,
        "elo": 150,
        "wins": 7,
        "losses": 3,
    })

    await dpytest.message("!stats")
    embed = dpytest.get_message().embeds[0]
    fields = {f.name: f.value for f in embed.fields}
    assert "150" in fields.get("🏅 ELO", "")
    assert "7" in fields.get("✅ Victoires", "")
    assert "3" in fields.get("❌ Défaites", "")
    assert "70" in fields.get("📈 Winrate", "")  # 70%


# ── !win / !lose (necessitent permissions) ────────────────────────
async def test_win_refused_without_permission(discord_bot, fake_guild):
    # Par defaut le membre 0 dans dpytest n'a pas manage_guild
    target = fake_guild.members[1]
    await dpytest.message(f"!win {target.mention}")
    assert dpytest.verify().message().contains().content("Pas la permission")


async def test_win_grants_elo_with_admin(discord_bot, fake_guild):
    """Donne manage_guild au membre 0 et verifie le gain d'ELO."""
    import bot as bot_module

    # Octroi des permissions admin au membre 0 via un role
    admin = fake_guild.members[0]
    target = fake_guild.members[1]
    perms = discord.Permissions()
    perms.update(manage_guild=True)
    admin_role = await fake_guild.create_role(name="Admin", permissions=perms)
    await admin.add_roles(admin_role)

    await dpytest.message(f"!win {target.mention}")

    # Verifier en base
    col = bot_module.get_elo_col(fake_guild.id)
    doc = col.find_one({"_id": str(target.id)})
    assert doc is not None, "Le joueur n'a pas ete cree en base"
    assert doc["elo"] == 20, f"ELO attendu 20, recu {doc['elo']}"
    assert doc["wins"] == 1


async def test_lose_floors_elo_at_zero(discord_bot, fake_guild):
    """Un joueur a 5 ELO qui perd ne doit pas descendre sous 0."""
    import bot as bot_module

    admin = fake_guild.members[0]
    target = fake_guild.members[1]
    perms = discord.Permissions()
    perms.update(manage_guild=True)
    admin_role = await fake_guild.create_role(name="Admin", permissions=perms)
    await admin.add_roles(admin_role)

    col = bot_module.get_elo_col(fake_guild.id)
    col.insert_one({
        "_id": str(target.id),
        "name": target.display_name,
        "elo": 5,
        "wins": 0,
        "losses": 0,
    })

    await dpytest.message(f"!lose {target.mention}")

    doc = col.find_one({"_id": str(target.id)})
    assert doc["elo"] == 0, f"ELO doit etre clampe a 0, recu {doc['elo']}"
    assert doc["losses"] == 1
