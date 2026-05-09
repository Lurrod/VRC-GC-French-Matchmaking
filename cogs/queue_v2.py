"""
Cog V2 : queues 10mans avec boutons persistants (Rejoindre / Quitter).

3 queues simultanees par guild :
  - Pro Queue : reserve aux joueurs avec le role "Rank S | Pro Queue".
  - Open Queue : sans gate de role.
  - GC Queue : reserve aux joueurs avec le role "GC".

Invariants :
  - Un joueur ne peut etre que dans UNE queue a la fois (single-queue lock).
  - Chaque queue a son salon vocal "Waiting Room" dedie.
  - Les custom_ids des boutons portent le `queue_type` pour permettre la
    cohabitation des 3 messages persistants apres restart du bot.

Flux :
  1. Admin lance /setup-queue queue:<Pro|Open|GC> dans un salon -> message
     persistant pose pour ce type.
  2. Joueurs cliquent "Rejoindre" / "Quitter".
     - Refus si pas de compte Riot lie.
     - Refus si deja dans un match en cours.
     - Refus si deja dans une autre queue.
     - Refus si role gate non satisfait.
  3. A 10 joueurs : status passe a "forming", _on_full() est appele avec
     `queue_type` pour permettre au cog match de propager l'info.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services import repository


# Roles "Match #1", "Match #2", "Match #3" attribues a un joueur en cours
# de match. Tant qu'un joueur a un de ces roles, il est dans un match
# pending (vote non termine) — on lui refuse l'entree dans une nouvelle
# queue. Le role est retire des le vote valide, donc le joueur peut
# rejoindre la queue sans delai. "Match Host" n'est PAS gate.
_MATCH_ROLE_PATTERN = re.compile(r"^Match #\d+$")


def _has_match_role(member: discord.Member) -> str | None:
    """Renvoie le nom du role 'Match #N' du membre, ou None s'il n'en a pas."""
    for role in member.roles:
        if _MATCH_ROLE_PATTERN.match(role.name):
            return role.name
    return None


logger = logging.getLogger(__name__)


# ── Constantes par queue_type ─────────────────────────────────────
# Salons vocaux "Waiting Room" dedies par queue.
WAITING_ROOM_NAMES: dict[str, str] = {
    "pro":  "Waiting Room Pro",
    "open": "Waiting Room Open",
    "gc":   "Waiting Room GC",
}

# Role required pour rejoindre une queue gated. None = pas de gate.
QUEUE_ROLE_GATES: dict[str, str | None] = {
    "pro":  "Rank S | Pro Queue",
    "open": None,
    "gc":   "GC",
}

# Nom du salon textuel attendu pour chaque queue (utilise par /setup
# pour pre-poster les messages dans les bons salons).
QUEUE_CHANNEL_NAMES: dict[str, str] = {
    "pro":  "pro-queue",
    "open": "open-queue",
    "gc":   "gc-queue",
}

# Label affiche dans le titre de l'embed.
QUEUE_LABELS: dict[str, str] = {
    "pro":  "Pro Queue",
    "open": "Open Queue",
    "gc":   "GC Queue",
}

QUEUE_ROLE_NAME: str = "En Queue"  # role global, partage entre les 3 queues
QUEUE_SIZE:      int = 10


# ── Roles helpers (inchanges) ─────────────────────────────────────
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


async def _move_to_waiting_room(
    member: discord.Member, queue_type: str,
) -> str | None:
    """Deplace `member` dans le salon vocal "Waiting Room <type>" si possible.

    Retourne un message d'info pour le joueur, ou None si tout s'est bien passe
    silencieusement. Discord n'autorise le deplacement que si le membre est deja
    connecte a un salon vocal du serveur.
    """
    waiting_name = WAITING_ROOM_NAMES[queue_type]
    waiting = discord.utils.get(member.guild.voice_channels, name=waiting_name)
    if waiting is None:
        return None

    voice_state = member.voice
    if voice_state is None or voice_state.channel is None:
        return f"ℹ️ Connecte-toi a un salon vocal pour etre deplace dans **{waiting_name}**."

    if voice_state.channel.id == waiting.id:
        return None

    try:
        await member.move_to(waiting, reason=f"Auto-move queue join ({queue_type})")
    except discord.Forbidden:
        return f"⚠️ Permissions insuffisantes pour te deplacer dans **{waiting_name}**."
    except discord.HTTPException:
        return None
    return None


# ── Embed builder ─────────────────────────────────────────────────
def build_queue_embed(
    queue_doc: dict | None, guild: discord.Guild, queue_type: str,
) -> discord.Embed:
    label = QUEUE_LABELS[queue_type]
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
        title=f"🎮 {label} 10mans — {count}/{QUEUE_SIZE}",
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
    """View persistante par `queue_type`. Custom IDs distincts pour cohabiter.

    Les boutons sont crees manuellement (pas via `@discord.ui.button`)
    parce que le `custom_id` doit dependre de `queue_type` connu au
    runtime, pas du decorateur fige a l'import du module.
    """

    def __init__(self, db, queue_type: str, on_full=None) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.queue_type = queue_type
        self._on_full = on_full
        # OrderedDict + LRU bornee pour eviter une fuite memoire sur bot
        # multi-guilds longue duree (1 Lock par guild_id, jamais purge).
        self._locks: OrderedDict[int, asyncio.Lock] = OrderedDict()

        # Boutons a custom_id dynamique (per-instance).
        join = discord.ui.Button(
            label="Rejoindre",
            style=discord.ButtonStyle.success,
            custom_id=f"queue_v2:join:{queue_type}",
        )
        join.callback = self._join_callback
        self.join_btn = join
        self.add_item(join)

        leave = discord.ui.Button(
            label="Quitter",
            style=discord.ButtonStyle.danger,
            custom_id=f"queue_v2:leave:{queue_type}",
        )
        leave.callback = self._leave_callback
        self.leave_btn = leave
        self.add_item(leave)

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

    def _has_required_role(
        self, member: discord.Member,
    ) -> tuple[bool, str | None]:
        """Renvoie (has_role, role_name_required_or_None_if_no_gate)."""
        required = QUEUE_ROLE_GATES.get(self.queue_type)
        if required is None:
            return True, None
        if any(r.name == required for r in member.roles):
            return True, required
        return False, required

    async def _join_callback(self, inter: discord.Interaction):
        # Acquitte tout de suite : sous contention du lock par-guild, le token
        # d'interaction (3s) peut expirer avant qu'on reponde -> 10062.
        try:
            await inter.response.defer()
        except discord.NotFound:
            return

        async with self._lock(inter.guild_id):
            # 1) compte Riot lie ?
            riot = await asyncio.to_thread(
                repository.get_riot_account,
                self.db, inter.guild_id, inter.user.id,
            )
            if not riot:
                await inter.followup.send(
                    "❌ Lie d'abord ton compte Riot avec `/link-riot Pseudo#TAG`.",
                    ephemeral=True,
                )
                return

            # 2) deja dans un match en cours ?
            # Le role `Match #N` reste tant que le vote n'est pas valide ;
            # on bloque le requeue pour eviter qu'un joueur soit dans 2
            # matches simultanes (lobbies eclates, equipes desequilibrees).
            if isinstance(inter.user, discord.Member):
                ongoing_role = _has_match_role(inter.user)
                if ongoing_role is not None:
                    await inter.followup.send(
                        f"❌ Tu es deja dans un match en cours (role `{ongoing_role}`). "
                        "Attends la fin du vote pour rejoindre une nouvelle queue.",
                        ephemeral=True,
                    )
                    return

            # 3) deja dans une autre queue ?
            current = await asyncio.to_thread(
                repository.find_player_in_any_queue,
                self.db, inter.guild_id, inter.user.id,
            )
            if current is not None and current != self.queue_type:
                await inter.followup.send(
                    f"❌ Tu es deja dans la queue **{current.upper()}**. "
                    "Quitte-la d'abord pour rejoindre une autre queue.",
                    ephemeral=True,
                )
                return

            # 4) gate de role pour la queue
            if isinstance(inter.user, discord.Member):
                ok, required = self._has_required_role(inter.user)
                if not ok:
                    await inter.followup.send(
                        f"❌ Cette queue est reservee aux joueurs avec le role "
                        f"**{required}** (Pro Queue / GC).",
                        ephemeral=True,
                    )
                    return

            # 5) ajout en base
            res = await asyncio.to_thread(
                repository.add_player_to_queue,
                self.db, inter.guild_id, self.queue_type, inter.user.id,
            )
            if not res.success:
                await inter.followup.send(
                    _join_error_message(res.reason), ephemeral=True,
                )
                return

            # 6) si la queue est maintenant pleine -> on ferme + trigger formation
            queue_doc = res.queue
            full = len(queue_doc.get("players", [])) >= QUEUE_SIZE
            if full:
                await asyncio.to_thread(
                    repository.close_active_queue,
                    self.db, inter.guild_id, self.queue_type,
                )
                queue_doc = await asyncio.to_thread(
                    repository.get_active_queue,
                    self.db, inter.guild_id, self.queue_type,
                )

            # 7) edit du message
            embed = build_queue_embed(queue_doc, inter.guild, self.queue_type)
            await inter.edit_original_response(embed=embed, view=self)

            # 7b) auto-move dans le salon vocal "Waiting Room <type>"
            move_notice = None
            role_notice = None
            if isinstance(inter.user, discord.Member):
                move_notice = await _move_to_waiting_room(inter.user, self.queue_type)
                role_notice = await _grant_queue_role(inter.user)

            # 7c) confirmation ephemere (visible uniquement par le joueur)
            count = len(queue_doc.get("players", []))
            label = QUEUE_LABELS[self.queue_type]
            confirm = f"✅ Tu as rejoint la queue **{label}** ({count}/{QUEUE_SIZE})"
            if move_notice:
                confirm += f"\n{move_notice}"
            if role_notice:
                confirm += f"\n{role_notice}"
            await inter.followup.send(confirm, ephemeral=True)

            # 8) trigger formation
            if full and self._on_full:
                asyncio.create_task(self._safe_on_full(inter, queue_doc))

    async def _safe_on_full(
        self, inter: discord.Interaction, queue_doc: dict,
    ) -> None:
        """Invoque `_on_full` en garantissant la liberation de la queue
        en cas d'exception non capturee, sinon la queue reste en status
        'forming' et bloque toute nouvelle entree."""
        try:
            await self._on_full(inter, queue_doc, self.queue_type)
        except Exception as e:
            logger.exception("[queue_v2] _safe_on_full a leve")
            try:
                repository.delete_active_queue(
                    self.db, inter.guild_id, self.queue_type,
                )
            except Exception:
                logger.exception("[queue_v2] cleanup apres on_full a leve")
            user_msg = (
                f"❌ Erreur lors de la formation du match : `{e}`. "
                f"La queue {self.queue_type.upper()} a ete liberee, "
                "retentez avec /setup-queue."
            )
            channel = inter.channel
            try:
                if channel is not None:
                    await channel.send(user_msg)
                else:
                    logger.warning(
                        "[queue_v2] inter.channel is None, fallback DM "
                        "to user %s in guild %s",
                        inter.user.id, inter.guild_id,
                    )
                    if inter.user is not None:
                        try:
                            await inter.user.send(user_msg)
                        except discord.Forbidden:
                            logger.warning(
                                "[queue_v2] DM fallback bloque (Forbidden) pour user %s",
                                inter.user.id,
                            )
            except Exception:
                logger.exception("[queue_v2] notification erreur a leve")

    async def _leave_callback(self, inter: discord.Interaction):
        try:
            await inter.response.defer()
        except discord.NotFound:
            return

        async with self._lock(inter.guild_id):
            res = await asyncio.to_thread(
                repository.remove_player_from_queue,
                self.db, inter.guild_id, self.queue_type, inter.user.id,
            )
            if not res.success:
                await inter.followup.send(
                    _leave_error_message(res.reason), ephemeral=True,
                )
                return
            embed = build_queue_embed(res.queue, inter.guild, self.queue_type)
            await inter.edit_original_response(embed=embed, view=self)
            if isinstance(inter.user, discord.Member):
                # Le joueur est sorti de SA queue. S'il n'est dans aucune
                # autre queue, on retire le role global "En Queue".
                still_in = await asyncio.to_thread(
                    repository.find_player_in_any_queue,
                    self.db, inter.guild_id, inter.user.id,
                )
                if still_in is None:
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
_QUEUE_CHOICES = [
    app_commands.Choice(name="Pro",  value="pro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC",   value="gc"),
]


class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db, on_full=None) -> None:
        self.bot     = bot
        self.db      = db
        self.on_full = on_full
        # 1 view par queue_type, custom_ids distincts. Toutes branchees
        # sur le meme on_full callback (le cog match dispatchera selon
        # le queue_type passe a _safe_on_full).
        self.views: dict[str, QueueView] = {
            qt: QueueView(db, queue_type=qt, on_full=on_full)
            for qt in repository.QUEUE_TYPES
        }

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Quand un joueur quitte le serveur (kick, ban, leave), le retirer
        des queues actives (toutes, on ne sait pas dans laquelle il etait).
        Sans ce handler, sa place reste reservee et la queue se bloque a
        9/10 jusqu'a ce qu'un admin force un reset."""
        for qt in repository.QUEUE_TYPES:
            try:
                await asyncio.to_thread(
                    repository.remove_player_from_queue,
                    self.db, member.guild.id, qt, member.id,
                )
            except Exception:
                logger.exception("[queue_v2] on_member_remove a leve (qt=%s)", qt)

    @app_commands.command(
        name="setup-queue",
        description="Pose le message de queue dans ce salon",
    )
    @app_commands.describe(queue="Type de queue : Pro, Open ou GC")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_queue(
        self, interaction: discord.Interaction, queue: str,
    ) -> None:
        # Reset de la queue precedente du meme type s'il y en avait une
        repository.delete_active_queue(self.db, interaction.guild_id, queue)

        await self.post_queue_message(interaction.channel, queue)

        await interaction.response.send_message(
            f"✅ Queue **{queue.upper()}** active dans {interaction.channel.mention} !",
            ephemeral=True,
        )

    async def post_queue_message(
        self, channel: discord.TextChannel, queue_type: str,
    ) -> None:
        """Pose un nouveau message de queue dans `channel` et l'enregistre.

        Utilise par /setup-queue ET par le cog match apres formation
        d'un match (pour qu'une nouvelle queue soit immediatement
        disponible apres formation)."""
        view = self.views[queue_type]
        embed = build_queue_embed(None, channel.guild, queue_type)
        msg = await channel.send(embed=embed, view=view)
        repository.setup_active_queue(
            self.db,
            guild_id=channel.guild.id,
            queue_type=queue_type,
            channel_id=channel.id,
            message_id=msg.id,
        )

    @app_commands.command(
        name="close-queue",
        description="Ferme la queue active d'un type",
    )
    @app_commands.describe(queue="Type de queue : Pro, Open ou GC")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def close_queue(
        self, interaction: discord.Interaction, queue: str,
    ) -> None:
        deleted = repository.delete_active_queue(
            self.db, interaction.guild_id, queue,
        )
        msg = (
            f"✅ Queue {queue.upper()} supprimee."
            if deleted else f"ℹ️ Aucune queue {queue.upper()} active."
        )
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
    # Enregistre les 3 views pour qu'elles persistent apres restart.
    for view in cog.views.values():
        bot.add_view(view)
