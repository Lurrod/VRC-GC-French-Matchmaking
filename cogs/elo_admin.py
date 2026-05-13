"""
Cog ELO admin : /win, /lose, /elomodify, /winmodify, /losemodify, /resetelo,
/reset-queue, /stats, /leaderboard. Extrait de bot.py (refactor monolithe).

Commandes admin reservees a manage_guild OU role bypass.
`/stats` est public (visible par tous).
`/leaderboard` est public dans #leaderboard, ephemere ailleurs.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import ReturnDocument

from services import elo_calc, repository
from services.leaderboard_refresh import (
    build_leaderboard_payload,
    refresh_leaderboard_channel,
)

logger = logging.getLogger(__name__)


ELO_START = elo_calc.ELO_START

# Pondération ELO par position de joueur (slot 1..5) pour /win et /lose.
# Le premier slot encaisse le plus gros gain / la plus petite perte.
WIN_DELTAS_BY_SLOT:  tuple[int, ...] = (20, 18, 17, 16, 15)
LOSE_DELTAS_BY_SLOT: tuple[int, ...] = (10, 10, 12, 13, 15)

# Mapping queue_type -> nom de salon ou poser le message persistant.
# (Duplique de cogs/admin.py volontairement pour eviter une dependance
# inter-cogs : ce mapping est tres stable, et la duplication evite un
# `from cogs.admin import ...` qui creerait un cycle d'import.)
QUEUE_CHANNEL_FOR_TYPE = {"pro": "pro-queue", "open": "open-queue", "gc": "gc-queue"}

_QUEUE_CHOICES = [
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC", value="gc"),
]


def _has_access(interaction: discord.Interaction, db) -> bool:
    """Admin (manage_guild) OU role bypass configure via /bypass."""
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = repository.get_bypass_role(db, interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))


def _get_player(col, member: discord.Member, queue_type: str):
    return repository.get_or_create_player(
        col, member.id, queue_type, member.display_name, initial_elo=ELO_START,
    )


def _match_elo_for_member(db, guild_id: int, user_id: int, queue_type: str) -> int:
    """ELO serveur du joueur dans la queue donnee, fallback ELO_REFERENCE."""
    doc = repository.get_elo_col(db, guild_id).find_one(
        {"_id": repository.player_doc_id(user_id, queue_type)}
    )
    if doc and doc.get("elo") is not None:
        return int(doc["elo"])
    return elo_calc.ELO_REFERENCE


def _compute_match_change_for_members(
    db, guild_id: int, members: list, queue_type: str,
) -> tuple[int, int, int]:
    """(avg_elo, gain, loss) pour la liste de joueurs dans la queue."""
    elos = [_match_elo_for_member(db, guild_id, m.id, queue_type) for m in members]
    avg  = round(sum(elos) / len(elos)) if elos else elo_calc.ELO_REFERENCE
    gain, loss = elo_calc.compute_match_elo_change(avg)
    return avg, gain, loss


async def _refresh_leaderboard_safe(guild: discord.Guild | None, db, queue_type: str) -> None:
    """Rafraichit le leaderboard de la queue donnee dans `#leaderboard`."""
    if guild is None:
        return
    try:
        await refresh_leaderboard_channel(guild, db, queue_type)
    except Exception:
        logger.exception("[leaderboard] refresh a leve")


def _is_leaderboard_channel(interaction: discord.Interaction) -> bool:
    chan = interaction.channel
    name = getattr(chan, "name", "") or ""
    return "leaderboard" in name.lower()


class _ResetQueueConfirmView(discord.ui.View):
    """Bouton de confirmation interactif pour /reset-queue."""

    def __init__(self, queue_type: str, *, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.queue_type = queue_type
        self.confirmed = False

    @discord.ui.button(label="Confirmer le reset", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()


class ELOAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db

    # ── /win ───────────────────────────────────────────────────
    @app_commands.command(name="win", description="Enregistre une victoire dans une queue (Pro=flat 16, autres=pondere)")
    @app_commands.describe(
        queue="Type de queue",
        joueur1="Joueur gagnant 1",
        joueur2="Joueur gagnant 2",
        joueur3="Joueur gagnant 3",
        joueur4="Joueur gagnant 4",
        joueur5="Joueur gagnant 5",
    )
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def win(
        self,
        interaction: discord.Interaction,
        queue: str,
        joueur1: discord.Member,
        joueur2: discord.Member = None,
        joueur3: discord.Member = None,
        joueur4: discord.Member = None,
        joueur5: discord.Member = None,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("Pas la permission.", ephemeral=True)
            return
        players = [p for p in [joueur1, joueur2, joueur3, joueur4, joueur5] if p is not None]
        col = repository.get_elo_col(self.db, interaction.guild_id)

        if queue == "pro":
            deltas = [16] * len(players)
            desc = "Pro Queue : +16 a plat pour chaque gagnant."
        else:
            deltas = list(WIN_DELTAS_BY_SLOT)[:len(players)]
            avg_elo, _, _ = _compute_match_change_for_members(
                self.db, interaction.guild_id, players, queue,
            )
            desc = f"Avg ELO du groupe : **{avg_elo}** -> gains ponderes par position."

        embed = discord.Embed(
            title=f"Resultats {queue.upper()} - Victoire enregistree !",
            description=desc,
            color=0x2ecc71,
            timestamp=datetime.now(UTC),
        )
        for slot, member in enumerate(players):
            gain = deltas[slot]
            _get_player(col, member, queue)
            old_doc = col.find_one_and_update(
                {"_id": repository.player_doc_id(member.id, queue)},
                {"$inc": {"elo": gain, "wins": 1}},
                return_document=ReturnDocument.BEFORE,
            )
            old = (old_doc or {}).get("elo", 0)
            new = old + gain
            embed.add_field(
                name=member.display_name,
                value=f"+{gain} ELO -> **{new}** *(etait {old})*",
                inline=False,
            )
        embed.set_footer(text=f"Enregistre par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /lose ──────────────────────────────────────────────────
    @app_commands.command(name="lose", description="Enregistre une defaite dans une queue (Pro=flat 16, autres=pondere)")
    @app_commands.describe(
        queue="Type de queue",
        joueur1="Joueur perdant 1",
        joueur2="Joueur perdant 2",
        joueur3="Joueur perdant 3",
        joueur4="Joueur perdant 4",
        joueur5="Joueur perdant 5",
    )
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def lose(
        self,
        interaction: discord.Interaction,
        queue: str,
        joueur1: discord.Member,
        joueur2: discord.Member = None,
        joueur3: discord.Member = None,
        joueur4: discord.Member = None,
        joueur5: discord.Member = None,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("Pas la permission.", ephemeral=True)
            return
        players = [p for p in [joueur1, joueur2, joueur3, joueur4, joueur5] if p is not None]
        col = repository.get_elo_col(self.db, interaction.guild_id)

        if queue == "pro":
            deltas = [16] * len(players)
            desc = "Pro Queue : -16 a plat pour chaque perdant."
        else:
            deltas = list(LOSE_DELTAS_BY_SLOT)[:len(players)]
            avg_elo, _, _ = _compute_match_change_for_members(
                self.db, interaction.guild_id, players, queue,
            )
            desc = f"Avg ELO du groupe : **{avg_elo}** -> pertes ponderees par position."

        embed = discord.Embed(
            title=f"Resultats {queue.upper()} - Defaite enregistree !",
            description=desc,
            color=0xe74c3c,
            timestamp=datetime.now(UTC),
        )
        for slot, member in enumerate(players):
            loss = deltas[slot]
            _get_player(col, member, queue)
            old_doc = col.find_one_and_update(
                {"_id": repository.player_doc_id(member.id, queue)},
                [{"$set": {
                    "elo": {"$max": [0, {"$subtract": [{"$ifNull": ["$elo", 0]}, loss]}]},
                    "losses": {"$add": [{"$ifNull": ["$losses", 0]}, 1]},
                }}],
                return_document=ReturnDocument.BEFORE,
            )
            old = (old_doc or {}).get("elo", 0)
            new = max(0, old - loss)
            embed.add_field(
                name=member.display_name,
                value=f"-{loss} ELO -> **{new}** (etait {old})",
                inline=False,
            )
        embed.set_footer(text=f"Enregistre par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /leaderboard ───────────────────────────────────────────
    @app_commands.command(name="leaderboard", description="Affiche le classement ELO d'une queue")
    @app_commands.describe(queue="Type de queue")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def leaderboard(self, interaction: discord.Interaction, queue: str):
        public = _is_leaderboard_channel(interaction)
        ephemeral = not public
        await interaction.response.defer(ephemeral=ephemeral)
        file, view = await build_leaderboard_payload(interaction.guild, self.db, queue)
        if file is None:
            await interaction.followup.send(
                f"Aucun joueur enregistre en {queue.upper()} Queue.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(file=file, view=view, ephemeral=ephemeral)

    # ── /resetelo ──────────────────────────────────────────────
    @app_commands.command(name="resetelo", description=f"Remet l'ELO d'un joueur (ou de tous) a {ELO_START} dans une queue")
    @app_commands.describe(
        queue="Type de queue",
        joueur="Le joueur a remettre a la valeur initiale",
        all_players=f"Remettre l'ELO de tous les joueurs de cette queue a {ELO_START}",
    )
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.rename(all_players="all")
    async def resetelo(
        self,
        interaction: discord.Interaction,
        queue: str,
        joueur: discord.Member = None,
        all_players: bool = False,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("Pas la permission.", ephemeral=True)
            return
        col = repository.get_elo_col(self.db, interaction.guild_id)
        if all_players:
            count = col.count_documents({"queue_type": queue})
            col.update_many(
                {"queue_type": queue},
                {"$set": {"elo": ELO_START, "wins": 0, "losses": 0}},
            )
            embed = discord.Embed(
                title=f"🔄 Reset général {queue.upper()} !",
                description=f"ELO de **{count} joueur(s)** remis a {ELO_START} dans la queue {queue.upper()}.",
                color=0xe74c3c,
                timestamp=datetime.now(UTC),
            )
            embed.set_footer(text=f"Reset par {interaction.user.display_name}")
            await interaction.response.send_message(embed=embed)
            await _refresh_leaderboard_safe(interaction.guild, self.db, queue)
            return
        if joueur is None:
            await interaction.response.send_message("Mentionne un joueur ou utilise `all:True`.", ephemeral=True)
            return
        doc = _get_player(col, joueur, queue)
        old = doc["elo"]
        col.update_one(
            {"_id": repository.player_doc_id(joueur.id, queue)},
            {"$set": {"elo": ELO_START, "wins": 0, "losses": 0}},
        )
        embed = discord.Embed(
            title=f"🔄 ELO {queue.upper()} réinitialisé !",
            color=0x95a5a6,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Joueur", value=joueur.mention, inline=True)
        embed.add_field(name="Ancien ELO", value=str(old), inline=True)
        embed.add_field(name="Nouvel ELO", value=str(ELO_START), inline=True)
        embed.set_thumbnail(url=joueur.display_avatar.url)
        embed.set_footer(text=f"Reset par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /reset-queue ───────────────────────────────────────────
    @app_commands.command(name="reset-queue", description="Drop toutes les donnees d'une queue (admin)")
    @app_commands.describe(queue="Type de queue a reset")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset_queue(self, interaction: discord.Interaction, queue: str):
        view = _ResetQueueConfirmView(queue_type=queue)
        embed = discord.Embed(
            title=f"⚠️ Reset {queue.upper()} Queue",
            description=(
                f"Cette action va **supprimer définitivement** :\n"
                f"- Tous les ELO de la queue {queue.upper()}\n"
                f"- L'historique des matchs de la queue {queue.upper()}\n"
                f"- L'état du leaderboard de la queue {queue.upper()}\n\n"
                f"Les autres queues ne sont pas touchées. **Confirmer ?**"
            ),
            color=0xe74c3c,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            await interaction.followup.send(
                "Reset annulé (timeout ou non-confirmé).", ephemeral=True,
            )
            return

        elo_col = repository.get_elo_col(self.db, interaction.guild_id)
        elo_col.delete_many({"queue_type": queue})
        repository.delete_active_queue(self.db, interaction.guild_id, queue)
        matches_col = repository.get_matches_col(self.db, interaction.guild_id)
        matches_col.delete_many({"queue_type": queue})
        repository.clear_leaderboard_message_id(self.db, interaction.guild_id, queue)

        # Re-poser le message de queue dans le bon salon
        queue_cog = self.bot.get_cog("QueueCog")
        target_name = QUEUE_CHANNEL_FOR_TYPE[queue]
        target_chan = discord.utils.get(interaction.guild.text_channels, name=target_name)
        if queue_cog and target_chan:
            try:
                await queue_cog.post_queue_message(target_chan, queue)  # type: ignore[attr-defined]
            except Exception:
                logger.exception("[reset-queue] re-post queue a leve")

        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

        audit = discord.Embed(
            title=f"🔄 Queue {queue.upper()} reset",
            description=f"Reset effectue par {interaction.user.mention}",
            color=0x2ecc71,
            timestamp=datetime.now(UTC),
        )
        try:
            await interaction.channel.send(embed=audit)
        except Exception:
            logger.exception("[reset-queue] audit log a leve")
        await interaction.followup.send(
            f"✅ Queue {queue.upper()} reset.", ephemeral=True,
        )

    @reset_queue.error
    async def _reset_queue_perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Réservé aux administrateurs.", ephemeral=True,
            )

    # ── /elomodify ─────────────────────────────────────────────
    @app_commands.command(name="elomodify", description="Ajoute ou enleve de l'ELO a un joueur dans une queue")
    @app_commands.describe(queue="Type de queue", joueur="Le joueur", action="Ajouter ou enlever", montant="Nombre d'ELO")
    @app_commands.choices(
        queue=_QUEUE_CHOICES,
        action=[
            app_commands.Choice(name="+ Ajouter", value="add"),
            app_commands.Choice(name="- Enlever", value="remove"),
        ],
    )
    async def elomodify(self, interaction: discord.Interaction, queue: str, joueur: discord.Member, action: str, montant: int):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("Pas la permission.", ephemeral=True)
            return
        if montant <= 0:
            await interaction.response.send_message(
                "❌ Le montant doit etre strictement positif. Utilise l'action `- Enlever` pour retirer de l'ELO.",
                ephemeral=True,
            )
            return
        col = repository.get_elo_col(self.db, interaction.guild_id)
        _get_player(col, joueur, queue)
        delta = montant if action == "add" else -montant
        old_doc = col.find_one_and_update(
            {"_id": repository.player_doc_id(joueur.id, queue)},
            [{"$set": {"elo": {"$max": [0, {"$add": [{"$ifNull": ["$elo", 0]}, delta]}]}}}],
            return_document=ReturnDocument.BEFORE,
        )
        old = (old_doc or {}).get("elo", 0)
        new = max(0, old + delta)
        if action == "add":
            color = 0x2ecc71
            label = f"+{montant}"
            title = f"➕ ELO {queue.upper()} ajouté"
        else:
            color = 0xe74c3c
            label = f"-{montant}"
            title = f"➖ ELO {queue.upper()} retiré"
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
        embed.add_field(name="Joueur",       value=joueur.mention,                    inline=True)
        embed.add_field(name="Modification", value=label,                             inline=True)
        embed.add_field(name="Nouvel ELO",   value=f"**{new}** (etait {old})",        inline=True)
        embed.set_footer(text=f"Par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /winmodify ─────────────────────────────────────────────
    @app_commands.command(name="winmodify", description="Ajoute ou enleve des victoires a un joueur dans une queue")
    @app_commands.describe(queue="Type de queue", joueur="Le joueur", action="Ajouter ou enlever", montant="Nombre de victoires")
    @app_commands.choices(
        queue=_QUEUE_CHOICES,
        action=[
            app_commands.Choice(name="+ Ajouter", value="add"),
            app_commands.Choice(name="- Enlever", value="remove"),
        ],
    )
    async def winmodify(self, interaction: discord.Interaction, queue: str, joueur: discord.Member, action: str, montant: int):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("Pas la permission.", ephemeral=True)
            return
        if montant <= 0:
            await interaction.response.send_message("❌ Le montant doit etre strictement positif.", ephemeral=True)
            return
        col = repository.get_elo_col(self.db, interaction.guild_id)
        _get_player(col, joueur, queue)
        delta = montant if action == "add" else -montant
        old_doc = col.find_one_and_update(
            {"_id": repository.player_doc_id(joueur.id, queue)},
            [{"$set": {"wins": {"$max": [0, {"$add": [{"$ifNull": ["$wins", 0]}, delta]}]}}}],
            return_document=ReturnDocument.BEFORE,
        )
        old = (old_doc or {}).get("wins", 0)
        new = max(0, old + delta)
        if action == "add":
            color = 0x2ecc71
            label = f"+{montant}"
            title = f"➕ Victoires {queue.upper()} ajoutées"
        else:
            color = 0xe74c3c
            label = f"-{montant}"
            title = f"➖ Victoires {queue.upper()} retirées"
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
        embed.add_field(name="Joueur",        value=joueur.mention,             inline=True)
        embed.add_field(name="Modification",  value=label,                      inline=True)
        embed.add_field(name="Nouveau total", value=f"**{new}** (etait {old})", inline=True)
        embed.set_footer(text=f"Par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /losemodify ────────────────────────────────────────────
    @app_commands.command(name="losemodify", description="Ajoute ou enleve des defaites a un joueur dans une queue")
    @app_commands.describe(queue="Type de queue", joueur="Le joueur", action="Ajouter ou enlever", montant="Nombre de defaites")
    @app_commands.choices(
        queue=_QUEUE_CHOICES,
        action=[
            app_commands.Choice(name="+ Ajouter", value="add"),
            app_commands.Choice(name="- Enlever", value="remove"),
        ],
    )
    async def losemodify(self, interaction: discord.Interaction, queue: str, joueur: discord.Member, action: str, montant: int):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("Pas la permission.", ephemeral=True)
            return
        if montant <= 0:
            await interaction.response.send_message("❌ Le montant doit etre strictement positif.", ephemeral=True)
            return
        col = repository.get_elo_col(self.db, interaction.guild_id)
        _get_player(col, joueur, queue)
        delta = montant if action == "add" else -montant
        old_doc = col.find_one_and_update(
            {"_id": repository.player_doc_id(joueur.id, queue)},
            [{"$set": {"losses": {"$max": [0, {"$add": [{"$ifNull": ["$losses", 0]}, delta]}]}}}],
            return_document=ReturnDocument.BEFORE,
        )
        old = (old_doc or {}).get("losses", 0)
        new = max(0, old + delta)
        if action == "add":
            color = 0xe74c3c
            label = f"+{montant}"
            title = f"➕ Défaites {queue.upper()} ajoutées"
        else:
            color = 0x2ecc71
            label = f"-{montant}"
            title = f"➖ Défaites {queue.upper()} retirées"
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
        embed.add_field(name="Joueur", value=joueur.mention, inline=True)
        embed.add_field(name="Modification", value=label, inline=True)
        embed.add_field(name="Nouveau total", value=f"**{new}** (etait {old})", inline=True)
        embed.set_footer(text=f"Par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /stats ─────────────────────────────────────────────────
    @app_commands.command(name="stats", description="Affiche les statistiques ELO d'un joueur dans une queue")
    @app_commands.describe(queue="Type de queue", joueur="Le joueur dont tu veux voir les stats")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def stats(self, interaction: discord.Interaction, queue: str, joueur: discord.Member = None):
        if joueur is None:
            joueur = interaction.user
        col = repository.get_elo_col(self.db, interaction.guild_id)
        doc_id = repository.player_doc_id(joueur.id, queue)
        doc = col.find_one({"_id": doc_id})
        if not doc:
            await interaction.response.send_message(
                f"{joueur.display_name} n'a pas encore joue en {queue.upper()} Queue.",
                ephemeral=True,
            )
            return
        elo     = doc["elo"]
        wins    = doc.get("wins", 0)
        losses  = doc.get("losses", 0)
        total   = wins + losses
        winrate = round((wins / total) * 100, 1) if total > 0 else 0
        rank = col.count_documents({
            "queue_type": queue,
            "$or": [
                {"elo": {"$gt": elo}},
                {"elo": elo, "wins": {"$gt": wins}},
                {"elo": elo, "wins": wins, "_id": {"$lt": doc_id}},
            ],
        }) + 1
        embed = discord.Embed(title=f"📊 Stats {queue.upper()} de {joueur.display_name}", color=0x3498db, timestamp=datetime.now(UTC))
        embed.set_thumbnail(url=joueur.display_avatar.url)
        embed.add_field(name="🏅 ELO",       value=f"**{elo}**",            inline=True)
        embed.add_field(name="🏆 Rang",      value=f"**#{rank}**",          inline=True)
        embed.add_field(name="📈 Winrate",   value=f"**{winrate}%**",       inline=True)
        embed.add_field(name="✅ Victoires", value=f"**{wins}**",           inline=True)
        embed.add_field(name="❌ Défaites",  value=f"**{losses}**",         inline=True)
        embed.add_field(name="🎮 Parties",   value=f"**{total}**",          inline=True)
        embed.set_footer(text=interaction.guild.name)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot, db) -> None:
    await bot.add_cog(ELOAdminCog(bot, db))
