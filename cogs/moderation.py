"""
Cog moderation : commandes /warn et /warn-list.

Reserve aux roles de moderation (Head Administrators, Administrators,
Modo Pro Queue, Moderators, THE HUB) OU aux membres avec la permission
Discord manage_guild. Les warns sont stockes par guild dans la collection
`warns_{guild_id}`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from services import repository

logger = logging.getLogger(__name__)


WARN_ROLE_NAMES: tuple[str, ...] = (
    "Head Administrators",
    "Administrators",
    "Modo Pro Queue",
    "Moderators",
    "THE HUB",
)

WARN_MESSAGE = (
    "Vous venez de recevoir un warn, au prochain, vous serez sanctionner."
)

WARN_LIST_PAGE_SIZE = 10


def _has_warn_access(user: discord.Member) -> bool:
    """manage_guild OU role dont le nom est dans WARN_ROLE_NAMES."""
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and getattr(perms, "manage_guild", False):
        return True
    return any(r.name in WARN_ROLE_NAMES for r in getattr(user, "roles", []))


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db

    # ── /warn ──────────────────────────────────────────────────
    @app_commands.command(
        name="warn",
        description="Avertit un utilisateur par DM avec une raison.",
    )
    @app_commands.describe(
        member="Le membre a avertir",
        reason="La raison du warn",
    )
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
    ) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ Commande utilisable uniquement dans un serveur.",
                ephemeral=True,
            )
            return

        if not _has_warn_access(interaction.user):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission d'utiliser cette commande.",
                ephemeral=True,
            )
            return

        if member.bot:
            await interaction.response.send_message(
                "❌ Impossible de warn un bot.",
                ephemeral=True,
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ Tu ne peux pas te warn toi-meme.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="⚠️ Avertissement",
            description=WARN_MESSAGE,
            color=0xE74C3C,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Raison", value=reason, inline=False)
        if interaction.guild is not None:
            embed.set_footer(text=f"Serveur : {interaction.guild.name}")

        dm_failed = False
        try:
            await member.send(embed=embed)
        except discord.Forbidden:
            dm_failed = True
            logger.info(
                "[warn] DM fermes pour %s (id=%s) — warn par %s",
                member.display_name,
                member.id,
                interaction.user.display_name,
            )
        except discord.HTTPException:
            logger.exception("[warn] echec envoi DM a %s", member.id)
            await interaction.response.send_message(
                f"❌ Echec de l'envoi du DM a {member.mention}.",
                ephemeral=True,
            )
            return

        try:
            repository.add_warn(
                self.db,
                interaction.guild_id,
                member_id=member.id,
                member_name=member.display_name,
                moderator_id=interaction.user.id,
                moderator_name=interaction.user.display_name,
                reason=reason,
            )
        except Exception:
            logger.exception("[warn] echec persistance MongoDB pour %s", member.id)
            await interaction.response.send_message(
                "❌ Erreur lors de l'enregistrement du warn.",
                ephemeral=True,
            )
            return

        if dm_failed:
            await interaction.response.send_message(
                f"⚠️ Warn enregistre pour {member.mention} mais DM impossible "
                f"(DM fermes).\n**Raison :** {reason}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"✅ {member.mention} a recu un warn.\n**Raison :** {reason}",
                ephemeral=True,
            )

        logger.info(
            "[warn] %s a warn %s — raison: %s",
            interaction.user.display_name,
            member.display_name,
            reason,
        )

    # ── /warn-list ──────────────────────────────────────────────
    @app_commands.command(
        name="warn-list",
        description="Affiche la liste des warns envoyes sur ce serveur.",
    )
    @app_commands.describe(
        member="Filtrer par membre (optionnel)",
    )
    async def warn_list(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ Commande utilisable uniquement dans un serveur.",
                ephemeral=True,
            )
            return

        if not _has_warn_access(interaction.user):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission d'utiliser cette commande.",
                ephemeral=True,
            )
            return

        warns = repository.list_warns(
            self.db,
            interaction.guild_id,
            member_id=member.id if member is not None else None,
            limit=WARN_LIST_PAGE_SIZE,
        )

        title = (
            f"📋 Warns de {member.display_name}"
            if member is not None
            else "📋 Warns du serveur"
        )

        if not warns:
            empty_msg = (
                f"Aucun warn enregistre pour {member.mention}."
                if member is not None
                else "Aucun warn enregistre sur ce serveur."
            )
            embed = discord.Embed(
                title=title,
                description=empty_msg,
                color=0x95A5A6,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=title,
            color=0xE74C3C,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(
            text=f"{len(warns)} warn(s) affiche(s) (max {WARN_LIST_PAGE_SIZE})",
        )

        for warn in warns:
            ts = warn.get("timestamp")
            ts_str = (
                f"<t:{int(ts.timestamp())}:f>" if isinstance(ts, datetime) else "?"
            )
            target = f"<@{warn['member_id']}>"
            moderator = warn.get("moderator_name", "?")
            reason = _truncate(str(warn.get("reason", "")), 200)
            embed.add_field(
                name=f"{ts_str} — {target}",
                value=f"**Par :** {moderator}\n**Raison :** {reason}",
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot, db) -> None:
    await bot.add_cog(ModerationCog(bot, db))
