"""
Cog V2 : queue 10mans avec boutons persistants (Rejoindre / Quitter).

Flux :
  1. Admin lance /setup-queue dans un salon -> message persistant pose.
  2. Joueurs cliquent "Rejoindre" / "Quitter".
     - Refus si pas de compte Riot lie.
     - Refus si deja dans la queue / pas dans la queue / queue pleine / queue fermee.
  3. A 10 joueurs : status passe a "forming", _on_queue_full() est appele
     (Phase 4 implementera la formation effective des equipes).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services import repository


JOIN_BTN_ID:  str = "queue_v2:join"
LEAVE_BTN_ID: str = "queue_v2:leave"
QUEUE_SIZE:   int = 10


# ── Embed builder ─────────────────────────────────────────────────
def build_queue_embed(queue_doc: dict | None, guild: discord.Guild) -> discord.Embed:
    players = list((queue_doc or {}).get("players", []))
    count   = len(players)
    full    = count >= QUEUE_SIZE
    status  = (queue_doc or {}).get("status", "open")

    if status == "forming":
        color = 0xe67e22
        state = "🔥 Match en formation"
    elif full:
        color = 0x2ecc71
        state = "🟢 Queue pleine !"
    else:
        color = 0x5865f2
        state = "🔵 En attente de joueurs"

    embed = discord.Embed(
        title=f"🎮 File d'attente Valorant 10mans — {count}/{QUEUE_SIZE}",
        description=state,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    if players:
        mentions = "\n".join(f"• <@{uid}>" for uid in players)
        embed.add_field(name="Joueurs", value=mentions, inline=False)
    else:
        embed.add_field(name="Joueurs", value="*Personne pour le moment.*", inline=False)

    embed.set_footer(text=guild.name)
    return embed


# ── View persistante ──────────────────────────────────────────────
class QueueView(discord.ui.View):
    """View persistante : Rejoindre / Quitter."""

    def __init__(self, db, on_full=None) -> None:
        super().__init__(timeout=None)
        self.db        = db
        self._on_full  = on_full
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    @discord.ui.button(
        label="Rejoindre", style=discord.ButtonStyle.success, custom_id=JOIN_BTN_ID,
    )
    async def join_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        async with self._lock(inter.guild_id):
            # 1) compte Riot lie ?
            riot = repository.get_riot_account(self.db, inter.guild_id, inter.user.id)
            if not riot:
                await inter.response.send_message(
                    "❌ Lie d'abord ton compte Riot avec `/link-riot Pseudo#TAG`.",
                    ephemeral=True,
                )
                return

            # 2) ajout en base
            res = repository.add_player_to_queue(
                self.db, inter.guild_id, inter.user.id,
            )
            if not res.success:
                await inter.response.send_message(_join_error_message(res.reason), ephemeral=True)
                return

            # 3) si la queue est maintenant pleine -> on ferme + trigger formation
            queue_doc = res.queue
            full = len(queue_doc.get("players", [])) >= QUEUE_SIZE
            if full:
                repository.close_active_queue(self.db, inter.guild_id)
                queue_doc = repository.get_active_queue(self.db, inter.guild_id)

            # 4) edit du message
            embed = build_queue_embed(queue_doc, inter.guild)
            await inter.response.edit_message(embed=embed, view=self)

            # 4b) confirmation ephemere (visible uniquement par le joueur)
            count = len(queue_doc.get("players", []))
            await inter.followup.send(
                f"✅ Tu as rejoint la queue ({count}/{QUEUE_SIZE})",
                ephemeral=True,
            )

            # 5) trigger formation (Phase 4 — on a un point d'extension)
            if full and self._on_full:
                asyncio.create_task(self._on_full(inter, queue_doc))

    @discord.ui.button(
        label="Quitter", style=discord.ButtonStyle.danger, custom_id=LEAVE_BTN_ID,
    )
    async def leave_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        async with self._lock(inter.guild_id):
            res = repository.remove_player_from_queue(
                self.db, inter.guild_id, inter.user.id,
            )
            if not res.success:
                await inter.response.send_message(_leave_error_message(res.reason), ephemeral=True)
                return
            embed = build_queue_embed(res.queue, inter.guild)
            await inter.response.edit_message(embed=embed, view=self)


def _join_error_message(reason: str) -> str:
    return {
        "no_queue":     "❌ Aucune queue active sur ce serveur.",
        "queue_closed": "❌ La queue est fermee (match en cours de formation).",
        "already_in":   "❌ Tu es deja dans la queue.",
        "queue_full":   "❌ La queue est pleine (10/10).",
        "race":         "⚠️ Conflit, reessaie.",
    }.get(reason, f"❌ Erreur : {reason}")


def _leave_error_message(reason: str) -> str:
    return {
        "no_queue": "❌ Aucune queue active.",
        "not_in":   "❌ Tu n'es pas dans la queue.",
    }.get(reason, f"❌ Erreur : {reason}")


# ── Cog ───────────────────────────────────────────────────────────
class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db, on_full=None) -> None:
        self.bot     = bot
        self.db      = db
        self.on_full = on_full
        self.view    = QueueView(db, on_full=on_full)

    @app_commands.command(name="setup-queue", description="Pose le message de queue dans ce salon")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_queue(self, interaction: discord.Interaction) -> None:
        # Reset de la queue precedente s'il y en avait une
        repository.delete_active_queue(self.db, interaction.guild_id)

        embed = build_queue_embed(None, interaction.guild)
        msg = await interaction.channel.send(embed=embed, view=self.view)

        repository.setup_active_queue(
            self.db,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            message_id=msg.id,
        )

        await interaction.response.send_message(
            f"✅ Queue active dans {interaction.channel.mention} !",
            ephemeral=True,
        )

    @app_commands.command(name="close-queue", description="Ferme la queue active")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def close_queue(self, interaction: discord.Interaction) -> None:
        deleted = repository.delete_active_queue(self.db, interaction.guild_id)
        msg = "✅ Queue supprimee." if deleted else "ℹ️ Aucune queue active."
        await interaction.response.send_message(msg, ephemeral=True)

    @setup_queue.error
    @close_queue.error
    async def _perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Reserve aux administrateurs.", ephemeral=True,
            )


async def setup(bot: commands.Bot, db, on_full=None) -> None:
    cog = QueueCog(bot, db, on_full=on_full)
    await bot.add_cog(cog)
    bot.add_view(cog.view)  # rend le view persistant apres restart
