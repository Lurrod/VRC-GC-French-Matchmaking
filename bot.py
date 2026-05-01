import discord
from discord.ext import commands
from discord import app_commands
import logging
import os
import sys

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
import random
from pymongo import MongoClient
from pymongo.collection import Collection

from services import elo_calc, repository
from services.riot_api import HenrikDevClient
from services.leaderboard_refresh import (
    build_leaderboard_payload,
    refresh_leaderboard_channel,
)

# ── Charge .env si present (sans planter si python-dotenv absent) ──
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Configuration ──────────────────────────────────────────────
TOKEN     = os.environ.get("DISCORD_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")

ELO_START = elo_calc.ELO_START
MAPS      = list(elo_calc.MAPS)

# ── MongoDB ────────────────────────────────────────────────────
# retryWrites/retryReads sont True par defaut depuis pymongo 4.x mais on les
# explicite pour resilience aux blips reseau. serverSelectionTimeoutMS=5000
# evite de bloquer >30s sur Mongo down -> Discord renvoie "L'application n'a
# pas repondu". connectTimeoutMS=5000 limite le handshake initial.
client = MongoClient(
    MONGO_URL,
    tz_aware=True,
    tzinfo=timezone.utc,
    retryWrites=True,
    retryReads=True,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db     = client["elobot"]

def get_elo_col(guild_id: int | str) -> Collection:
    return repository.get_elo_col(db, guild_id)

def get_bypass_col() -> Collection:
    return repository.get_bypass_col(db)

def get_player(col, member: discord.Member):
    return repository.get_or_create_player(
        col, member.id, member.display_name, initial_elo=ELO_START,
    )

def get_bypass_role(guild_id):
    return repository.get_bypass_role(db, guild_id)

def set_bypass_role(guild_id, role_id):
    repository.set_bypass_role(db, guild_id, role_id)

def has_access(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = get_bypass_role(interaction.guild_id)
    if role_id and any(r.id == role_id for r in interaction.user.roles):
        return True
    return False

# ── Bot ────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── /setup ─────────────────────────────────────────────────────
SETUP_CATEGORY_NAME = "🎮 Valorant 10mans"
SETUP_CHANNELS = ["leaderboard", "queue", "matchs"]


@tree.command(name="setup", description="Crée la catégorie et les salons necessaires au bot")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_bot(interaction: discord.Interaction):
    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)

    # 1) Categorie
    category = discord.utils.get(guild.categories, name=SETUP_CATEGORY_NAME)
    if category is None:
        try:
            category = await guild.create_category(SETUP_CATEGORY_NAME)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Le bot n'a pas la permission **Gérer les salons**.",
                ephemeral=True,
            )
            return

    # 2) Salons
    created: list[str] = []
    existed: list[str] = []
    for name in SETUP_CHANNELS:
        chan = discord.utils.get(guild.text_channels, name=name)
        if chan is None:
            try:
                await guild.create_text_channel(name, category=category)
                created.append(name)
            except discord.Forbidden:
                await interaction.followup.send(
                    f"❌ Impossible de créer `#{name}` (permissions manquantes).",
                    ephemeral=True,
                )
                return
        else:
            existed.append(name)

    # 3) Pose le message de queue dans #queue (idempotent)
    from cogs.queue_v2 import build_queue_embed
    queue_chan = discord.utils.get(guild.text_channels, name="queue")
    queue_cog  = bot.get_cog("QueueCog")
    queue_status = ""
    if queue_chan and queue_cog is not None:
        repository.delete_active_queue(db, guild.id)
        try:
            embed = build_queue_embed(None, guild)
            msg = await queue_chan.send(embed=embed, view=queue_cog.view)
            repository.setup_active_queue(
                db, guild_id=guild.id, channel_id=queue_chan.id, message_id=msg.id,
            )
            queue_status = f"🎯 Message de queue posté dans {queue_chan.mention}"
        except discord.Forbidden:
            queue_status = f"⚠️ Impossible d'envoyer dans {queue_chan.mention} (permissions)"

    # 4) Recap
    lines: list[str] = []
    if created:
        lines.append(f"✅ Créés : {', '.join(f'`#{c}`' for c in created)}")
    if existed:
        lines.append(f"ℹ️ Déjà présents : {', '.join(f'`#{c}`' for c in existed)}")
    if queue_status:
        lines.append(queue_status)
    if not lines:
        lines.append("✅ Setup terminé.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@setup_bot.error
async def _setup_perm_error(inter: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await inter.response.send_message(
            "🚫 Reservé aux administrateurs.", ephemeral=True,
        )


# ── /bypass ────────────────────────────────────────────────────
@tree.command(name="bypass", description="Donne acces a toutes les commandes du bot a un role")
@app_commands.describe(role="Le role qui aura acces a toutes les commandes")
@app_commands.checks.has_permissions(manage_guild=True)
async def bypass(interaction: discord.Interaction, role: discord.Role):
    if role.id == interaction.guild_id or role.is_default():
        await interaction.response.send_message(
            "❌ Impossible d'accorder le bypass a @everyone — cela donnerait l'acces admin a tout le serveur.",
            ephemeral=True,
        )
        return
    if role.managed:
        await interaction.response.send_message(
            "❌ Impossible d'accorder le bypass a un role gere par une integration (bot, booster, etc.).",
            ephemeral=True,
        )
        return
    set_bypass_role(interaction.guild_id, role.id)
    embed = discord.Embed(
        title="🔓 Bypass activé !",
        description=f"Le role {role.mention} a maintenant acces a toutes les commandes du bot.",
        color=0xe67e22,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Configuré par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── Helper V2 : ELO serveur avec fallback ──────────────────────
def _match_elo_for_member(guild_id: int, user_id: int) -> int:
    """Renvoie l'ELO serveur du joueur (elo_<guild>.elo), ou ELO_REFERENCE si absente."""
    doc = repository.get_elo_col(db, guild_id).find_one({"_id": str(user_id)})
    if doc and doc.get("elo") is not None:
        return int(doc["elo"])
    return elo_calc.ELO_REFERENCE


def _compute_match_change_for_members(guild_id: int, members: list) -> tuple[int, int, int]:
    """Renvoie (avg_elo, gain, loss) pour la liste de joueurs passee."""
    elos = [_match_elo_for_member(guild_id, m.id) for m in members]
    avg  = round(sum(elos) / len(elos)) if elos else elo_calc.ELO_REFERENCE
    gain, loss = elo_calc.compute_match_elo_change(avg)
    return avg, gain, loss


# ── /win ───────────────────────────────────────────────────────
@tree.command(name="win", description="Enregistre une victoire (gain V2 proportionnel a la moyenne d'ELO)")
@app_commands.describe(
    joueur1="Joueur gagnant 1",
    joueur2="Joueur gagnant 2",
    joueur3="Joueur gagnant 3",
    joueur4="Joueur gagnant 4",
    joueur5="Joueur gagnant 5",
)
async def win(
    interaction: discord.Interaction,
    joueur1: discord.Member,
    joueur2: discord.Member = None,
    joueur3: discord.Member = None,
    joueur4: discord.Member = None,
    joueur5: discord.Member = None,
):
    if not has_access(interaction):
        await interaction.response.send_message("Pas la permission.", ephemeral=True)
        return
    players = [p for p in [joueur1, joueur2, joueur3, joueur4, joueur5] if p is not None]
    col = get_elo_col(interaction.guild_id)

    avg_elo, gain, _ = _compute_match_change_for_members(interaction.guild_id, players)

    embed = discord.Embed(
        title="Resultats - Victoire enregistree !",
        description=f"Avg ELO du groupe : **{avg_elo}** -> +**{gain}** ELO chacun",
        color=0x2ecc71,
        timestamp=datetime.now(timezone.utc),
    )
    for member in players:
        doc = get_player(col, member)
        old = doc["elo"]
        new = old + gain
        col.update_one({"_id": str(member.id)}, {"$set": {"elo": new}, "$inc": {"wins": 1}})
        embed.add_field(
            name=member.display_name,
            value=f"+{gain} ELO -> **{new}** *(était {old})*",
            inline=False,
        )
    embed.set_footer(text=f"Enregistre par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

# ── /lose ──────────────────────────────────────────────────────
@tree.command(name="lose", description="Enregistre une defaite (perte V2 proportionnelle a la moyenne d'ELO)")
@app_commands.describe(
    joueur1="Joueur perdant 1",
    joueur2="Joueur perdant 2",
    joueur3="Joueur perdant 3",
    joueur4="Joueur perdant 4",
    joueur5="Joueur perdant 5",
)
async def lose(
    interaction: discord.Interaction,
    joueur1: discord.Member,
    joueur2: discord.Member = None,
    joueur3: discord.Member = None,
    joueur4: discord.Member = None,
    joueur5: discord.Member = None,
):
    if not has_access(interaction):
        await interaction.response.send_message("Pas la permission.", ephemeral=True)
        return
    players = [p for p in [joueur1, joueur2, joueur3, joueur4, joueur5] if p is not None]
    col = get_elo_col(interaction.guild_id)

    avg_elo, _, loss = _compute_match_change_for_members(interaction.guild_id, players)

    embed = discord.Embed(
        title="Resultats - Defaite enregistree !",
        description=f"Avg ELO du groupe : **{avg_elo}** -> -**{loss}** ELO chacun",
        color=0xe74c3c,
        timestamp=datetime.now(timezone.utc),
    )
    for member in players:
        doc = get_player(col, member)
        old = doc["elo"]
        new = max(0, old - loss)
        col.update_one({"_id": str(member.id)}, {"$set": {"elo": new}, "$inc": {"losses": 1}})
        embed.add_field(
            name=member.display_name,
            value=f"-{loss} ELO -> **{new}** (etait {old})",
            inline=False,
        )
    embed.set_footer(text=f"Enregistre par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

# ── /kd ────────────────────────────────────────────────────────
# ── /map ───────────────────────────────────────────────────────
@tree.command(name="map", description="Sélectionne une map aléatoire pour la partie")
async def map_pick(interaction: discord.Interaction):
    if not has_access(interaction):
        await interaction.response.send_message("🚫 Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True)
        return
    chosen = random.choice(MAPS)
    embed = discord.Embed(title="🗺️ Map sélectionnée !", description=f"## {chosen}", color=0x9b59b6, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Tirage par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

@tree.command(name="coinflip", description="Fait un pile ou face")
async def coinflip(interaction: discord.Interaction):
    result = random.choice(["Pile", "Face"])
    embed  = discord.Embed(title="🪙 Pile ou Face !", description=f"## {result}", color=0xf1c40f, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Lancé par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


# ── Refresh leaderboard helper ─────────────────────────────────
async def _refresh_leaderboard_safe(guild: discord.Guild | None) -> None:
    """Rafraichit le leaderboard du salon `#leaderboard` apres une
    modification d'ELO. Silencieux si le bot n'est pas pret ou si la guild
    n'a pas de salon dedie."""
    if guild is None or bot.user is None:
        return
    try:
        await refresh_leaderboard_channel(guild, db, bot.user.id)
    except Exception as e:
        logger.exception("[leaderboard] refresh a leve")


# ── /leaderboard ───────────────────────────────────────────────
def _is_leaderboard_channel(interaction: discord.Interaction) -> bool:
    chan = interaction.channel
    name = getattr(chan, "name", "") or ""
    return "leaderboard" in name.lower()


@tree.command(name="leaderboard", description="Affiche le classement ELO du serveur")
async def leaderboard(interaction: discord.Interaction):
    public = _is_leaderboard_channel(interaction)
    ephemeral = not public
    await interaction.response.defer(ephemeral=ephemeral)
    file, view = await build_leaderboard_payload(interaction.guild, db)
    if file is None:
        await interaction.followup.send("Aucun joueur enregistre.", ephemeral=True)
        return
    await interaction.followup.send(file=file, view=view, ephemeral=ephemeral)

# ── /resetelo ──────────────────────────────────────────────────
@tree.command(name="resetelo", description="Remet l'ELO d'un joueur a 0")
@app_commands.describe(joueur="Le joueur a remettre a zero", all="Remettre l'ELO de TOUS les joueurs a 0")
async def resetelo(interaction: discord.Interaction, joueur: discord.Member = None, all: bool = False):
    if not has_access(interaction):
        await interaction.response.send_message("Pas la permission.", ephemeral=True)
        return
    col = get_elo_col(interaction.guild_id)
    if all:
        count = col.count_documents({})
        col.update_many({}, {"$set": {"elo": 0, "wins": 0, "losses": 0}})
        embed = discord.Embed(title="🔄 Reset général !", description=f"ELO de **{count} joueur(s)** remis a 0.", color=0xe74c3c, timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"Reset par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild)
        return
    if joueur is None:
        await interaction.response.send_message("Mentionne un joueur ou utilise all:True.", ephemeral=True)
        return
    doc = get_player(col, joueur)
    old = doc["elo"]
    col.update_one({"_id": str(joueur.id)}, {"$set": {"elo": 0, "wins": 0, "losses": 0}})
    embed = discord.Embed(title="🔄 ELO réinitialisé !", color=0x95a5a6, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Joueur", value=joueur.mention, inline=True)
    embed.add_field(name="Ancien ELO", value=str(old), inline=True)
    embed.add_field(name="Nouvel ELO", value="0", inline=True)
    embed.set_thumbnail(url=joueur.display_avatar.url)
    embed.set_footer(text=f"Reset par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    await _refresh_leaderboard_safe(interaction.guild)

# ── /elomodify ─────────────────────────────────────────────────
@tree.command(name="elomodify", description="Ajoute ou enleve de l'ELO a un joueur")
@app_commands.describe(joueur="Le joueur", action="Ajouter ou enlever", montant="Nombre d'ELO")
@app_commands.choices(action=[
    app_commands.Choice(name="+ Ajouter", value="add"),
    app_commands.Choice(name="- Enlever", value="remove"),
])
async def elomodify(interaction: discord.Interaction, joueur: discord.Member, action: str, montant: int):
    if not has_access(interaction):
        await interaction.response.send_message("Pas la permission.", ephemeral=True)
        return
    if montant <= 0:
        await interaction.response.send_message(
            "❌ Le montant doit etre strictement positif. Utilise l'action `- Enlever` pour retirer de l'ELO.",
            ephemeral=True,
        )
        return
    col = get_elo_col(interaction.guild_id)
    doc = get_player(col, joueur)
    old = doc["elo"]
    if action == "add":
        new   = old + montant
        color = 0x2ecc71
        label = f"+{montant}"
        title = "➕ ELO ajouté"
    else:
        new   = max(0, old - montant)
        color = 0xe74c3c
        label = f"-{montant}"
        title = "➖ ELO retiré"
    col.update_one({"_id": str(joueur.id)}, {"$set": {"elo": new}})
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Joueur",       value=joueur.mention,                    inline=True)
    embed.add_field(name="Modification", value=label,                             inline=True)
    embed.add_field(name="Nouvel ELO",   value=f"**{new}** (etait {old})",        inline=True)
    embed.set_footer(text=f"Par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    await _refresh_leaderboard_safe(interaction.guild)

# ── /winmodify ─────────────────────────────────────────────────
@tree.command(name="winmodify", description="Ajoute ou enleve des victoires a un joueur")
@app_commands.describe(joueur="Le joueur", action="Ajouter ou enlever", montant="Nombre de victoires")
@app_commands.choices(action=[
    app_commands.Choice(name="+ Ajouter", value="add"),
    app_commands.Choice(name="- Enlever", value="remove"),
])
async def winmodify(interaction: discord.Interaction, joueur: discord.Member, action: str, montant: int):
    if not has_access(interaction):
        await interaction.response.send_message("Pas la permission.", ephemeral=True)
        return
    if montant <= 0:
        await interaction.response.send_message(
            "❌ Le montant doit etre strictement positif.",
            ephemeral=True,
        )
        return
    col = get_elo_col(interaction.guild_id)
    doc = get_player(col, joueur)
    old = doc.get("wins", 0)
    if action == "add":
        new   = old + montant
        color = 0x2ecc71
        label = f"+{montant}"
        title = "➕ Victoires ajoutées"
    else:
        new   = max(0, old - montant)
        color = 0xe74c3c
        label = f"-{montant}"
        title = "➖ Victoires retirées"
    col.update_one({"_id": str(joueur.id)}, {"$set": {"wins": new}})
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Joueur",        value=joueur.mention,             inline=True)
    embed.add_field(name="Modification",  value=label,                      inline=True)
    embed.add_field(name="Nouveau total", value=f"**{new}** (etait {old})", inline=True)
    embed.set_footer(text=f"Par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    await _refresh_leaderboard_safe(interaction.guild)

# ── /stats ─────────────────────────────────────────────────────

@tree.command(name="losemodify", description="Ajoute ou enleve des defaites a un joueur")
@app_commands.describe(joueur="Le joueur", action="Ajouter ou enlever", montant="Nombre de defaites")
@app_commands.choices(action=[
    app_commands.Choice(name="+ Ajouter", value="add"),
    app_commands.Choice(name="- Enlever", value="remove"),
])
async def losemodify(interaction: discord.Interaction, joueur: discord.Member, action: str, montant: int):
    if not has_access(interaction):
        await interaction.response.send_message("Pas la permission.", ephemeral=True)
        return
    if montant <= 0:
        await interaction.response.send_message(
            "❌ Le montant doit etre strictement positif.",
            ephemeral=True,
        )
        return
    col = get_elo_col(interaction.guild_id)
    doc = get_player(col, joueur)
    old = doc.get("losses", 0)
    if action == "add":
        new   = old + montant
        color = 0xe74c3c
        label = f"+{montant}"
        title = "➕ Défaites ajoutées"
    else:
        new   = max(0, old - montant)
        color = 0x2ecc71
        label = f"-{montant}"
        title = "➖ Défaites retirées"
    col.update_one({"_id": str(joueur.id)}, {"$set": {"losses": new}})
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Joueur", value=joueur.mention, inline=True)
    embed.add_field(name="Modification", value=label, inline=True)
    embed.add_field(name="Nouveau total", value=f"**{new}** (etait {old})", inline=True)
    embed.set_footer(text=f"Par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    await _refresh_leaderboard_safe(interaction.guild)

@tree.command(name="stats", description="Affiche les statistiques ELO d'un joueur")
@app_commands.describe(joueur="Le joueur dont tu veux voir les stats")
async def stats(interaction: discord.Interaction, joueur: discord.Member = None):
    if joueur is None:
        joueur = interaction.user
    col = get_elo_col(interaction.guild_id)
    doc = col.find_one({"_id": str(joueur.id)})
    if not doc:
        await interaction.response.send_message(f"{joueur.display_name} n'a pas encore joue.", ephemeral=True)
        return
    elo     = doc["elo"]
    wins    = doc.get("wins", 0)
    losses  = doc.get("losses", 0)
    total   = wins + losses
    winrate = round((wins / total) * 100, 1) if total > 0 else 0
    # Rang aligne avec le tri du leaderboard (ELO desc, wins desc, _id asc)
    # pour eviter qu'un /stats affiche un rang qui ne correspond pas a la
    # position dans le leaderboard sur les ex-aequo.
    rank    = col.count_documents({
        "$or": [
            {"elo": {"$gt": elo}},
            {"elo": elo, "wins": {"$gt": wins}},
            {"elo": elo, "wins": wins, "_id": {"$lt": str(joueur.id)}},
        ],
    }) + 1
    embed = discord.Embed(title=f"📊 Stats de {joueur.display_name}", color=0x3498db, timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=joueur.display_avatar.url)
    embed.add_field(name="🏅 ELO",       value=f"**{elo}**",            inline=True)
    embed.add_field(name="🏆 Rang",      value=f"**#{rank}**",          inline=True)
    embed.add_field(name="📈 Winrate",   value=f"**{winrate}%**",       inline=True)
    embed.add_field(name="✅ Victoires", value=f"**{wins}**",           inline=True)
    embed.add_field(name="❌ Défaites",  value=f"**{losses}**",         inline=True)
    embed.add_field(name="🎮 Parties",   value=f"**{total}**",          inline=True)
    embed.set_footer(text=interaction.guild.name)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── /clear ─────────────────────────────────────────────────────
@tree.command(name="clear", description="Supprime un nombre de messages dans le salon")
@app_commands.describe(nombre="Nombre de messages a supprimer (max 100)")
async def clear(interaction: discord.Interaction, nombre: int):
    if not has_access(interaction):
        await interaction.response.send_message("Pas la permission.", ephemeral=True)
        return
    if nombre < 1 or nombre > 100:
        await interaction.response.send_message("Le nombre doit etre entre 1 et 100.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=nombre)
    embed = discord.Embed(title="🗑️ Messages supprimés", description=f"**{len(deleted)}** message(s) supprime(s).", color=0xe74c3c, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Par {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── /help ─────────────────────────────────────────────────────
@tree.command(name="help", description="Affiche la liste des commandes disponibles")
@app_commands.describe(type="Choisis le type d'aide")
@app_commands.choices(type=[
    app_commands.Choice(name="Commandes membres", value="membres"),
    app_commands.Choice(name="Commandes admin", value="admin"),
])
async def help_cmd(interaction: discord.Interaction, type: str = "membres"):
    if type == "admin":
        if not has_access(interaction):
            await interaction.response.send_message("Pas la permission.", ephemeral=True)
            return
        embed = discord.Embed(title="⚙️ Commandes Admin", color=0xe74c3c, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="/setup",               value="Crée la catégorie et les salons (`leaderboard`, `queue`, `matchs`) et pose le message de queue", inline=False)
        embed.add_field(name="/win @j1..@j5",        value="Victoire — gain proportionnel à l'avg ELO Riot des joueurs (V2)", inline=False)
        embed.add_field(name="/lose @j1..@j5",       value="Défaite — perte proportionnelle à l'avg ELO Riot des joueurs (V2)", inline=False)
        embed.add_field(name="/kd @j kills morts...", value="Enregistre les kills/morts", inline=False)
        embed.add_field(name="/map",                 value="Map aleatoire", inline=False)
        embed.add_field(name="/elomodify @j action montant", value="Ajoute ou enleve de l'ELO", inline=False)
        embed.add_field(name="/winmodify @j action montant", value="Ajoute ou enleve des victoires", inline=False)
        embed.add_field(name="/resetelo @joueur",    value="Remet l'ELO a 0", inline=False)
        embed.add_field(name="/resetelo all:True",   value="Remet l'ELO de tout le monde a 0", inline=False)
        embed.add_field(name="/bypass @role",        value="Donne acces aux commandes admin a un role", inline=False)
        embed.add_field(name="/clear nombre",        value="Supprime des messages", inline=False)
        embed.set_footer(text=f"Demande par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        embed = discord.Embed(title="📖 Commandes disponibles", color=0x3498db, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="/leaderboard", value="Classement ELO du serveur", inline=False)
        embed.add_field(name="/stats @joueur", value="Stats d'un joueur. Sans mention = tes propres stats", inline=False)
        embed.add_field(name="/help", value="Affiche cette aide", inline=False)
        embed.set_footer(text=f"Demande par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ── Erreur bypass ──────────────────────────────────────────────
@bypass.error
async def bypass_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("Seuls les administrateurs peuvent configurer le bypass.", ephemeral=True)

# ── Commandes prefix ───────────────────────────────────────────
@bot.command(name="leaderboard")
async def leaderboard_prefix(ctx):
    col  = get_elo_col(ctx.guild.id)
    docs = list(col.find().sort([("elo", -1), ("wins", -1), ("_id", 1)]).limit(10))
    if not docs:
        await ctx.send("Aucun joueur enregistre.")
        return
    lines = []
    for i, doc in enumerate(docs):
        uid    = doc["_id"]
        member = ctx.guild.get_member(int(uid))
        if member is None:
            continue
        medal  = ["1er", "2e", "3e"][i] if i < 3 else f"#{i+1}"
        lines.append(f"{medal} **{doc.get('name', uid)}** - {doc['elo']} ELO (W:{doc.get('wins',0)} / L:{doc.get('losses',0)})")
    if not lines:
        await ctx.send("Aucun joueur enregistre.")
        return
    embed = discord.Embed(title="Classement ELO", description="\n".join(lines), color=0xf1c40f, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=ctx.guild.name)
    await ctx.send(embed=embed)

@bot.command(name="stats")
async def stats_prefix(ctx, member: discord.Member = None):
    if member is None:
        member = ctx.author
    col = get_elo_col(ctx.guild.id)
    doc = col.find_one({"_id": str(member.id)})
    if not doc:
        await ctx.send(f"{member.display_name} n'a pas encore joue.")
        return
    elo     = doc["elo"]
    wins    = doc.get("wins", 0)
    losses  = doc.get("losses", 0)
    total   = wins + losses
    winrate = round((wins / total) * 100, 1) if total > 0 else 0
    rank    = col.count_documents({
        "$or": [
            {"elo": {"$gt": elo}},
            {"elo": elo, "wins": {"$gt": wins}},
            {"elo": elo, "wins": wins, "_id": {"$lt": str(member.id)}},
        ],
    }) + 1
    embed = discord.Embed(title=f"Stats de {member.display_name}", color=0x3498db, timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🏅 ELO",       value=f"**{elo}**",            inline=True)
    embed.add_field(name="🏆 Rang",      value=f"**#{rank}**",          inline=True)
    embed.add_field(name="📈 Winrate",   value=f"**{winrate}%**",       inline=True)
    embed.add_field(name="✅ Victoires", value=f"**{wins}**",           inline=True)
    embed.add_field(name="❌ Défaites",  value=f"**{losses}**",         inline=True)
    embed.add_field(name="🎮 Parties",   value=f"**{total}**",          inline=True)
    embed.set_footer(text=ctx.guild.name)
    await ctx.send(embed=embed)

@bot.command(name="win")
async def win_prefix(ctx, joueur1: discord.Member, joueur2: discord.Member = None, joueur3: discord.Member = None, joueur4: discord.Member = None, joueur5: discord.Member = None):
    if not ctx.author.guild_permissions.manage_guild:
        role_id = get_bypass_role(ctx.guild.id)
        if not role_id or not any(r.id == role_id for r in ctx.author.roles):
            await ctx.send("Pas la permission.")
            return
    players = [p for p in [joueur1, joueur2, joueur3, joueur4, joueur5] if p is not None]
    col = get_elo_col(ctx.guild.id)

    avg_elo, gain, _ = _compute_match_change_for_members(ctx.guild.id, players)

    embed = discord.Embed(
        title="🏆 Résultats — Victoire enregistrée !",
        description=f"Avg ELO du groupe : **{avg_elo}** -> +**{gain}** ELO chacun",
        color=0x2ecc71,
        timestamp=datetime.now(timezone.utc),
    )
    for member in players:
        doc = get_player(col, member)
        old = doc["elo"]
        new = old + gain
        col.update_one({"_id": str(member.id)}, {"$set": {"elo": new}, "$inc": {"wins": 1}})
        embed.add_field(name=member.display_name, value=f"+{gain} ELO -> **{new}**", inline=False)
    embed.set_footer(text=f"Enregistre par {ctx.author.display_name}")
    await ctx.send(embed=embed)
    await _refresh_leaderboard_safe(ctx.guild)

@bot.command(name="lose")
async def lose_prefix(ctx, joueur1: discord.Member, joueur2: discord.Member = None, joueur3: discord.Member = None, joueur4: discord.Member = None, joueur5: discord.Member = None):
    if not ctx.author.guild_permissions.manage_guild:
        role_id = get_bypass_role(ctx.guild.id)
        if not role_id or not any(r.id == role_id for r in ctx.author.roles):
            await ctx.send("Pas la permission.")
            return
    players = [p for p in [joueur1, joueur2, joueur3, joueur4, joueur5] if p is not None]
    col = get_elo_col(ctx.guild.id)

    avg_elo, _, loss = _compute_match_change_for_members(ctx.guild.id, players)

    embed = discord.Embed(
        title="💀 Résultats — Défaite enregistrée !",
        description=f"Avg ELO du groupe : **{avg_elo}** -> -**{loss}** ELO chacun",
        color=0xe74c3c,
        timestamp=datetime.now(timezone.utc),
    )
    for member in players:
        doc = get_player(col, member)
        old = doc["elo"]
        new = max(0, old - loss)
        col.update_one({"_id": str(member.id)}, {"$set": {"elo": new}, "$inc": {"losses": 1}})
        embed.add_field(name=member.display_name, value=f"-{loss} ELO -> **{new}**", inline=False)
    embed.set_footer(text=f"Enregistre par {ctx.author.display_name}")
    await ctx.send(embed=embed)
    await _refresh_leaderboard_safe(ctx.guild)

@bot.command(name="map")
async def map_prefix(ctx):
    if not ctx.author.guild_permissions.manage_guild:
        role_id = get_bypass_role(ctx.guild.id)
        if not role_id or not any(r.id == role_id for r in ctx.author.roles):
            await ctx.send("Pas la permission.")
            return
    chosen = random.choice(MAPS)
    embed = discord.Embed(title="🗺️ Map sélectionnée !", description=f"## {chosen}", color=0x9b59b6, timestamp=datetime.now(timezone.utc))
    await ctx.send(embed=embed)

@bot.command(name="resetelo")
async def resetelo_prefix(ctx, member: discord.Member):
    if not ctx.author.guild_permissions.manage_guild:
        role_id = get_bypass_role(ctx.guild.id)
        if not role_id or not any(r.id == role_id for r in ctx.author.roles):
            await ctx.send("Pas la permission.")
            return
    col = get_elo_col(ctx.guild.id)
    doc = get_player(col, member)
    old = doc["elo"]
    col.update_one({"_id": str(member.id)}, {"$set": {"elo": 0, "wins": 0, "losses": 0}})
    embed = discord.Embed(title="🔄 ELO réinitialisé !", color=0x95a5a6, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Joueur", value=member.mention, inline=True)
    embed.add_field(name="Ancien ELO", value=str(old), inline=True)
    embed.add_field(name="Nouvel ELO", value="0", inline=True)
    await ctx.send(embed=embed)
    await _refresh_leaderboard_safe(ctx.guild)

# ── Système de candidatures ────────────────────────────────────
CANDIDATURE_CHANNEL = "candidatures"
WELCOME_CHANNEL     = "verify"
PLAYERS_ROLE        = "Members"
STAFF_ROLE          = "Coach/Analyst/Manager"

class ApplicationModal(discord.ui.Modal, title="Candidature 10mans"):
    pseudo = discord.ui.TextInput(label="Quel est ton pseudo ?", placeholder="Comment puis-je t'appeler ? ex : jetax", max_length=50)
    tracker = discord.ui.TextInput(label="Lien vers ton tracker", placeholder="https://tracker.gg/...", max_length=200)
    experience = discord.ui.TextInput(label="Experiences en tournois / LAN ?", placeholder="Indique les tournois/lans auxquels tu as participe", style=discord.TextStyle.paragraph, required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        # Defer immediat : le DM utilisateur (slow Discord rate-limit possible)
        # et l'envoi sur le salon candidatures peuvent depasser les 3s du
        # token d'interaction. Le defer libere ce delai.
        await interaction.response.defer(ephemeral=True, thinking=True)
        cooldown_col = db["candidature_cooldowns"]
        uid = str(interaction.user.id)
        doc = cooldown_col.find_one({"_id": uid})
        if doc:
            last = doc["last_apply"]
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            diff = datetime.now(timezone.utc) - last
            if diff.total_seconds() < 3600:
                remaining = 3600 - diff.total_seconds()
                minutes = int(remaining // 60)
                seconds = int(remaining % 60)
                await interaction.followup.send(f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**.", ephemeral=True)
                return
        cooldown_col.update_one({"_id": uid}, {"$set": {"last_apply": datetime.now(timezone.utc)}}, upsert=True)
        try:
            await interaction.user.send(embed=discord.Embed(title="✅ Candidature reçue !", description="Merci d'avoir postulé, nous analysons votre profil et nous revenons vers vous le plus vite possible.", color=0x2ecc71, timestamp=datetime.now(timezone.utc)))
        except discord.Forbidden:
            pass
        channel = discord.utils.get(interaction.guild.text_channels, name=CANDIDATURE_CHANNEL)
        if not channel:
            await interaction.followup.send("Salon candidatures introuvable.", ephemeral=True)
            return
        embed = discord.Embed(title="📋 Nouvelle candidature", description="🎮 **Candidature Joueur**", color=0x5865f2, timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Membre", value=interaction.user.mention, inline=True)
        embed.add_field(name="🎮 Pseudo en jeu", value=self.pseudo.value, inline=True)
        embed.add_field(name="🔗 Tracker", value=self.tracker.value, inline=False)
        embed.add_field(name="🏆 Tournois / LAN", value=self.experience.value if self.experience.value else "Aucune", inline=False)
        embed.set_footer(text=f"ID: {interaction.user.id}")
        view = ApplicationReviewView()
        msg = await channel.send(embed=embed, view=view)
        repository.register_application(
            db, interaction.guild_id, msg.id, interaction.user.id, is_staff=False,
        )
        await interaction.followup.send("✅ Ta candidature a bien été envoyée !", ephemeral=True)


class RefuseReasonModal(discord.ui.Modal, title="Raison du refus"):
    reason = discord.ui.TextInput(label="Raison du refus (optionnel)", placeholder="Explique pourquoi...", style=discord.TextStyle.paragraph, required=False, max_length=500)

    def __init__(self, applicant_id: int):
        super().__init__()
        self.applicant_id = applicant_id

    async def on_submit(self, interaction: discord.Interaction):
        # CAS atomique : empeche refuse concurrent avec accept (autre admin).
        # Doit etre fait avant tout side-effect (kick, DM, edit).
        claimed = repository.claim_application_decision(
            db, interaction.guild_id, interaction.message.id,
            status="refused", decided_by=interaction.user.id,
        )
        if not claimed:
            await interaction.response.send_message(
                "❌ Cette candidature a deja ete traitee par un autre admin.",
                ephemeral=True,
            )
            return
        member = interaction.guild.get_member(self.applicant_id)
        reason_text = self.reason.value if self.reason.value else "Aucune raison fournie."
        if member:
            try:
                embed_dm = discord.Embed(title="❌ Candidature refusée", description="Désolé, votre candidature n'a pas été retenue, merci de réessayer plus tard.", color=0xe74c3c, timestamp=datetime.now(timezone.utc))
                embed_dm.add_field(name="📋 Raison", value=reason_text, inline=False)
                await member.send(embed=embed_dm)
            except discord.Forbidden:
                pass
            try:
                await member.kick(reason=f"Candidature refusee : {reason_text}")
            except discord.Forbidden:
                pass
        try:
            embed = interaction.message.embeds[0]
            embed.color = 0xe74c3c
            embed.add_field(name="Refuse par", value=interaction.user.mention, inline=True)
            embed.add_field(name="📋 Raison", value=reason_text, inline=True)
            await interaction.message.edit(embed=embed, view=None)
        except Exception:
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
        await interaction.response.send_message("✅ Candidature refusée et utilisateur kické.", ephemeral=True)


def _parse_application_embed(message: discord.Message) -> tuple[int | None, str, bool]:
    """Extrait (applicant_id, pseudo, is_staff) depuis l'embed d'une
    candidature. Retourne (None, "", False) si parsing impossible.

    Permet a `ApplicationReviewView` d'etre persistante (sans state interne)
    en reconstruisant le contexte depuis le message a chaque clic."""
    if not message.embeds:
        return None, "", False
    embed = message.embeds[0]
    is_staff = "Staff" in (embed.title or "")
    applicant_id: int | None = None
    footer_text = (embed.footer.text or "") if embed.footer else ""
    if footer_text.startswith("ID:"):
        try:
            applicant_id = int(footer_text.split(":", 1)[1].strip())
        except (ValueError, IndexError):
            applicant_id = None
    pseudo = ""
    for field in embed.fields:
        if field.name in ("🎮 Pseudo en jeu", "🎮 Pseudo"):
            pseudo = field.value or ""
            break
    return applicant_id, pseudo, is_staff


class ApplicationReviewView(discord.ui.View):
    """Vue persistante : se reconstruit a partir de l'embed du message."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Accepter", style=discord.ButtonStyle.success,
        custom_id="application_accept",
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_access(interaction):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission de traiter les candidatures.",
                ephemeral=True,
            )
            return
        # CAS atomique : seul un admin peut decider chaque candidature.
        # Empeche role grant + kick concurrents si 2 admins cliquent en
        # meme temps (accept/refuse), et le double DM.
        claimed = repository.claim_application_decision(
            db, interaction.guild_id, interaction.message.id,
            status="accepted", decided_by=interaction.user.id,
        )
        if not claimed:
            await interaction.response.send_message(
                "❌ Cette candidature a deja ete traitee par un autre admin.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        applicant_id, pseudo, is_staff = _parse_application_embed(interaction.message)
        if applicant_id is None:
            await interaction.followup.send(
                "❌ Donnees candidature illisibles (embed corrompu).",
                ephemeral=True,
            )
            return
        member = interaction.guild.get_member(applicant_id)
        if not member:
            await interaction.followup.send("❌ Membre introuvable.", ephemeral=True)
            return
        try:
            old_embed = interaction.message.embeds[0] if interaction.message.embeds else None
            new_embed = discord.Embed(title="📋 Candidature acceptée", color=0x2ecc71, timestamp=datetime.now(timezone.utc))
            new_embed.set_thumbnail(url=member.display_avatar.url)
            new_embed.add_field(name="👤 Membre", value=member.mention, inline=True)
            new_embed.add_field(name="🎮 Pseudo", value=pseudo, inline=True)
            if old_embed:
                for field in old_embed.fields:
                    if field.name in ("🔗 Tracker", "🏆 Tournois / LAN", "💼 Poste", "📋 Expériences", "Tracker", "Tournois / LAN", "Poste", "Experiences"):
                        new_embed.add_field(name=field.name, value=field.value, inline=False)
            new_embed.add_field(name="✅ Accepté par", value=interaction.user.mention, inline=False)
            await interaction.message.edit(embed=new_embed, view=None)
        except Exception as e:
            logger.exception("[accept] Edit impossible")
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
        role_name = STAFF_ROLE if is_staff else PLAYERS_ROLE
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role:
            try:
                await member.add_roles(role)
            except Exception as e:
                logger.exception("[accept] Role impossible")
        if is_staff:
            members_role = discord.utils.get(interaction.guild.roles, name=PLAYERS_ROLE)
            if members_role:
                try:
                    await member.add_roles(members_role)
                except Exception as e:
                    logger.exception("[accept] Role Members impossible")
        try:
            await member.edit(nick=pseudo)
        except Exception:
            pass
        try:
            await member.send(embed=discord.Embed(title="🎉 Candidature acceptée !", description="Bravo, vous avez été accepté, vous pouvez désormais faire des 10mans !", color=0x2ecc71, timestamp=datetime.now(timezone.utc)))
        except discord.Forbidden:
            pass
        await interaction.followup.send("✅ Candidature acceptée !", ephemeral=True)

    @discord.ui.button(
        label="Refuser", style=discord.ButtonStyle.danger,
        custom_id="application_refuse",
    )
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_access(interaction):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission de traiter les candidatures.",
                ephemeral=True,
            )
            return
        applicant_id, _pseudo, _is_staff = _parse_application_embed(interaction.message)
        if applicant_id is None:
            await interaction.response.send_message(
                "❌ Donnees candidature illisibles (embed corrompu).",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(RefuseReasonModal(applicant_id=applicant_id))


class StaffModal(discord.ui.Modal, title="Candidature Staff"):
    pseudo = discord.ui.TextInput(label="Quel est ton pseudo ?", placeholder="Comment puis-je t'appeler ? ex : jetax", max_length=50)
    poste = discord.ui.TextInput(label="Poste occupe actuellement", placeholder="Ex : Coach, Analyst, Manager... et dans quelle structure/organisation ?", max_length=100)
    experience = discord.ui.TextInput(label="Experiences", placeholder="Decris tes experiences dans le domaine...", style=discord.TextStyle.paragraph, required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cooldown_col = db["candidature_cooldowns"]
        uid = str(interaction.user.id)
        doc = cooldown_col.find_one({"_id": uid})
        if doc:
            last = doc["last_apply"]
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            diff = datetime.now(timezone.utc) - last
            if diff.total_seconds() < 3600:
                remaining = 3600 - diff.total_seconds()
                minutes = int(remaining // 60)
                seconds = int(remaining % 60)
                await interaction.followup.send(f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**.", ephemeral=True)
                return
        cooldown_col.update_one({"_id": uid}, {"$set": {"last_apply": datetime.now(timezone.utc)}}, upsert=True)
        try:
            await interaction.user.send(embed=discord.Embed(title="✅ Candidature reçue !", description="Merci d'avoir postulé, nous analysons votre profil et nous revenons vers vous le plus vite possible.", color=0x2ecc71, timestamp=datetime.now(timezone.utc)))
        except discord.Forbidden:
            pass
        channel = discord.utils.get(interaction.guild.text_channels, name=CANDIDATURE_CHANNEL)
        if not channel:
            await interaction.followup.send("Salon candidatures introuvable.", ephemeral=True)
            return
        embed = discord.Embed(title="📋 Nouvelle candidature Staff", description="🎯 **Candidature Coach / Analyst / Manager**", color=0xe67e22, timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Membre",      value=interaction.user.mention,                                    inline=True)
        embed.add_field(name="🎮 Pseudo",       value=self.pseudo.value,                                          inline=True)
        embed.add_field(name="💼 Poste",        value=self.poste.value,                                           inline=False)
        embed.add_field(name="📋 Expériences",  value=self.experience.value if self.experience.value else "Aucune", inline=False)
        embed.set_footer(text=f"ID: {interaction.user.id}")
        view = ApplicationReviewView()
        msg = await channel.send(embed=embed, view=view)
        repository.register_application(
            db, interaction.guild_id, msg.id, interaction.user.id, is_staff=True,
        )
        await interaction.followup.send("✅ Ta candidature a bien été envoyée !", ephemeral=True)


class RoleChoiceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Joueur", style=discord.ButtonStyle.primary)
    async def joueur_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApplicationModal())

    @discord.ui.button(label="Coach / Analyst / Manager", style=discord.ButtonStyle.secondary)
    async def staff_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffModal())


class WelcomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Postuler", style=discord.ButtonStyle.primary, custom_id="postuler_btn")
    async def postuler(self, interaction: discord.Interaction, button: discord.ui.Button):
        cooldown_col = db["candidature_cooldowns"]
        uid = str(interaction.user.id)
        doc = cooldown_col.find_one({"_id": uid})
        if doc:
            last = doc["last_apply"]
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            diff = datetime.now(timezone.utc) - last
            if diff.total_seconds() < 3600:
                remaining = 3600 - diff.total_seconds()
                minutes = int(remaining // 60)
                seconds = int(remaining % 60)
                await interaction.response.send_message(f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**.", ephemeral=True)
                return
        await interaction.response.send_message("## Pour quel poste souhaites-tu postuler ? 🎮", view=RoleChoiceView(), ephemeral=True)


# ── /welcome ───────────────────────────────────────────────────
@tree.command(name="welcome", description="Envoie le message de bienvenue dans le salon verify")
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome(interaction: discord.Interaction):
    channel = discord.utils.get(interaction.guild.text_channels, name=WELCOME_CHANNEL)
    if not channel:
        await interaction.response.send_message("Salon verify introuvable.", ephemeral=True)
        return
    embed = discord.Embed(
        title="Bienvenue sur le serveur 10mans FR",
        description="Bienvenue sur un serveur de **10mans français** accessible exclusivement aux joueurs ayant un ELO d'au moins **High Ascendant**.\n\nPour pouvoir accéder au serveur, merci de cliquer sur le bouton **Postuler** juste en dessous.\n\n**Bon Jeu ! 🍀**",
        color=0x5865f2,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=interaction.guild.name)
    await channel.send(embed=embed, view=WelcomeView())
    await interaction.response.send_message(f"Message envoye dans {channel.mention} !", ephemeral=True)

@welcome.error
async def welcome_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("Seuls les administrateurs peuvent utiliser cette commande.", ephemeral=True)

# ── Chargement des cogs V2 ────────────────────────────────────────
riot_client = HenrikDevClient()


async def _load_v2_cogs() -> None:
    from cogs.riot_link import setup as setup_riot_link
    from cogs.queue_v2 import setup as setup_queue_v2
    from cogs.match    import setup as setup_match

    await setup_riot_link(bot, db, riot_client)
    match_cog = await setup_match(bot, db, henrik_client=riot_client)
    await setup_queue_v2(bot, db, on_full=match_cog.on_queue_full)


@bot.event
async def setup_hook():
    # Charger les cogs essentiels (queue_v2, match, riot_link). Sans eux,
    # le bot demarre en mode degrade (slash commands manquantes, queue
    # inaccessible) sans signaler clairement l'erreur. On log + on
    # raise pour fail fast plutot que de laisser tourner un bot inutile.
    try:
        await _load_v2_cogs()
    except Exception:
        # Re-raise : Discord.py va arreter le startup. Mieux qu'un bot
        # silencieusement cassé en prod.
        logger.critical("[setup_hook] CRITIQUE : echec chargement des cogs", exc_info=True)
        raise


_synced_once = False


@bot.event
async def on_ready():
    global _synced_once

    if _synced_once:
        # on_ready peut etre fire plusieurs fois (reconnects WS) : on sync
        # uniquement au premier ready pour eviter de spammer Discord et de
        # subir le temps de propagation global (~1h) inutilement. Idem
        # pour `add_view` qui referencerait des instances View neuves a
        # chaque reconnect (leak memoire mineur sur reconnects frequents).
        logger.info("Bot reconnecte : %s (sync slash skipped)", bot.user)
        return

    # Premier on_ready uniquement : enregistrement des views persistantes.
    bot.add_view(WelcomeView())
    bot.add_view(ApplicationReviewView())

    # Sync rapide sur une guild specifique si DEV_GUILD_ID est defini.
    # Sinon, sync global (peut prendre jusqu'a 1h pour propager).
    dev_guild_id = os.getenv("DEV_GUILD_ID")
    if dev_guild_id:
        guild = discord.Object(id=int(dev_guild_id))
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        logger.info("Bot connecte : %s (ID: %s)", bot.user, bot.user.id)
        logger.info("%d commandes slash synchronisees sur guild %s.", len(synced), dev_guild_id)
    else:
        synced = await tree.sync()
        logger.info("Bot connecte : %s (ID: %s)", bot.user, bot.user.id)
        logger.info("%d commandes slash synchronisees (global, propagation jusqu'a 1h).", len(synced))
    _synced_once = True

if __name__ == "__main__":
    # Configuration logging : niveau INFO + format avec timestamp et logger
    # name. Permet de filtrer en prod (ex: -e LOG_LEVEL=DEBUG via supervisor).
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    # Split stdout / stderr : DEBUG+INFO -> stdout, WARNING+ -> stderr.
    # PM2 capture stdout -> out.log et stderr -> error.log, donc tant que
    # rien n'est anormal seul out.log se remplit.
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    )
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(lambda r: r.levelno < logging.WARNING)
    stdout_handler.setFormatter(fmt)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))
    root.handlers.clear()
    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable not set")
    bot.run(TOKEN)
