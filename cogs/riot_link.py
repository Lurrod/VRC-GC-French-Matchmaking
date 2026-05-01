"""
Cog V2 : association compte Discord <-> compte Riot.

Commandes :
  /link-riot riot_id:Pseudo#TAG     (region forcee a EU)
  /unlink-riot

Aucun gate-keeping : la verification du rang des nouveaux membres est
faite manuellement a l'entree sur le serveur Discord.

Le link Riot seede simplement l'ELO de depart a LINK_BASE_ELO (2000)
dans `elo_<guild_id>`. Apres le seed, l'ELO Riot n'a plus aucun impact :
seuls les wins/losses du serveur modifient l'ELO.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services import repository

logger = logging.getLogger(__name__)
from services.riot_id import parse_riot_id
from services.riot_api import (
    HenrikDevClient,
    PlayerNotFound,
    RateLimited,
    RiotApiError,
)


# Serveur reserve aux EU
DEFAULT_REGION = "eu"

# ELO de depart distribuee a tout joueur qui lie son compte Riot.
# Cette valeur s'ajoute a l'ELO bot deja accumulee (matches anterieurs au link).
LINK_BASE_ELO = 2000


class RiotLinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db, riot_client: HenrikDevClient) -> None:
        self.bot         = bot
        self.db          = db
        self.riot_client = riot_client

    # ── /link-riot ────────────────────────────────────────────────
    @app_commands.command(name="link-riot", description="Lie ton compte Discord a ton compte Riot (EU)")
    @app_commands.describe(
        riot_id="Ton Riot ID au format Pseudo#TAG (ex: Player#EUW)",
    )
    async def link_riot(
        self,
        interaction: discord.Interaction,
        riot_id: str,
    ) -> None:
        region = DEFAULT_REGION
        # 1) Parse riot_id
        try:
            name, tag = parse_riot_id(riot_id)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 2) Verifier l'existence du compte Riot + recuperer le rang actuel (display).
        # Les appels HenrikDev sont synchrones (`requests`) et bloqueraient l'event
        # loop Discord pendant ~10s en cas de lenteur API. On les execute dans un
        # thread pour preserver la reactivite du bot.
        try:
            account = await asyncio.to_thread(self.riot_client.get_account, name, tag)
            mmr     = await asyncio.to_thread(self.riot_client.get_current_mmr, region, name, tag)
        except PlayerNotFound:
            await interaction.followup.send(f"❌ Joueur **{name}#{tag}** introuvable.", ephemeral=True)
            return
        except RateLimited:
            await interaction.followup.send("⏳ API HenrikDev rate-limited, reessaie dans 1 minute.", ephemeral=True)
            return
        except RiotApiError as e:
            # Ne pas leak la reponse brute de l'API (contient potentiellement
            # des details internes ou des extraits HTML d'erreur). On log
            # cote serveur et on remonte un message generique a l'utilisateur.
            logger.error(f"[link-riot] RiotApiError pour user={interaction.user.id} : {e!r}", exc_info=True)
            await interaction.followup.send(
                "❌ Erreur API Riot temporaire. Reessaie dans quelques instants.",
                ephemeral=True,
            )
            return

        # 2.5) Dedup PUUID : un compte Riot ne peut etre lie qu'a un
        # seul compte Discord par serveur. Sans ce check, un joueur
        # pourrait farmer plusieurs fois le seed LINK_BASE_ELO en liant
        # le meme compte Riot a plusieurs comptes Discord, et tenir 2
        # places en queue avec un seul compte de jeu.
        existing = await asyncio.to_thread(
            repository.find_riot_account_by_puuid,
            self.db, interaction.guild_id, account.puuid,
        )
        if existing is not None and str(existing.get("_id")) != str(interaction.user.id):
            await interaction.followup.send(
                f"❌ Le compte Riot **{name}#{tag}** est deja lie a un autre "
                "membre du serveur. Un compte Riot ne peut etre lie qu'a un "
                "seul compte Discord par serveur.",
                ephemeral=True,
            )
            return

        # 3) Seed atomique de l'ELO de depart (idempotent)
        # Premier link : elo_<guild>.elo += LINK_BASE_ELO (+ ELO bot deja accumulee)
        # Re-link apres unlink : aucun changement (linked_once=True).
        final_elo, seeded_now = repository.seed_elo_with_riot_base(
            self.db,
            interaction.guild_id,
            interaction.user.id,
            riot_base_elo=LINK_BASE_ELO,
            display_name=interaction.user.display_name,
        )

        # 4) Persister la metadata Riot (utilisee pour la queue gate-keep)
        repository.link_riot_account(
            self.db,
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            riot_name=name,
            riot_tag=tag,
            riot_region=region,
            puuid=account.puuid,
            peak_elo=0,
            source="link_base",
        )

        # 5) Embed de confirmation
        embed = discord.Embed(
            title="🎯 Compte Riot lie !",
            color=0x2ecc71,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Riot ID", value=f"**{name}#{tag}**", inline=True)
        embed.add_field(name="Region", value=region.upper(),       inline=True)
        embed.add_field(name="Rang actuel", value=mmr.tier_name,   inline=True)
        embed.add_field(name="ELO serveur", value=f"**{final_elo}**", inline=True)
        if not seeded_now:
            embed.add_field(
                name="ℹ️ Note",
                value="ELO inchangee (deja initialisee lors d'un link precedent).",
                inline=False,
            )
        embed.set_footer(text=f"Discord: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /unlink-riot ──────────────────────────────────────────────
    @app_commands.command(name="unlink-riot", description="Supprime le lien avec ton compte Riot")
    async def unlink_riot(self, interaction: discord.Interaction) -> None:
        ok = repository.unlink_riot_account(
            self.db, interaction.guild_id, interaction.user.id,
        )
        if ok:
            await interaction.response.send_message("✅ Compte Riot delie.", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ Aucun compte Riot lie.", ephemeral=True)

async def setup(bot: commands.Bot, db, riot_client: HenrikDevClient) -> None:
    await bot.add_cog(RiotLinkCog(bot, db, riot_client))
