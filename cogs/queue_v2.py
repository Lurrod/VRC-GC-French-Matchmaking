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
import logging
from collections import OrderedDict
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services import repository

logger = logging.getLogger(__name__)


JOIN_BTN_ID:  str = "queue_v2:join"
LEAVE_BTN_ID: str = "queue_v2:leave"
QUEUE_SIZE:   int = 10
WAITING_ROOM_NAME: str = "Waiting Room"
QUEUE_ROLE_NAME:   str = "En Queue"


async def _grant_queue_role(member: discord.Member) -> str | None:
    role = discord.utils.get(member.guild.roles, name=QUEUE_ROLE_NAME)
    if role is None:
        return f"⚠️ Role **{QUEUE_ROLE_NAME}** introuvable sur le serveur."
    if role in member.roles:
        return None
    try:
        await member.add_roles(role, reason="Joined queue")
    except discord.Forbidden:
        return f"⚠️ Permissions insuffisantes pour ajouter le role **{QUEUE_ROLE_NAME}**."
    except discord.HTTPException:
        return None
    return None


async def _revoke_queue_role(member: discord.Member) -> None:
    role = discord.utils.get(member.guild.roles, name=QUEUE_ROLE_NAME)
    if role is None or role not in member.roles:
        return
    try:
        await member.remove_roles(role, reason="Left queue")
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _grant_match_role(member: discord.Member, role_name: str) -> None:
    """Donne le role correspondant a la categorie de match (e.g. "Match #1")."""
    role = discord.utils.get(member.guild.roles, name=role_name)
    if role is None or role in member.roles:
        return
    try:
        await member.add_roles(role, reason=f"Match formed in {role_name}")
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _revoke_match_role(member: discord.Member, role_name: str) -> None:
    role = discord.utils.get(member.guild.roles, name=role_name)
    if role is None or role not in member.roles:
        return
    try:
        await member.remove_roles(role, reason="Match ended")
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _move_to_waiting_room(member: discord.Member) -> str | None:
    """Deplace `member` dans le salon vocal "Waiting Room" si possible.

    Retourne un message d'info pour le joueur, ou None si tout s'est bien passe
    silencieusement. Discord n'autorise le deplacement que si le membre est deja
    connecte a un salon vocal du serveur.
    """
    waiting = discord.utils.get(member.guild.voice_channels, name=WAITING_ROOM_NAME)
    if waiting is None:
        return None

    voice_state = member.voice
    if voice_state is None or voice_state.channel is None:
        return f"ℹ️ Connecte-toi a un salon vocal pour etre deplace dans **{WAITING_ROOM_NAME}**."

    if voice_state.channel.id == waiting.id:
        return None

    try:
        await member.move_to(waiting, reason="Auto-move queue join")
    except discord.Forbidden:
        return f"⚠️ Permissions insuffisantes pour te deplacer dans **{WAITING_ROOM_NAME}**."
    except discord.HTTPException:
        return None
    return None


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
_LOCKS_MAXSIZE: int = 128


