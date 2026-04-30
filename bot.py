import discord
from discord.ext import commands
from discord import app_commands
import os
import asyncio
from datetime import datetime, timezone
import random
from pymongo import MongoClient
from leaderboard_img import generate_leaderboard

from services import elo_calc, repository
from services.riot_api import HenrikDevClient

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
client = MongoClient(MONGO_URL, tz_aware=True, tzinfo=timezone.utc)
db     = client["elobot"]

def get_elo_col(guild_id):
    return repository.get_elo_col(db, guild_id)

def get_bypass_col():
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
    set_bypass_role(interaction.guild_id, role.id)
    embed = discord.Embed(
        title="🔓 Bypass activé !",
        description=f"Le role {role.mention} a maintenant acces a toutes les commandes du bot.",
        color=0xe67e22,
        timestamp=datetime.now()
    )
    embed.set_footer(text=f"Configuré par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

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
        timestamp=datetime.now(),
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
        timestamp=datetime.now(),
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
    embed = discord.Embed(title="🗺️ Map sélectionnée !", description=f"## {chosen}", color=0x9b59b6, timestamp=datetime.now())
    embed.set_footer(text=f"Tirage par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

@tree.command(name="coinflip", description="Fait un pile ou face")
async def coinflip(interaction: discord.Interaction):
    result = random.choice(["Pile", "Face"])
    embed  = discord.Embed(title="🪙 Pile ou Face !", description=f"## {result}", color=0xf1c40f, timestamp=datetime.now())
    embed.set_footer(text=f"Lancé par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


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
    col  = get_elo_col(interaction.guild_id)
    docs = list(col.find().sort("elo", -1))
    if not docs:
        await interaction.followup.send("Aucun joueur enregistre.", ephemeral=True)
        return
    all_players = []
    rank = 1
    for doc in docs:
        uid    = doc["_id"]
        member = interaction.guild.get_member(int(uid))
        # Si le membre n'est pas dans le cache du bot, on l'inclut quand meme
        # avec son nom stocke en base (avatar par defaut Discord pour fallback).
        if member is not None:
            ava_url = str(member.display_avatar.replace(format="png", size=64).url)
            display_name = member.display_name or doc.get("name", uid)
        else:
            ava_url = f"https://cdn.discordapp.com/embed/avatars/{int(uid) % 6}.png"
            display_name = doc.get("name", str(uid))
        all_players.append({
            "rank":       rank,
            "name":       display_name,
            "elo":        doc["elo"],
            "wins":       doc.get("wins", 0),
            "losses":     doc.get("losses", 0),
            "kills":      doc.get("kills", 0),
            "deaths":     doc.get("deaths", 0),
            "avatar_url": ava_url,
        })
        rank += 1
    if not all_players:
        await interaction.followup.send("Aucun joueur enregistre.", ephemeral=True)
        return
    PAGE_SIZE   = 15
    total_pages = max(1, (len(all_players) + PAGE_SIZE - 1) // PAGE_SIZE)
    loop        = asyncio.get_event_loop()

    async def build_page(page: int) -> discord.File:
        start = page * PAGE_SIZE
        chunk = all_players[start:start + PAGE_SIZE]
        buf = await loop.run_in_executor(None, lambda: generate_leaderboard(chunk, server_name=interaction.guild.name))
        return discord.File(buf, filename="leaderboard.png")

    class LeaderboardView(discord.ui.View):
        def __init__(self, page: int):
            super().__init__(timeout=300)
            self.page = page
            self.update_buttons()
        def update_buttons(self):
            self.prev_btn.disabled = self.page == 0
            self.next_btn.disabled = self.page >= total_pages - 1
            self.page_btn.label    = f"Page {self.page + 1} / {total_pages}"
        async def _go(self, inter: discord.Interaction, new_page: int):
            if new_page < 0 or new_page >= total_pages:
                if not inter.response.is_done():
                    await inter.response.defer()
                return
            self.page = new_page
            self.update_buttons()
            try:
                if not inter.response.is_done():
                    await inter.response.defer()
                file = await build_page(self.page)
                await inter.followup.edit_message(
                    message_id=inter.message.id,
                    attachments=[file],
                    view=self,
                )
            except Exception:
                import traceback
                traceback.print_exc()
                try:
                    await inter.followup.send(
                        "Erreur lors du changement de page. Réessaie.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
        @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
        async def prev_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            await self._go(inter, self.page - 1)
        @discord.ui.button(label="Page 1 / 1", style=discord.ButtonStyle.grey, disabled=True)
        async def page_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            pass
        @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
        async def next_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            await self._go(inter, self.page + 1)

    view = LeaderboardView(page=0)
    file = await build_page(0)
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
        embed = discord.Embed(title="🔄 Reset général !", description=f"ELO de **{count} joueur(s)** remis a 0.", color=0xe74c3c, timestamp=datetime.now())
        embed.set_footer(text=f"Reset par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        return
    if joueur is None:
        await interaction.response.send_message("Mentionne un joueur ou utilise all:True.", ephemeral=True)
        return
    doc = get_player(col, joueur)
    old = doc["elo"]
    col.update_one({"_id": str(joueur.id)}, {"$set": {"elo": 0, "wins": 0, "losses": 0}})
    embed = discord.Embed(title="🔄 ELO réinitialisé !", color=0x95a5a6, timestamp=datetime.now())
    embed.add_field(name="Joueur", value=joueur.mention, inline=True)
    embed.add_field(name="Ancien ELO", value=str(old), inline=True)
    embed.add_field(name="Nouvel ELO", value="0", inline=True)
    embed.set_thumbnail(url=joueur.display_avatar.url)
    embed.set_footer(text=f"Reset par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

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
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now())
    embed.add_field(name="Joueur",       value=joueur.mention,                    inline=True)
    embed.add_field(name="Modification", value=label,                             inline=True)
    embed.add_field(name="Nouvel ELO",   value=f"**{new}** (etait {old})",        inline=True)
    embed.set_footer(text=f"Par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

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
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now())
    embed.add_field(name="Joueur",        value=joueur.mention,             inline=True)
    embed.add_field(name="Modification",  value=label,                      inline=True)
    embed.add_field(name="Nouveau total", value=f"**{new}** (etait {old})", inline=True)
    embed.set_footer(text=f"Par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

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
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now())
    embed.add_field(name="Joueur", value=joueur.mention, inline=True)
    embed.add_field(name="Modification", value=label, inline=True)
    embed.add_field(name="Nouveau total", value=f"**{new}** (etait {old})", inline=True)
    embed.set_footer(text=f"Par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

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
    rank    = col.count_documents({"elo": {"$gt": elo}}) + 1
    embed = discord.Embed(title=f"📊 Stats de {joueur.display_name}", color=0x3498db, timestamp=datetime.now())
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
    embed = discord.Embed(title="🗑️ Messages supprimés", description=f"**{len(deleted)}** message(s) supprime(s).", color=0xe74c3c, timestamp=datetime.now())
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
        embed = discord.Embed(title="⚙️ Commandes Admin", color=0xe74c3c, timestamp=datetime.now())
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
        embed = discord.Embed(title="📖 Commandes disponibles", color=0x3498db, timestamp=datetime.now())
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
    docs = list(col.find().sort("elo", -1).limit(10))
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
    embed = discord.Embed(title="Classement ELO", description="\n".join(lines), color=0xf1c40f, timestamp=datetime.now())
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
    rank    = col.count_documents({"elo": {"$gt": elo}}) + 1
    embed = discord.Embed(title=f"Stats de {member.display_name}", color=0x3498db, timestamp=datetime.now())
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
        timestamp=datetime.now(),
    )
    for member in players:
        doc = get_player(col, member)
        old = doc["elo"]
        new = old + gain
        col.update_one({"_id": str(member.id)}, {"$set": {"elo": new}, "$inc": {"wins": 1}})
        embed.add_field(name=member.display_name, value=f"+{gain} ELO -> **{new}**", inline=False)
    embed.set_footer(text=f"Enregistre par {ctx.author.display_name}")
    await ctx.send(embed=embed)

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
        timestamp=datetime.now(),
    )
    for member in players:
        doc = get_player(col, member)
        old = doc["elo"]
        new = max(0, old - loss)
        col.update_one({"_id": str(member.id)}, {"$set": {"elo": new}, "$inc": {"losses": 1}})
        embed.add_field(name=member.display_name, value=f"-{loss} ELO -> **{new}**", inline=False)
    embed.set_footer(text=f"Enregistre par {ctx.author.display_name}")
    await ctx.send(embed=embed)

@bot.command(name="map")
async def map_prefix(ctx):
    if not ctx.author.guild_permissions.manage_guild:
        role_id = get_bypass_role(ctx.guild.id)
        if not role_id or not any(r.id == role_id for r in ctx.author.roles):
            await ctx.send("Pas la permission.")
            return
    chosen = random.choice(MAPS)
    embed = discord.Embed(title="🗺️ Map sélectionnée !", description=f"## {chosen}", color=0x9b59b6, timestamp=datetime.now())
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
    embed = discord.Embed(title="🔄 ELO réinitialisé !", color=0x95a5a6, timestamp=datetime.now())
    embed.add_field(name="Joueur", value=member.mention, inline=True)
    embed.add_field(name="Ancien ELO", value=str(old), inline=True)
    embed.add_field(name="Nouvel ELO", value="0", inline=True)
    await ctx.send(embed=embed)

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
        cooldown_col = db["candidature_cooldowns"]
        uid = str(interaction.user.id)
        doc = cooldown_col.find_one({"_id": uid})
        if doc:
            last = doc["last_apply"]
            diff = datetime.now() - last
            if diff.total_seconds() < 3600:
                remaining = 3600 - diff.total_seconds()
                minutes = int(remaining // 60)
                seconds = int(remaining % 60)
                await interaction.response.send_message(f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**.", ephemeral=True)
                return
        cooldown_col.update_one({"_id": uid}, {"$set": {"last_apply": datetime.now()}}, upsert=True)
        try:
            await interaction.user.send(embed=discord.Embed(title="✅ Candidature reçue !", description="Merci d'avoir postulé, nous analysons votre profil et nous revenons vers vous le plus vite possible.", color=0x2ecc71, timestamp=datetime.now()))
        except discord.Forbidden:
            pass
        channel = discord.utils.get(interaction.guild.text_channels, name=CANDIDATURE_CHANNEL)
        if not channel:
            await interaction.response.send_message("Salon candidatures introuvable.", ephemeral=True)
            return
        embed = discord.Embed(title="📋 Nouvelle candidature", description="🎮 **Candidature Joueur**", color=0x5865f2, timestamp=datetime.now())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Membre", value=interaction.user.mention, inline=True)
        embed.add_field(name="🎮 Pseudo en jeu", value=self.pseudo.value, inline=True)
        embed.add_field(name="🔗 Tracker", value=self.tracker.value, inline=False)
        embed.add_field(name="🏆 Tournois / LAN", value=self.experience.value if self.experience.value else "Aucune", inline=False)
        embed.set_footer(text=f"ID: {interaction.user.id}")
        view = ApplicationReviewView(applicant_id=interaction.user.id, pseudo=self.pseudo.value)
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ Ta candidature a bien été envoyée !", ephemeral=True)


class RefuseReasonModal(discord.ui.Modal, title="Raison du refus"):
    reason = discord.ui.TextInput(label="Raison du refus (optionnel)", placeholder="Explique pourquoi...", style=discord.TextStyle.paragraph, required=False, max_length=500)

    def __init__(self, applicant_id: int):
        super().__init__()
        self.applicant_id = applicant_id

    async def on_submit(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(self.applicant_id)
        reason_text = self.reason.value if self.reason.value else "Aucune raison fournie."
        if member:
            try:
                embed_dm = discord.Embed(title="❌ Candidature refusée", description="Désolé, votre candidature n'a pas été retenue, merci de réessayer plus tard.", color=0xe74c3c, timestamp=datetime.now())
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


class ApplicationReviewView(discord.ui.View):
    def __init__(self, applicant_id: int, pseudo: str, is_staff: bool = False):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.pseudo       = pseudo
        self.is_staff     = is_staff

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        member = interaction.guild.get_member(self.applicant_id)
        if not member:
            await interaction.followup.send("❌ Membre introuvable.", ephemeral=True)
            return
        try:
            old_embed = interaction.message.embeds[0] if interaction.message.embeds else None
            new_embed = discord.Embed(title="📋 Candidature acceptée", color=0x2ecc71, timestamp=datetime.now())
            new_embed.set_thumbnail(url=member.display_avatar.url)
            new_embed.add_field(name="👤 Membre", value=member.mention, inline=True)
            new_embed.add_field(name="🎮 Pseudo", value=self.pseudo, inline=True)
            if old_embed:
                for field in old_embed.fields:
                    if field.name in ("🔗 Tracker", "🏆 Tournois / LAN", "💼 Poste", "📋 Expériences", "Tracker", "Tournois / LAN", "Poste", "Experiences"):
                        new_embed.add_field(name=field.name, value=field.value, inline=False)
            new_embed.add_field(name="✅ Accepté par", value=interaction.user.mention, inline=False)
            await interaction.message.edit(embed=new_embed, view=None)
        except Exception as e:
            print(f"[accept] Edit impossible : {e}")
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
        role_name = STAFF_ROLE if self.is_staff else PLAYERS_ROLE
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role:
            try:
                await member.add_roles(role)
            except Exception as e:
                print(f"[accept] Role impossible : {e}")
        if self.is_staff:
            members_role = discord.utils.get(interaction.guild.roles, name=PLAYERS_ROLE)
            if members_role:
                try:
                    await member.add_roles(members_role)
                except Exception as e:
                    print(f"[accept] Role Members impossible : {e}")
        try:
            await member.edit(nick=self.pseudo)
        except Exception:
            pass
        try:
            await member.send(embed=discord.Embed(title="🎉 Candidature acceptée !", description="Bravo, vous avez été accepté, vous pouvez désormais faire des 10mans !", color=0x2ecc71, timestamp=datetime.now()))
        except discord.Forbidden:
            pass
        await interaction.followup.send("✅ Candidature acceptée !", ephemeral=True)

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.danger)
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RefuseReasonModal(applicant_id=self.applicant_id))


class StaffModal(discord.ui.Modal, title="Candidature Staff"):
    pseudo = discord.ui.TextInput(label="Quel est ton pseudo ?", placeholder="Comment puis-je t'appeler ? ex : jetax", max_length=50)
    poste = discord.ui.TextInput(label="Poste occupe actuellement", placeholder="Ex : Coach, Analyst, Manager... et dans quelle structure/organisation ?", max_length=100)
    experience = discord.ui.TextInput(label="Experiences", placeholder="Decris tes experiences dans le domaine...", style=discord.TextStyle.paragraph, required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        cooldown_col = db["candidature_cooldowns"]
        uid = str(interaction.user.id)
        doc = cooldown_col.find_one({"_id": uid})
        if doc:
            last = doc["last_apply"]
            diff = datetime.now() - last
            if diff.total_seconds() < 3600:
                remaining = 3600 - diff.total_seconds()
                minutes = int(remaining // 60)
                seconds = int(remaining % 60)
                await interaction.response.send_message(f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**.", ephemeral=True)
                return
        cooldown_col.update_one({"_id": uid}, {"$set": {"last_apply": datetime.now()}}, upsert=True)
        try:
            await interaction.user.send(embed=discord.Embed(title="✅ Candidature reçue !", description="Merci d'avoir postulé, nous analysons votre profil et nous revenons vers vous le plus vite possible.", color=0x2ecc71, timestamp=datetime.now()))
        except discord.Forbidden:
            pass
        channel = discord.utils.get(interaction.guild.text_channels, name=CANDIDATURE_CHANNEL)
        if not channel:
            await interaction.response.send_message("Salon candidatures introuvable.", ephemeral=True)
            return
        embed = discord.Embed(title="📋 Nouvelle candidature Staff", description="🎯 **Candidature Coach / Analyst / Manager**", color=0xe67e22, timestamp=datetime.now())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Membre",      value=interaction.user.mention,                                    inline=True)
        embed.add_field(name="🎮 Pseudo",       value=self.pseudo.value,                                          inline=True)
        embed.add_field(name="💼 Poste",        value=self.poste.value,                                           inline=False)
        embed.add_field(name="📋 Expériences",  value=self.experience.value if self.experience.value else "Aucune", inline=False)
        embed.set_footer(text=f"ID: {interaction.user.id}")
        view = ApplicationReviewView(applicant_id=interaction.user.id, pseudo=self.pseudo.value, is_staff=True)
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ Ta candidature a bien été envoyée !", ephemeral=True)


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
            diff = datetime.now() - last
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
        timestamp=datetime.now()
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
    await _load_v2_cogs()


@bot.event
async def on_ready():
    bot.add_view(WelcomeView())

    # Sync rapide sur une guild specifique si DEV_GUILD_ID est defini.
    # Sinon, sync global (peut prendre jusqu'a 1h pour propager).
    dev_guild_id = os.getenv("DEV_GUILD_ID")
    if dev_guild_id:
        guild = discord.Object(id=int(dev_guild_id))
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        print(f"Bot connecte : {bot.user} (ID: {bot.user.id})")
        print(f"{len(synced)} commandes slash synchronisees sur guild {dev_guild_id}.")
    else:
        synced = await tree.sync()
        print(f"Bot connecte : {bot.user} (ID: {bot.user.id})")
        print(f"{len(synced)} commandes slash synchronisees (global, propagation jusqu'a 1h).")

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable not set")
    bot.run(TOKEN)
