"""
Cog des commandes prefix legacy : !leaderboard, !stats, !win, !lose, !map.
Extrait de bot.py (refactor monolithe).

NOTE : !leaderboard et !stats utilisent encore l'ancien schema V1
(`_id = str(user_id)`, sans queue_type). Elles renvoient un classement
mixte (toutes queues confondues) en pratique cassé apres la migration V2.
Conservees pour compat ascendante avec tests existants. Les commandes
slash `/leaderboard queue:X` et `/stats queue:X` sont la version correcte.

!win, !lose defaultent a la queue Open (cf. docstrings respectifs).
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime

import discord
from discord.ext import commands
from pymongo import ReturnDocument

from services import elo_calc, repository
from services.leaderboard_refresh import refresh_leaderboard_channel

logger = logging.getLogger(__name__)


# Pondération ELO par position (cohérent avec /win, /lose slash).
WIN_DELTAS_BY_SLOT:  tuple[int, ...] = (20, 18, 17, 16, 15)
LOSE_DELTAS_BY_SLOT: tuple[int, ...] = (10, 10, 12, 13, 15)


def _has_prefix_access(ctx: commands.Context, db) -> bool:
    """Admin (manage_guild) OU role bypass."""
    if ctx.author.guild_permissions.manage_guild:
        return True
    role_id = repository.get_bypass_role(db, ctx.guild.id)
    return bool(role_id and any(r.id == role_id for r in ctx.author.roles))


def _match_elo_for_member(db, guild_id: int, user_id: int, queue_type: str) -> int:
    doc = repository.get_elo_col(db, guild_id).find_one(
        {"_id": repository.player_doc_id(user_id, queue_type)}
    )
    if doc and doc.get("elo") is not None:
        return int(doc["elo"])
    return elo_calc.ELO_REFERENCE


def _compute_match_change(db, guild_id: int, members: list, queue_type: str) -> int:
    elos = [_match_elo_for_member(db, guild_id, m.id, queue_type) for m in members]
    return round(sum(elos) / len(elos)) if elos else elo_calc.ELO_REFERENCE


async def _refresh_leaderboard_safe(guild: discord.Guild | None, db, queue_type: str) -> None:
    if guild is None:
        return
    try:
        await refresh_leaderboard_channel(guild, db, queue_type)
    except Exception:
        logger.exception("[prefix-legacy] refresh a leve")


class PrefixLegacyCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db

    @commands.command(name="leaderboard")
    async def leaderboard_prefix(self, ctx: commands.Context):
        col  = repository.get_elo_col(self.db, ctx.guild.id)
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
        embed = discord.Embed(title="Classement ELO", description="\n".join(lines), color=0xf1c40f, timestamp=datetime.now(UTC))
        embed.set_footer(text=ctx.guild.name)
        await ctx.send(embed=embed)

    @commands.command(name="stats")
    async def stats_prefix(self, ctx: commands.Context, member: discord.Member = None):
        if member is None:
            member = ctx.author
        col = repository.get_elo_col(self.db, ctx.guild.id)
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
        embed = discord.Embed(title=f"Stats de {member.display_name}", color=0x3498db, timestamp=datetime.now(UTC))
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="🏅 ELO",       value=f"**{elo}**",            inline=True)
        embed.add_field(name="🏆 Rang",      value=f"**#{rank}**",          inline=True)
        embed.add_field(name="📈 Winrate",   value=f"**{winrate}%**",       inline=True)
        embed.add_field(name="✅ Victoires", value=f"**{wins}**",           inline=True)
        embed.add_field(name="❌ Défaites",  value=f"**{losses}**",         inline=True)
        embed.add_field(name="🎮 Parties",   value=f"**{total}**",          inline=True)
        embed.set_footer(text=ctx.guild.name)
        await ctx.send(embed=embed)

    @commands.command(name="win")
    async def win_prefix(self, ctx: commands.Context,
        joueur1: discord.Member,
        joueur2: discord.Member = None, joueur3: discord.Member = None,
        joueur4: discord.Member = None, joueur5: discord.Member = None,
    ):
        """Prefix legacy : applique sur la queue Open par defaut."""
        if not _has_prefix_access(ctx, self.db):
            await ctx.send("Pas la permission.")
            return
        queue = "open"
        players = [p for p in [joueur1, joueur2, joueur3, joueur4, joueur5] if p is not None]
        col = repository.get_elo_col(self.db, ctx.guild.id)
        avg_elo = _compute_match_change(self.db, ctx.guild.id, players, queue)
        embed = discord.Embed(
            title="🏆 Résultats Open — Victoire enregistrée !",
            description=f"Avg ELO du groupe : **{avg_elo}** -> gains pondérés par position (joueur1→joueur5)",
            color=0x2ecc71,
            timestamp=datetime.now(UTC),
        )
        for slot, member in enumerate(players):
            gain = WIN_DELTAS_BY_SLOT[slot]
            repository.get_or_create_player(
                col, member.id, queue, member.display_name, initial_elo=elo_calc.ELO_START,
            )
            old_doc = col.find_one_and_update(
                {"_id": repository.player_doc_id(member.id, queue)},
                {"$inc": {"elo": gain, "wins": 1}},
                return_document=ReturnDocument.BEFORE,
            )
            old = (old_doc or {}).get("elo", 0)
            new = old + gain
            embed.add_field(name=member.display_name, value=f"+{gain} ELO -> **{new}**", inline=False)
        embed.set_footer(text=f"Enregistre par {ctx.author.display_name}")
        await ctx.send(embed=embed)
        await _refresh_leaderboard_safe(ctx.guild, self.db, queue)

    @commands.command(name="lose")
    async def lose_prefix(self, ctx: commands.Context,
        joueur1: discord.Member,
        joueur2: discord.Member = None, joueur3: discord.Member = None,
        joueur4: discord.Member = None, joueur5: discord.Member = None,
    ):
        """Prefix legacy : applique sur la queue Open par defaut."""
        if not _has_prefix_access(ctx, self.db):
            await ctx.send("Pas la permission.")
            return
        queue = "open"
        players = [p for p in [joueur1, joueur2, joueur3, joueur4, joueur5] if p is not None]
        col = repository.get_elo_col(self.db, ctx.guild.id)
        avg_elo = _compute_match_change(self.db, ctx.guild.id, players, queue)
        embed = discord.Embed(
            title="💀 Résultats — Défaite enregistrée !",
            description=f"Avg ELO du groupe : **{avg_elo}** -> pertes pondérées par position (joueur1→joueur5)",
            color=0xe74c3c,
            timestamp=datetime.now(UTC),
        )
        for slot, member in enumerate(players):
            loss = LOSE_DELTAS_BY_SLOT[slot]
            repository.get_or_create_player(
                col, member.id, queue, member.display_name, initial_elo=elo_calc.ELO_START,
            )
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
            embed.add_field(name=member.display_name, value=f"-{loss} ELO -> **{new}**", inline=False)
        embed.set_footer(text=f"Enregistre par {ctx.author.display_name}")
        await ctx.send(embed=embed)
        await _refresh_leaderboard_safe(ctx.guild, self.db, queue)

    @commands.command(name="map")
    async def map_prefix(self, ctx: commands.Context):
        if not _has_prefix_access(ctx, self.db):
            await ctx.send("Pas la permission.")
            return
        chosen = random.choice(elo_calc.MAPS)
        embed = discord.Embed(title="🗺️ Map sélectionnée !", description=f"## {chosen}", color=0x9b59b6, timestamp=datetime.now(UTC))
        embed.set_footer(text=f"Tirage par {ctx.author.display_name}")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot, db) -> None:
    await bot.add_cog(PrefixLegacyCog(bot, db))