class QueueView(discord.ui.View):
    """View persistante : Rejoindre / Quitter."""

    def __init__(self, db, on_full=None) -> None:
        super().__init__(timeout=None)
        self.db        = db
        self._on_full  = on_full
        # OrderedDict + LRU bornee pour eviter une fuite memoire sur bot
        # multi-guilds longue duree (1 Lock par guild_id, jamais purge).
        # Une eviction du dict ne libere pas un Lock detenu : la coroutine
        # qui l'utilise en garde une reference forte.
        self._locks: OrderedDict[int, asyncio.Lock] = OrderedDict()

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
            while len(self._locks) > _LOCKS_MAXSIZE:
                self._locks.popitem(last=False)
        else:
            self._locks.move_to_end(guild_id)
        return lock

    @discord.ui.button(
        label="Rejoindre", style=discord.ButtonStyle.success, custom_id=JOIN_BTN_ID,
    )
    async def join_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        # Acquitte tout de suite : sous contention du lock par-guild, le token
        # d'interaction (3s) peut expirer avant qu'on reponde -> 10062.
        try:
            await inter.response.defer()
        except discord.NotFound:
            return

        async with self._lock(inter.guild_id):
            # 1) compte Riot lie ?
            riot = repository.get_riot_account(self.db, inter.guild_id, inter.user.id)
            if not riot:
                await inter.followup.send(
                    "❌ Lie d'abord ton compte Riot avec `/link-riot Pseudo#TAG`.",
                    ephemeral=True,
                )
                return

            # 2) ajout en base
            res = repository.add_player_to_queue(
                self.db, inter.guild_id, inter.user.id,
            )
            if not res.success:
                await inter.followup.send(_join_error_message(res.reason), ephemeral=True)
                return

            # 3) si la queue est maintenant pleine -> on ferme + trigger formation
            queue_doc = res.queue
            full = len(queue_doc.get("players", [])) >= QUEUE_SIZE
            if full:
                repository.close_active_queue(self.db, inter.guild_id)
                queue_doc = repository.get_active_queue(self.db, inter.guild_id)

            # 4) edit du message
            embed = build_queue_embed(queue_doc, inter.guild)
            await inter.edit_original_response(embed=embed, view=self)

            # 4b) auto-move dans le salon vocal "Waiting Room"
            move_notice = None
            role_notice = None
            if isinstance(inter.user, discord.Member):
                move_notice = await _move_to_waiting_room(inter.user)
                role_notice = await _grant_queue_role(inter.user)

            # 4c) confirmation ephemere (visible uniquement par le joueur)
            count = len(queue_doc.get("players", []))
            confirm = f"✅ Tu as rejoint la queue ({count}/{QUEUE_SIZE})"
            if move_notice:
                confirm += f"\n{move_notice}"
            if role_notice:
                confirm += f"\n{role_notice}"
            await inter.followup.send(confirm, ephemeral=True)

            # 5) trigger formation (Phase 4 — on a un point d'extension)
            if full and self._on_full:
                asyncio.create_task(self._safe_on_full(inter, queue_doc))

    async def _safe_on_full(
        self, inter: discord.Interaction, queue_doc: dict,
    ) -> None:
        """Invoque `_on_full` en garantissant la liberation de la queue
        en cas d'exception non capturee, sinon la queue reste en status
        'forming' et bloque toute nouvelle entree."""
        try:
            await self._on_full(inter, queue_doc)
        except Exception:
            logger.exception("[queue_v2] _safe_on_full a leve")
            try:
                repository.delete_active_queue(self.db, inter.guild_id)
            except Exception as cleanup_err:
                logger.exception("[queue_v2] cleanup apres on_full a leve")
            try:
                channel = inter.channel
                if channel is not None:
                    await channel.send(
                        f"❌ Erreur lors de la formation du match : `{e}`. "
                        "La queue a ete liberee, retentez avec /setup-queue.",
                    )
            except Exception:
                pass

    @discord.ui.button(
        label="Quitter", style=discord.ButtonStyle.danger, custom_id=LEAVE_BTN_ID,
    )
    async def leave_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        try:
            await inter.response.defer()
        except discord.NotFound:
            return

        async with self._lock(inter.guild_id):
            res = repository.remove_player_from_queue(
                self.db, inter.guild_id, inter.user.id,
            )
            if not res.success:
                await inter.followup.send(_leave_error_message(res.reason), ephemeral=True)
                return
            embed = build_queue_embed(res.queue, inter.guild)
            await inter.edit_original_response(embed=embed, view=self)
            if isinstance(inter.user, discord.Member):
                await _revoke_queue_role(inter.user)


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

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Quand un joueur quitte le serveur (kick, ban, leave), le retirer
        de la queue active s'il y est. Sans ce handler, sa place reste
        reservee et la queue se bloque a 9/10 jusqu'a ce qu'un admin
        force un reset via /close-queue + /setup-queue."""
        try:
            await asyncio.to_thread(
                repository.remove_player_from_queue,
                self.db, member.guild.id, member.id,
            )
        except Exception as e:
            logger.exception("[queue_v2] on_member_remove a leve")

    @app_commands.command(name="setup-queue", description="Pose le message de queue dans ce salon")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_queue(self, interaction: discord.Interaction) -> None:
        # Reset de la queue precedente s'il y en avait une
        repository.delete_active_queue(self.db, interaction.guild_id)

        await self.post_queue_message(interaction.channel)

        await interaction.response.send_message(
            f"✅ Queue active dans {interaction.channel.mention} !",
            ephemeral=True,
        )

    async def post_queue_message(self, channel: discord.TextChannel) -> None:
        """Pose un nouveau message de queue dans `channel` et l'enregistre.

        Utilise par /setup-queue ET par le cog match apres formation
        d'un match (pour qu'une nouvelle queue soit immediatement disponible).
        """
        embed = build_queue_embed(None, channel.guild)
        msg = await channel.send(embed=embed, view=self.view)
        repository.setup_active_queue(
            self.db,
            guild_id=channel.guild.id,
            channel_id=channel.id,
            message_id=msg.id,
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
