"""
Cog admin : commandes utilitaires (/setup, /bypass, /map, /coinflip,
/clear, /help). Extrait de bot.py (refactor monolithe).

`/setup` cree la categorie + les salons et pose les 3 messages de queue
en delegant a QueueCog.post_queue_message et refresh_leaderboard_channel.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from services import elo_calc, repository
from services.leaderboard_refresh import refresh_leaderboard_channel

logger = logging.getLogger(__name__)


SETUP_CATEGORY_NAME = "🎮 Valorant 10mans"
# 3 salons queue + 1 leaderboard partage + 1 matchs.
SETUP_CHANNELS = ["leaderboard", "pro-queue", "open-queue", "gc-queue", "matchs"]
# Mapping queue_type -> nom de salon ou poser le message persistant.
QUEUE_CHANNEL_FOR_TYPE = {"pro": "pro-queue", "open": "open-queue", "gc": "gc-queue"}


def _has_access(interaction: discord.Interaction, db) -> bool:
    """Admin (manage_guild) OU role bypass configure via /bypass."""
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = repository.get_bypass_role(db, interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db

    # ── /setup ─────────────────────────────────────────────────
    @app_commands.command(name="setup", description="Crée la catégorie et les salons necessaires au bot")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_bot(self, interaction: discord.Interaction):
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

        # 3) Pose le message persistant de chaque queue dans son salon dedie
        queue_cog = self.bot.get_cog("QueueCog")
        queue_status: list[str] = []
        if queue_cog is not None:
            for qt in repository.QUEUE_TYPES:
                channel_name = QUEUE_CHANNEL_FOR_TYPE[qt]
                chan = discord.utils.get(guild.text_channels, name=channel_name)
                if chan is None:
                    queue_status.append(f"⚠️ Salon `#{channel_name}` introuvable.")
                    continue
                repository.delete_active_queue(self.db, guild.id, qt)
                try:
                    await queue_cog.post_queue_message(chan, qt)  # type: ignore[attr-defined]
                    queue_status.append(f"🎯 Queue {qt.upper()} posée dans {chan.mention}")
                except discord.Forbidden:
                    queue_status.append(
                        f"⚠️ Impossible d'envoyer dans {chan.mention} (permissions)"
                    )

        # 4) Pre-post les 3 leaderboards (skip silencieusement si 0 joueur)
        for qt in repository.QUEUE_TYPES:
            try:
                await refresh_leaderboard_channel(guild, self.db, qt)
            except Exception:
                logger.exception("[setup] pre-post leaderboard %s a leve", qt)

        # 5) Recap
        lines: list[str] = []
        if created:
            lines.append(f"✅ Créés : {', '.join(f'`#{c}`' for c in created)}")
        if existed:
            lines.append(f"ℹ️ Déjà présents : {', '.join(f'`#{c}`' for c in existed)}")
        lines.extend(queue_status)
        if not lines:
            lines.append("✅ Setup terminé.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @setup_bot.error
    async def _setup_perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Reservé aux administrateurs.", ephemeral=True,
            )

    # ── /bypass ────────────────────────────────────────────────
    @app_commands.command(name="bypass", description="Donne acces a toutes les commandes du bot a un role")
    @app_commands.describe(role="Le role qui aura acces a toutes les commandes")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bypass(self, interaction: discord.Interaction, role: discord.Role):
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
        repository.set_bypass_role(self.db, interaction.guild_id, role.id)
        embed = discord.Embed(
            title="🔓 Bypass activé !",
            description=f"Le role {role.mention} a maintenant acces a toutes les commandes du bot.",
            color=0xe67e22,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=f"Configuré par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bypass.error
    async def _bypass_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("Seuls les administrateurs peuvent configurer le bypass.", ephemeral=True)

    # ── /map ───────────────────────────────────────────────────
    @app_commands.command(name="map", description="Sélectionne une map aléatoire pour la partie")
    async def map_pick(self, interaction: discord.Interaction):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("🚫 Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True)
            return
        chosen = random.choice(elo_calc.MAPS)
        embed = discord.Embed(title="🗺️ Map sélectionnée !", description=f"## {chosen}", color=0x9b59b6, timestamp=datetime.now(UTC))
        embed.set_footer(text=f"Tirage par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    # ── /coinflip ──────────────────────────────────────────────
    @app_commands.command(name="coinflip", description="Fait un pile ou face")
    async def coinflip(self, interaction: discord.Interaction):
        result = random.choice(["Pile", "Face"])
        embed  = discord.Embed(title="🪙 Pile ou Face !", description=f"## {result}", color=0xf1c40f, timestamp=datetime.now(UTC))
        embed.set_footer(text=f"Lancé par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    # ── /clear ─────────────────────────────────────────────────
    @app_commands.command(name="clear", description="Supprime un nombre de messages dans le salon")
    @app_commands.describe(nombre="Nombre de messages a supprimer (max 100)")
    async def clear(self, interaction: discord.Interaction, nombre: int):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("Pas la permission.", ephemeral=True)
            return
        if nombre < 1 or nombre > 100:
            await interaction.response.send_message("Le nombre doit etre entre 1 et 100.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=nombre)
        embed = discord.Embed(title="🗑️ Messages supprimés", description=f"**{len(deleted)}** message(s) supprime(s).", color=0xe74c3c, timestamp=datetime.now(UTC))
        embed.set_footer(text=f"Par {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /help ──────────────────────────────────────────────────
    @app_commands.command(name="help", description="Affiche la liste des commandes disponibles")
    @app_commands.describe(kind="Choisis le type d'aide")
    @app_commands.choices(kind=[
        app_commands.Choice(name="Commandes membres", value="membres"),
        app_commands.Choice(name="Commandes admin", value="admin"),
    ])
    @app_commands.rename(kind="type")
    async def help_cmd(self, interaction: discord.Interaction, kind: str = "membres"):
        if kind == "admin":
            if not _has_access(interaction, self.db):
                await interaction.response.send_message("Pas la permission.", ephemeral=True)
                return
            embed = discord.Embed(title="⚙️ Commandes Admin", color=0xe74c3c, timestamp=datetime.now(UTC))
            embed.add_field(name="/setup",               value="Crée la catégorie + 3 salons queue (`pro-queue`, `open-queue`, `gc-queue`) + `leaderboard` + `matchs` et pose les 3 messages de queue", inline=False)
            embed.add_field(name="/setup-queue queue",   value="Repose le message persistant d'une queue (pro/open/gc)", inline=False)
            embed.add_field(name="/close-queue queue",   value="Ferme la queue active d'un type", inline=False)
            embed.add_field(name="/win queue @j1..@j5",  value="Victoire — Pro Queue : flat ±16 ; Open/GC : pondéré par position", inline=False)
            embed.add_field(name="/lose queue @j1..@j5", value="Défaite — Pro Queue : flat ±16 ; Open/GC : pondéré par position", inline=False)
            embed.add_field(name="/map",                 value="Map aleatoire", inline=False)
            embed.add_field(name="/elomodify queue @j action montant", value="Ajoute ou enleve de l'ELO d'un joueur dans une queue", inline=False)
            embed.add_field(name="/winmodify queue @j action montant", value="Ajoute ou enleve des victoires", inline=False)
            embed.add_field(name="/losemodify queue @j action montant", value="Ajoute ou enleve des défaites", inline=False)
            embed.add_field(name="/resetelo queue [@joueur|all]", value=f"Reset ELO d'un joueur (ou tous) a {elo_calc.ELO_START} dans une queue", inline=False)
            embed.add_field(name="/reset-queue queue",   value="Drop complet d'une queue (ELO + matchs + leaderboard) — confirmation requise", inline=False)
            embed.add_field(name="/bypass @role",        value="Donne acces aux commandes admin a un role", inline=False)
            embed.add_field(name="/clear nombre",        value="Supprime des messages", inline=False)
            embed.set_footer(text=f"Demande par {interaction.user.display_name}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(title="📖 Commandes disponibles", color=0x3498db, timestamp=datetime.now(UTC))
            embed.add_field(name="/leaderboard queue", value="Classement ELO d'une queue (pro/open/gc)", inline=False)
            embed.add_field(name="/stats queue [@joueur]", value="Stats d'un joueur dans une queue. Sans mention = tes propres stats", inline=False)
            embed.add_field(name="/help", value="Affiche cette aide", inline=False)
            embed.set_footer(text=f"Demande par {interaction.user.display_name}")
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot, db) -> None:
    await bot.add_cog(AdminCog(bot, db))
