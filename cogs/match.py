"""
Cog V2 : formation de match + systeme de vote.

Phase 4 : formation des equipes apres queue pleine.
Phase 5 : VoteView complete (7/10 majorite, timeout 5min, ping admin).

Branche en tant que `on_full` du QueueCog. Quand 10 joueurs sont dans la queue :
  1. Construit les Player a partir des comptes Riot lies.
  2. Equilibre via team_balancer.
  3. Trouve une categorie Match #N libre (sinon annonce vocaux libres).
  4. Poste un message taggant les 10 joueurs avec embed equipes/map/leader/VCs
     + VoteView attache.
  5. Persiste le match en base.
  6. Reset la queue.

Vote :
  - Seuls les 10 participants peuvent voter, vote modifiable.
  - Des qu'une equipe atteint 7/10 votes -> match valide (`validated_a/b`).
  - Si pas de majorite apres 5 min -> match `contested`, ping admin.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Final

import discord
from discord.ext import commands, tasks

from services import repository
from services.elo_updater import apply_match_validation, MatchEloOutcome
from services.match_service import (
    build_players,
    plan_match,
    serialize_team,
    find_free_match_category,
    find_free_match_prep,
)


VOTE_A_BTN_ID:    Final[str] = "vote_v2:a"
VOTE_B_BTN_ID:    Final[str] = "vote_v2:b"
MAJORITY_THRESHOLD: Final[int] = 7
VOTE_TIMEOUT_MINUTES: Final[int] = 60

# Roles cibles pour le ping admin (premier trouve gagne)
ADMIN_ROLE_NAMES: Final[tuple[str, ...]] = ("Admin", "Match Staff", "Administrateur")


# ── Embed : depuis MatchPlan (publication initiale) ───────────────
def build_match_embed(plan, guild_name: str) -> discord.Embed:
    teams        = plan.teams
    map_name     = plan.map_name
    leader       = plan.lobby_leader
    category     = plan.category_name

    embed = discord.Embed(
        title="🎯 Match trouve !",
        description=f"**Map :** {map_name}\n**Lobby host :** <@{leader.id}> ({leader.name})",
        color=0x5865f2,
        timestamp=datetime.now(timezone.utc),
    )

    a_lines = "\n".join(f"• <@{p.id}> ({p.elo})" for p in teams.team_a)
    b_lines = "\n".join(f"• <@{p.id}> ({p.elo})" for p in teams.team_b)
    embed.add_field(name=f"🔵 Team A ({teams.total_a})", value=a_lines, inline=True)
    embed.add_field(name=f"🔴 Team B ({teams.total_b})", value=b_lines, inline=True)
    embed.add_field(
        name="Equilibrage",
        value=f"diff `{teams.elo_diff}` · peak diff `{teams.peak_diff}`",
        inline=False,
    )

    if category:
        embed.add_field(
            name="🔊 Vocaux",
            value=f"**Team A** -> `{category} / Team 1`\n**Team B** -> `{category} / Team 2`",
            inline=False,
        )
    else:
        embed.add_field(
            name="🔊 Vocaux",
            value="⚠️ Aucune categorie libre (`Match #1/2/3` toutes occupees).",
            inline=False,
        )

    embed.add_field(
        name="🗳️ Votes",
        value=f"Team A : **0** / Team B : **0** *(majorite : {MAJORITY_THRESHOLD}/10)*",
        inline=False,
    )

    embed.set_footer(text=f"{guild_name} · Votez quelle equipe a gagne ci-dessous")
    return embed


# ── Embed : depuis match_doc (vote update) ────────────────────────
def build_match_embed_from_doc(doc: dict, guild_name: str) -> discord.Embed:
    team_a   = doc["team_a"]
    team_b   = doc["team_b"]
    map_name = doc["map"]
    leader_id   = doc["lobby_leader_id"]
    leader_name = next(
        (p["name"] for p in (team_a + team_b) if str(p["id"]) == str(leader_id)),
        "?",
    )
    category = doc.get("category_name")
    status   = doc.get("status", "pending")
    votes    = doc.get("votes", {})
    count_a  = sum(1 for v in votes.values() if v == "a")
    count_b  = sum(1 for v in votes.values() if v == "b")

    if status == "validated_a":
        title, color, footer_extra = "🏆 Team A a gagne !", 0x2ecc71, "Match valide"
    elif status == "validated_b":
        title, color, footer_extra = "🏆 Team B a gagne !", 0xe74c3c, "Match valide"
    elif status == "contested":
        title, color, footer_extra = "⚠️ Match en attente admin", 0xe67e22, "Vote en timeout"
    else:
        title, color, footer_extra = "🎯 Match en cours - Votez !", 0x5865f2, "Cliquez sur l'equipe gagnante"

    embed = discord.Embed(
        title=title, color=color, timestamp=datetime.now(timezone.utc),
        description=f"**Map :** {map_name}\n**Lobby host :** <@{leader_id}> ({leader_name})",
    )

    sum_a   = sum(p["elo"] for p in team_a)
    sum_b   = sum(p["elo"] for p in team_b)
    a_lines = "\n".join(f"• <@{p['id']}> ({p['elo']})" for p in team_a)
    b_lines = "\n".join(f"• <@{p['id']}> ({p['elo']})" for p in team_b)
    embed.add_field(name=f"🔵 Team A ({sum_a})", value=a_lines, inline=True)
    embed.add_field(name=f"🔴 Team B ({sum_b})", value=b_lines, inline=True)
    embed.add_field(name="Equilibrage", value=f"diff `{abs(sum_a - sum_b)}`", inline=False)

    if category:
        embed.add_field(
            name="🔊 Vocaux",
            value=f"**Team A** -> `{category} / Team 1`\n**Team B** -> `{category} / Team 2`",
            inline=False,
        )

    embed.add_field(
        name="🗳️ Votes",
        value=f"Team A : **{count_a}** / Team B : **{count_b}** *(majorite : {MAJORITY_THRESHOLD}/10)*",
        inline=False,
    )

    embed.set_footer(text=f"{guild_name} · {footer_extra}")
    return embed


# ── Embed : recap MAJ ELO post-validation ─────────────────────────
def build_elo_changes_embed(outcome: MatchEloOutcome, match_doc: dict, guild_name: str) -> discord.Embed:
    status = match_doc.get("status")
    if status == "validated_a":
        winner_label, color = "Team A", 0x2ecc71
    else:
        winner_label, color = "Team B", 0xe74c3c

    embed = discord.Embed(
        title=f"🏆 {winner_label} l'emporte ! ELO mis a jour",
        description=(
            f"Avg ELO du match : **{outcome.avg_elo}**\n"
            f"Gain par gagnant : **+{outcome.gain}**\n"
            f"Perte par perdant : **-{outcome.loss}**"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    winners = [c for c in outcome.changes if c.win]
    losers  = [c for c in outcome.changes if not c.win]

    w_lines = "\n".join(
        f"• <@{c.user_id}>  +{c.delta}  →  **{c.new_elo}** *(etait {c.old_elo})*"
        for c in winners
    )
    l_lines = "\n".join(
        f"• <@{c.user_id}>  {c.delta}  →  **{c.new_elo}** *(etait {c.old_elo})*"
        for c in losers
    )
    embed.add_field(name="🟢 Gagnants", value=w_lines or "—", inline=False)
    embed.add_field(name="🔴 Perdants", value=l_lines or "—", inline=False)
    embed.set_footer(text=guild_name)
    return embed


# ── VoteView ──────────────────────────────────────────────────────
class VoteView(discord.ui.View):
    """View persistante : Team A gagne / Team B gagne."""

    def __init__(self, db, on_validated=None) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.on_validated = on_validated  # callable(inter, match_doc) -> awaitable

    async def _vote(self, inter: discord.Interaction, choice: str) -> None:
        # 1) Retrouver le match via le message_id
        match = repository.get_match_by_message(self.db, inter.guild_id, inter.message.id)
        if not match:
            await inter.response.send_message("❌ Match introuvable.", ephemeral=True)
            return

        # 2) Match deja valide -> refus
        if match.get("status") in ("validated_a", "validated_b"):
            await inter.response.send_message(
                "✅ Ce match est deja valide.", ephemeral=True,
            )
            return

        # 3) Verifie la participation
        all_player_ids = {
            str(p["id"]) for p in (match.get("team_a", []) + match.get("team_b", []))
        }
        if str(inter.user.id) not in all_player_ids:
            await inter.response.send_message(
                "❌ Tu n'as pas joue ce match, tu ne peux pas voter.",
                ephemeral=True,
            )
            return

        # 4) Enregistre le vote (ecrase un vote precedent)
        updated = repository.add_match_vote(
            self.db, inter.guild_id, match["_id"], inter.user.id, choice,
        )
        if updated is None:
            await inter.response.send_message("❌ Erreur d'enregistrement.", ephemeral=True)
            return

        # 5) Compte
        votes   = updated.get("votes", {})
        count_a = sum(1 for v in votes.values() if v == "a")
        count_b = sum(1 for v in votes.values() if v == "b")

        # 6) Majorite ?
        new_status = None
        if count_a >= MAJORITY_THRESHOLD:
            new_status = "validated_a"
        elif count_b >= MAJORITY_THRESHOLD:
            new_status = "validated_b"

        if new_status:
            repository.set_match_status(
                self.db, inter.guild_id, match["_id"], new_status,
            )
            updated = repository.get_match(self.db, inter.guild_id, match["_id"])

        # 7) Edit du message (embed maj, view retiree si valide)
        embed = build_match_embed_from_doc(updated, inter.guild.name)
        if new_status:
            await inter.response.edit_message(embed=embed, view=None)
        else:
            await inter.response.edit_message(embed=embed, view=self)

        # 8) Hook Phase 6 : MAJ ELO
        if new_status and self.on_validated:
            try:
                await self.on_validated(inter, updated)
            except Exception as e:
                print(f"[vote] on_validated a leve : {e}")

    @discord.ui.button(
        label="Team A gagne", style=discord.ButtonStyle.primary, custom_id=VOTE_A_BTN_ID,
    )
    async def vote_a(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._vote(inter, "a")

    @discord.ui.button(
        label="Team B gagne", style=discord.ButtonStyle.primary, custom_id=VOTE_B_BTN_ID,
    )
    async def vote_b(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._vote(inter, "b")


# ── Cog ───────────────────────────────────────────────────────────
class MatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db, *, rng: random.Random | None = None) -> None:
        self.bot       = bot
        self.db        = db
        self.rng       = rng or random.Random()
        self.vote_view = VoteView(db, on_validated=self._on_match_validated)

    # ── Branchement queue full ───────────────────────────────────
    async def on_queue_full(self, interaction: discord.Interaction, queue_doc: dict):
        guild      = interaction.guild
        player_ids = [str(uid) for uid in queue_doc.get("players", [])]

        riot_accounts: dict[str, dict] = {}
        member_names:  dict[str, str]  = {}
        bot_elos:      dict[str, int]  = {}
        elo_col = repository.get_elo_col(self.db, guild.id)
        for uid in player_ids:
            doc = repository.get_riot_account(self.db, guild.id, uid)
            if doc:
                riot_accounts[uid] = dict(doc)
            member = guild.get_member(int(uid))
            if member:
                member_names[uid] = member.display_name
            elo_doc = elo_col.find_one({"_id": uid})
            if elo_doc:
                bot_elos[uid] = int(elo_doc.get("elo", 0))

        players = build_players(player_ids, riot_accounts, member_names, bot_elos)
        if len(players) < 10:
            await self._fail(interaction, queue_doc,
                             "Joueur(s) sans compte Riot lie. Match annule.")
            return None

        # Channel d'origine de la queue (pour reposter le setup-queue apres)
        queue_channel = guild.get_channel(int(queue_doc["channel_id"]))
        if queue_channel is None:
            await self._fail(interaction, queue_doc, "Salon de queue introuvable.")
            return None

        # Recherche d'un salon 'match-preparation' libre (categories Match #1/2/3)
        free = find_free_match_prep(guild)
        if free is None:
            await self._fail(
                interaction, queue_doc,
                "Aucun salon 'match-preparation' libre dans les categories Match #1/2/3.",
            )
            return None
        free_cat_name, prep_channel = free

        plan = plan_match(players, free_category=free_cat_name, rng=self.rng)

        mentions = " ".join(f"<@{p.id}>" for p in players)
        embed    = build_match_embed(plan, guild.name)
        msg = await prep_channel.send(
            content=f"🎯 Match trouve ! {mentions}",
            embed=embed,
            view=self.vote_view,
        )

        match_id = repository.create_match(
            self.db,
            guild_id=guild.id,
            team_a=serialize_team(plan.teams.team_a),
            team_b=serialize_team(plan.teams.team_b),
            map_name=plan.map_name,
            lobby_leader_id=plan.lobby_leader.id,
            category_name=plan.category_name,
            message_id=msg.id,
            channel_id=prep_channel.id,
        )

        # Reset queue + repose le message setup-queue dans le salon d'origine
        # pour qu'une nouvelle queue soit immediatement disponible.
        repository.delete_active_queue(self.db, guild.id)
        queue_cog = self.bot.get_cog("QueueCog")
        if queue_cog is not None:
            try:
                await queue_cog.post_queue_message(queue_channel)
            except Exception as e:
                print(f"[match] echec re-post setup-queue : {e}")
        return match_id

    async def _fail(self, interaction, queue_doc, reason: str) -> None:
        repository.delete_active_queue(self.db, interaction.guild.id)
        try:
            channel = interaction.guild.get_channel(int(queue_doc["channel_id"]))
            if channel:
                await channel.send(
                    f"⚠️ {reason} La queue a ete annulee, refaites `/setup-queue`.",
                )
        except Exception:
            pass

    # ── Hook : MAJ ELO apres validation du vote ──────────────────
    async def _on_match_validated(self, inter, match_doc) -> None:
        """
        Distribue les ELO sur la table V1 selon la moyenne d'effective_elo
        des 10 joueurs du match. Envoie un recap dans le salon.
        """
        try:
            outcome = apply_match_validation(self.db, inter.guild.id, match_doc)
        except Exception as e:
            print(f"[match] apply_match_validation a leve : {e}")
            return

        embed = build_elo_changes_embed(outcome, match_doc, inter.guild.name)
        channel_id = match_doc.get("channel_id")
        if not channel_id:
            return
        channel = inter.guild.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[match] envoi du recap a leve : {e}")

    # ── Timeout des votes ────────────────────────────────────────
    async def check_vote_timeouts(self, *, now: datetime | None = None) -> int:
        """
        Scanne tous les guilds connus. Pour chaque match `pending` cree
        depuis plus de VOTE_TIMEOUT_MINUTES, marque `contested` et
        ping le role admin du salon.

        Returns:
            nombre de matches passes en `contested` cet appel
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=VOTE_TIMEOUT_MINUTES)
        flagged = 0
        for guild in self.bot.guilds:
            col = repository.get_matches_col(self.db, guild.id)
            stale = list(col.find({
                "status":     "pending",
                "created_at": {"$lt": cutoff},
            }))
            for match in stale:
                await self._handle_timeout(guild, match)
                flagged += 1
        return flagged

    async def _handle_timeout(self, guild, match) -> None:
        repository.set_match_status(
            self.db, guild.id, match["_id"], "contested",
        )

        admin_role = None
        for role_name in ADMIN_ROLE_NAMES:
            admin_role = discord.utils.get(guild.roles, name=role_name)
            if admin_role:
                break

        channel_id = match.get("channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not channel:
            return

        ping = admin_role.mention if admin_role else "@admin"
        votes = match.get("votes", {})
        count_a = sum(1 for v in votes.values() if v == "a")
        count_b = sum(1 for v in votes.values() if v == "b")

        try:
            await channel.send(
                f"⏰ {ping} Vote du match en timeout (>{VOTE_TIMEOUT_MINUTES} min "
                f"sans {MAJORITY_THRESHOLD}/10). Score actuel : Team A `{count_a}` / Team B `{count_b}`. "
                f"Validation manuelle requise.",
            )
        except Exception as e:
            print(f"[match] _handle_timeout send a leve : {e}")

    # ── Loop periodique (1 min) ──────────────────────────────────
    @tasks.loop(minutes=1)
    async def _timeout_loop(self):
        try:
            await self.check_vote_timeouts()
        except Exception as e:
            print(f"[match] _timeout_loop a leve : {e}")

    @_timeout_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        self._timeout_loop.start()

    async def cog_unload(self):
        self._timeout_loop.cancel()


async def setup(bot: commands.Bot, db, *, rng: random.Random | None = None) -> MatchCog:
    cog = MatchCog(bot, db, rng=rng)
    await bot.add_cog(cog)
    bot.add_view(cog.vote_view)
    return cog
