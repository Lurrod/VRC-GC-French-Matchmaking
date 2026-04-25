"""
Cog V2 : association compte Discord <-> compte Riot.

Commandes :
  /link-riot riot_id:Pseudo#TAG     (region forcee a EU)
  /unlink-riot
  /refresh-elo                      (cooldown 1h via cache HenrikDev)

Restriction serveur : peak elo >= Immortal 1 (2400). Sinon le link est refuse.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services import repository
from services.elo_calc import IMMORTAL_FLOOR_ELO
from services.elo_mapping import elo_to_tier_name
from services.peak_calculator import (
    MatchEntry,
    compute_effective_elo,
    parse_riot_id,
)
from services.riot_api import (
    HenrikDevClient,
    PlayerNotFound,
    RateLimited,
    RiotApiError,
)


# Serveur reserve aux EU
DEFAULT_REGION = "eu"


class RiotLinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db, riot_client: HenrikDevClient) -> None:
        self.bot         = bot
        self.db          = db
        self.riot_client = riot_client

    # ── /link-riot ────────────────────────────────────────────────
    @app_commands.command(name="link-riot", description="Lie ton compte Discord a ton compte Riot (EU, Immortal+ requis)")
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

        # 2) Recuperer account + mmr + history via HenrikDev
        try:
            account = self.riot_client.get_account(name, tag)
            mmr     = self.riot_client.get_current_mmr(region, name, tag)
            history = self.riot_client.get_mmr_history(region, name, tag)
        except PlayerNotFound:
            await interaction.followup.send(f"❌ Joueur **{name}#{tag}** introuvable.", ephemeral=True)
            return
        except RateLimited:
            await interaction.followup.send("⏳ API HenrikDev rate-limited, reessaie dans 1 minute.", ephemeral=True)
            return
        except RiotApiError as e:
            await interaction.followup.send(f"❌ Erreur Riot API : {e}", ephemeral=True)
            return

        # 3) Calculer l'effective elo (regle 6 mois)
        entries = [MatchEntry(elo=h.elo, date=h.date) for h in history]
        result  = compute_effective_elo(
            entries,
            now=datetime.now(timezone.utc),
            fallback=mmr.elo,
        )

        # 3b) Restriction Immortal+ : peak (history) ou MMR courant doit etre Immortal+
        max_observed = max(result.peak, mmr.elo)
        if max_observed < IMMORTAL_FLOOR_ELO:
            tier = elo_to_tier_name(max_observed) if max_observed > 0 else "Unrated"
            await interaction.followup.send(
                f"🚫 Ce serveur est reserve aux joueurs **Immortal 1+**.\n"
                f"Ton meilleur rang detecte : **{tier}** ({max_observed}).",
                ephemeral=True,
            )
            return

        # 4) Persister
        repository.link_riot_account(
            self.db,
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            riot_name=name,
            riot_tag=tag,
            riot_region=region,
            puuid=account.puuid,
            effective_elo=result.elo,
            peak_elo=result.peak,
            source=result.source,
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
        embed.add_field(name="Effective ELO", value=f"**{result.elo}**", inline=True)
        embed.add_field(name="Peak", value=f"**{result.peak}**",   inline=True)
        embed.add_field(name="Source", value=_explain_source(result.source), inline=True)
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

    # ── /refresh-elo ──────────────────────────────────────────────
    @app_commands.command(name="refresh-elo", description="Recalcule ton effective ELO (peut prendre 1h via cache)")
    async def refresh_elo(self, interaction: discord.Interaction) -> None:
        doc = repository.get_riot_account(
            self.db, interaction.guild_id, interaction.user.id,
        )
        if not doc:
            await interaction.response.send_message(
                "❌ Tu n'as pas de compte Riot lie. Utilise `/link-riot` d'abord.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        name   = doc["riot_name"]
        tag    = doc["riot_tag"]
        region = doc["riot_region"]

        # Force le cache a se vider pour cet utilisateur (sinon valeur stale)
        self.riot_client.clear_cache()

        try:
            mmr     = self.riot_client.get_current_mmr(region, name, tag)
            history = self.riot_client.get_mmr_history(region, name, tag)
        except RiotApiError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        entries = [MatchEntry(elo=h.elo, date=h.date) for h in history]
        result  = compute_effective_elo(
            entries,
            now=datetime.now(timezone.utc),
            fallback=mmr.elo,
        )

        repository.link_riot_account(
            self.db,
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            riot_name=name,
            riot_tag=tag,
            riot_region=region,
            puuid=doc.get("puuid", ""),
            effective_elo=result.elo,
            peak_elo=result.peak,
            source=result.source,
        )

        old_elo = doc.get("effective_elo", 0)
        delta   = result.elo - old_elo
        sign    = "+" if delta >= 0 else ""
        embed = discord.Embed(
            title="🔄 Effective ELO mis a jour",
            color=0x3498db,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Avant", value=str(old_elo), inline=True)
        embed.add_field(name="Apres", value=f"**{result.elo}**", inline=True)
        embed.add_field(name="Delta", value=f"{sign}{delta}", inline=True)
        embed.add_field(name="Source", value=_explain_source(result.source), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


def _explain_source(source: str) -> str:
    return {
        "peak_recent":    "🏔️ Peak elo (<6 mois)",
        "average_6m":     "📊 Moyenne 6 derniers mois",
        "peak_fallback":  "🏔️ Peak (aucun match recent)",
        "empty":          "❓ Aucun historique",
    }.get(source, source)


async def setup(bot: commands.Bot, db, riot_client: HenrikDevClient) -> None:
    await bot.add_cog(RiotLinkCog(bot, db, riot_client))
