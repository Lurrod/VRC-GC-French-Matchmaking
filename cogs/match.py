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

import asyncio

from cogs.queue_v2 import _grant_match_role, _revoke_match_role, _revoke_queue_role

MATCH_ROLE_CLEANUP_DELAY_SECONDS: Final[int] = 60
MATCH_HOST_ROLE_NAME: Final[str]              = "Match Host"
MATCH_HOST_CLEANUP_DELAY_SECONDS: Final[int]  = 600  # 10 min apres validation

from services import repository
from services.elo_updater import (
    apply_match_validation,
    MatchEloOutcome,
)
from services.match_verifier import (
    find_henrik_custom_match,
    compute_acs_multipliers,
)
from services.riot_api import HenrikDevClient
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
VOTE_TIMEOUT_MINUTES: Final[int]            = 60
HENRIK_VERIFY_DELAY_MINUTES: Final[int]     = 5    # premier essai Henrik a 5 min
HENRIK_VERIFY_TIMEOUT_MINUTES: Final[int]   = 30   # abandon Henrik et ELO plat a 30 min

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

    embed.set_footer(text=f"{guild_name} · Reportez ci-dessous quelle equipe a remporte la partie")
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
        title, color, footer_extra = "🎯 Match termine - Reportez le vainqueur", 0x5865f2, "Cliquez sur l'equipe qui a remporte la partie"

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

    weighted = outcome.weighted
    title = (
        f"🏆 {winner_label} l'emporte ! ELO mis a jour"
        f"{' (ponderation ACS)' if weighted else ''}"
    )
    desc_extra = (
        "\nPonderation ACS appliquee via stats HenrikDev."
        if weighted
        else "\n⚠️ Match Riot non retrouve sur HenrikDev — ELO plat applique."
    )

    embed = discord.Embed(
        title=title,
        description=(
            f"Avg ELO du match : **{outcome.avg_elo}**\n"
            f"Base gagnant : **+{outcome.gain}**\n"
            f"Base perdant : **-{outcome.loss}**"
            f"{desc_extra}"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    winners = [c for c in outcome.changes if c.win]
    losers  = [c for c in outcome.changes if not c.win]

    def _fmt(c):
        sign = "+" if c.delta >= 0 else ""
        mult = f" ×{c.multiplier:.2f}" if weighted else ""
        return (
            f"• <@{c.user_id}>{mult}  {sign}{c.delta}  →  **{c.new_elo}** "
            f"*(etait {c.old_elo})*"
        )

    w_lines = "\n".join(_fmt(c) for c in winners)
    l_lines = "\n".join(_fmt(c) for c in losers)
    embed.add_field(name="🟢 Gagnants", value=w_lines or "—", inline=False)
    embed.add_field(name="🔴 Perdants", value=l_lines or "—", inline=False)
    embed.set_footer(text=guild_name)
    return embed


# ── VoteView ──────────────────────────────────────────────────────
class VoteView(discord.ui.View):
    """View persistante : reporter le vainqueur du match (Team A / Team B)."""

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

        # 6) Majorite atteinte ? Transition atomique (CAS) pour eviter
        #    qu'un vote concurrent ne valide deux fois et ne declenche
        #    `on_validated` plusieurs fois.
        target_status = None
        if count_a >= MAJORITY_THRESHOLD:
            target_status = "validated_a"
        elif count_b >= MAJORITY_THRESHOLD:
            target_status = "validated_b"

        transitioned_doc = None
        if target_status:
            transitioned_doc = repository.transition_match_status(
                self.db, inter.guild_id, match["_id"],
                from_status="pending", to_status=target_status,
            )
            if transitioned_doc is not None:
                updated = transitioned_doc
            else:
                # Un autre vote concurrent a deja valide. On re-fetch pour
                # afficher l'etat reel sans tirer `on_validated` de notre cote.
                updated = repository.get_match(self.db, inter.guild_id, match["_id"]) or updated

        # 7) Edit du message (embed maj, view retiree si valide)
        embed = build_match_embed_from_doc(updated, inter.guild.name)
        if updated.get("status") in ("validated_a", "validated_b"):
            await inter.response.edit_message(embed=embed, view=None)
        else:
            await inter.response.edit_message(embed=embed, view=self)

        # 8) Hook Phase 6 : MAJ ELO. Tire UNIQUEMENT si la transition CAS a
        #    reussi de notre cote (i.e. ce vote-ci est celui qui a fait
        #    basculer le match).
        if transitioned_doc is not None and self.on_validated:
            try:
                await self.on_validated(inter, transitioned_doc)
            except Exception as e:
                print(f"[vote] on_validated a leve : {e}")

    @discord.ui.button(
        label="Team A a gagne", style=discord.ButtonStyle.primary, custom_id=VOTE_A_BTN_ID,
    )
    async def vote_a(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._vote(inter, "a")

    @discord.ui.button(
        label="Team B a gagne", style=discord.ButtonStyle.primary, custom_id=VOTE_B_BTN_ID,
    )
    async def vote_b(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._vote(inter, "b")


# ── Cog ───────────────────────────────────────────────────────────
class MatchCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        db,
        *,
        rng: random.Random | None = None,
        henrik_client: HenrikDevClient | None = None,
    ) -> None:
        self.bot           = bot
        self.db            = db
        self.rng           = rng or random.Random()
        self.henrik_client = henrik_client
        self.vote_view     = VoteView(db, on_validated=self._on_match_validated)

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

        # ── Etape 1 : roles AVANT le message ────────────────────
        # Le salon match-preparation peut etre gate par le role Match #N ;
        # si on envoie le message avant d'attribuer le role, certains joueurs
        # ne verront pas l'annonce. On grant en premier.
        for uid in player_ids:
            member = guild.get_member(int(uid))
            if member is None:
                continue
            await _revoke_queue_role(member)
            await _grant_match_role(member, free_cat_name)

        leader_member = guild.get_member(int(plan.lobby_leader.id))
        if leader_member is not None:
            await _grant_match_role(leader_member, MATCH_HOST_ROLE_NAME)

        # ── Etape 2 : deplacement vocal Waiting Room -> Waiting Match ──
        # Le bot rassemble les 10 joueurs dans la salle vocale dediee de la
        # categorie attribuee. Sans matchmaking VC ici, on a un trou de UX.
        await self._move_players_to_match_vc(guild, free_cat_name, player_ids)

        # ── Etape 3 : message d'annonce avec embed + VoteView ───
        mentions = " ".join(f"<@{p.id}>" for p in players)
        embed    = build_match_embed(plan, guild.name)
        msg = await prep_channel.send(
            content=f"🎯 Match trouve ! {mentions}",
            embed=embed,
            view=self.vote_view,
        )

        # ── Etape 4 : persistance du match ──────────────────────
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

        # ── Etape 5 : reset queue + repose setup-queue ──────────
        repository.delete_active_queue(self.db, guild.id)
        queue_cog = self.bot.get_cog("QueueCog")
        if queue_cog is not None:
            try:
                await queue_cog.post_queue_message(queue_channel)
            except Exception as e:
                print(f"[match] echec re-post setup-queue : {e}")
        return match_id

    async def _move_players_to_match_vc(
        self, guild, free_cat_name: str, player_ids: list[str],
    ) -> None:
        """Deplace les 10 joueurs dans la VC `Waiting Match` de la categorie
        attribuee. Skip silencieusement les joueurs hors vocal ou deja sur place.

        Tous les joueurs valides ont ete auto-deplaces dans `Waiting Room` au
        clic sur Rejoindre (cf. queue_v2._move_to_waiting_room) ; on les
        regroupe ici dans la VC du match formee.
        """
        category = discord.utils.get(guild.categories, name=free_cat_name)
        if category is None:
            return
        waiting_match = discord.utils.get(
            category.voice_channels, name="Waiting Match",
        )
        if waiting_match is None:
            return
        for uid in player_ids:
            member = guild.get_member(int(uid))
            if member is None:
                continue
            voice = getattr(member, "voice", None)
            if voice is None or getattr(voice, "channel", None) is None:
                continue  # joueur hors vocal -> rien a deplacer
            if voice.channel.id == waiting_match.id:
                continue  # deja a destination
            try:
                await member.move_to(
                    waiting_match, reason="Match forme : regroupement VC",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

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

    # ── Hook : vote valide ───────────────────────────────────────
    async def _on_match_validated(self, inter, match_doc) -> None:
        """
        Vote valide : on NE TOUCHE PAS encore a l'ELO.
        L'ELO sera applique en une seule passe par `_verify_match`
        apres ~HENRIK_VERIFY_DELAY_MINUTES (avec ponderation ACS si
        HenrikDev a retrouve le custom, plat sinon).
        """
        elo_log_channel = discord.utils.get(
            inter.guild.text_channels, name="elo-adding",
        )
        if elo_log_channel is not None:
            try:
                await elo_log_channel.send(
                    f"⏳ Match valide ({match_doc.get('status')}). "
                    f"Verification HenrikDev a partir de {HENRIK_VERIFY_DELAY_MINUTES} min "
                    f"(retry chaque minute, abandon a {HENRIK_VERIFY_TIMEOUT_MINUTES} min)."
                )
            except Exception as e:
                print(f"[match] envoi annonce attente Henrik a leve : {e}")

        # Suppression differee du role "Match #N" attribue aux 10 joueurs
        category_name = match_doc.get("category_name")
        if category_name:
            asyncio.create_task(
                self._cleanup_match_role(inter.guild, match_doc, category_name)
            )

        # Suppression differee du role "Match Host" attribue au lobby leader
        # (laisse 10 min pour qu'il poste le screen de fin de game).
        leader_id = match_doc.get("lobby_leader_id")
        if leader_id is not None:
            asyncio.create_task(
                self._cleanup_match_host_role(inter.guild, leader_id)
            )

    async def _cleanup_match_role(self, guild, match_doc: dict, role_name: str) -> None:
        await asyncio.sleep(MATCH_ROLE_CLEANUP_DELAY_SECONDS)
        for team_key in ("team_a", "team_b"):
            for player in match_doc.get(team_key, []):
                uid = player.get("id")
                if uid is None:
                    continue
                member = guild.get_member(int(uid))
                if member is not None:
                    await _revoke_match_role(member, role_name)

    async def _cleanup_match_host_role(self, guild, leader_id) -> None:
        await asyncio.sleep(MATCH_HOST_CLEANUP_DELAY_SECONDS)
        member = guild.get_member(int(leader_id))
        if member is not None:
            await _revoke_match_role(member, MATCH_HOST_ROLE_NAME)

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
                # Auto-reparation : un match peut avoir atteint 7+ votes pour
                # une equipe sans transition (ex: crash bot entre l'ecriture
                # du vote et set_match_status). On recupere au lieu de
                # marquer contested ; check_henrik_verifications appliquera
                # l'ELO au prochain tick.
                votes   = match.get("votes", {})
                count_a = sum(1 for v in votes.values() if v == "a")
                count_b = sum(1 for v in votes.values() if v == "b")
                if count_a >= MAJORITY_THRESHOLD:
                    repository.set_match_status(
                        self.db, guild.id, match["_id"], "validated_a",
                    )
                    continue
                if count_b >= MAJORITY_THRESHOLD:
                    repository.set_match_status(
                        self.db, guild.id, match["_id"], "validated_b",
                    )
                    continue
                await self._handle_timeout(guild, match)
                flagged += 1
        return flagged

    async def _handle_timeout(self, guild, match) -> None:
        repository.set_match_status(
            self.db, guild.id, match["_id"], "contested",
        )

        # Retire immediatement le role "Match Host" au lobby leader :
        # le vote n'a pas abouti dans VOTE_TIMEOUT_MINUTES, l'admin reprend la main.
        leader_id = match.get("lobby_leader_id")
        if leader_id is not None:
            leader_member = guild.get_member(int(leader_id))
            if leader_member is not None:
                await _revoke_match_role(leader_member, MATCH_HOST_ROLE_NAME)

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

    # ── Verification HenrikDev + application ELO unique ──────────
    async def check_henrik_verifications(self, *, now: datetime | None = None) -> int:
        """Pour chaque match valide depuis > HENRIK_VERIFY_DELAY_MINUTES sans
        verification Henrik :
          - cherche le custom HenrikDev (multiplicateurs ACS si trouve)
          - si Henrik trouve : applique ELO pondere (definitif)
          - si Henrik ne trouve pas et qu'on est sous le timeout : on retentera
            au prochain tick (boucle 1 min)
          - si on a depasse HENRIK_VERIFY_TIMEOUT_MINUTES : applique ELO plat
            et marque le match comme verifie (abandon Henrik)
        Retourne le nombre de matches traites."""
        now    = now or datetime.now(timezone.utc)
        start_cutoff   = now - timedelta(minutes=HENRIK_VERIFY_DELAY_MINUTES)
        timeout_cutoff = now - timedelta(minutes=HENRIK_VERIFY_TIMEOUT_MINUTES)
        processed = 0
        for guild in self.bot.guilds:
            stale = repository.find_validated_unverified(self.db, guild.id, start_cutoff)
            for match in stale:
                validated_at = match.get("validated_at") or match.get("created_at")
                timed_out = bool(
                    validated_at is not None and validated_at <= timeout_cutoff
                )
                try:
                    await self._verify_match(guild, match, force_apply=timed_out)
                except Exception as e:
                    print(f"[match] verify_match a leve : {e}")
                processed += 1
        return processed

    async def _verify_match(
        self, guild, match_doc: dict, *, force_apply: bool = False,
    ) -> None:
        """
        Tente la verif HenrikDev. Applique l'ELO si :
          - Henrik a trouve les multiplicateurs ACS (ELO pondere), OU
          - `force_apply` est True (timeout atteint -> ELO plat).
        Sinon : ne fait rien, le match sera retente au prochain tick.

        Idempotence : on **claim** le match (`elo_applied=True`) AVANT
        d'appliquer l'ELO. Si le claim echoue (deja applique ailleurs), on
        skip. Si l'application ELO leve, on relache le claim pour permettre
        un retry au prochain tick.
        """
        multipliers: dict[str, float] | None = None
        if self.henrik_client is not None:
            multipliers = await self._fetch_henrik_multipliers(guild, match_doc)

        if multipliers is None and not force_apply:
            # Pas trouve, pas en timeout -> on retentera dans 1 min.
            return

        # Claim atomique : seul le premier appel passe. Empeche la double
        # application en cas de crash entre apply_match_validation et
        # set_match_henrik_verified, ou de tick concurrent.
        claimed = repository.claim_match_for_elo(
            self.db, guild.id, match_doc["_id"],
        )
        if claimed is None:
            return  # Deja applique par un tick precedent.

        try:
            outcome = apply_match_validation(
                self.db, guild.id, match_doc, multipliers=multipliers,
            )
        except Exception as e:
            print(f"[match] apply_match_validation a leve : {e}")
            # Rollback du claim pour permettre un retry au prochain tick.
            repository.release_elo_claim(self.db, guild.id, match_doc["_id"])
            return

        repository.set_match_henrik_verified(
            self.db, guild.id, match_doc["_id"],
            found=multipliers is not None,
            multipliers=multipliers,
        )

        embed   = build_elo_changes_embed(outcome, match_doc, guild.name)
        elo_log = discord.utils.get(guild.text_channels, name="elo-adding")
        if elo_log is not None:
            try:
                await elo_log.send(embed=embed)
            except Exception as e:
                print(f"[match] envoi recap ELO a leve : {e}")

    async def _fetch_henrik_multipliers(
        self, guild, match_doc: dict,
    ) -> dict[str, float] | None:
        """Tente de retrouver le custom HenrikDev et de calculer les
        multiplicateurs ACS. Retourne None si pas exploitable."""
        leader_uid  = str(match_doc.get("lobby_leader_id"))
        leader_riot = repository.get_riot_account(self.db, guild.id, leader_uid)
        if not leader_riot:
            return None

        team_a_uid_by_puuid: dict[str, str] = {}
        team_b_uid_by_puuid: dict[str, str] = {}
        for p in match_doc.get("team_a", []):
            riot = repository.get_riot_account(self.db, guild.id, str(p["id"]))
            if riot and riot.get("puuid"):
                team_a_uid_by_puuid[riot["puuid"]] = str(p["id"])
        for p in match_doc.get("team_b", []):
            riot = repository.get_riot_account(self.db, guild.id, str(p["id"]))
            if riot and riot.get("puuid"):
                team_b_uid_by_puuid[riot["puuid"]] = str(p["id"])

        expected = set(team_a_uid_by_puuid) | set(team_b_uid_by_puuid)
        if len(expected) < 10:
            return None

        after = match_doc.get("created_at") or match_doc.get("validated_at")
        # `find_henrik_custom_match` fait un appel HTTP synchrone (`requests`).
        # On l'execute dans un thread pour ne pas bloquer l'event loop Discord
        # pendant le timeout (jusqu'a 10s par appel).
        summary = await asyncio.to_thread(
            find_henrik_custom_match,
            self.henrik_client,
            region=str(leader_riot.get("region", "eu")),
            leader_name=str(leader_riot.get("name", "")),
            leader_tag=str(leader_riot.get("tag", "")),
            expected_puuids=expected,
            after=after,
        )
        if summary is None:
            return None

        verified = compute_acs_multipliers(
            summary,
            team_a_uid_by_puuid=team_a_uid_by_puuid,
            team_b_uid_by_puuid=team_b_uid_by_puuid,
        )
        return {p.user_id: p.multiplier for p in verified.performances}

    # ── Loop periodique (1 min) ──────────────────────────────────
    @tasks.loop(minutes=1)
    async def _timeout_loop(self):
        try:
            await self.check_vote_timeouts()
        except Exception as e:
            print(f"[match] check_vote_timeouts a leve : {e}")
        try:
            await self.check_henrik_verifications()
        except Exception as e:
            print(f"[match] check_henrik_verifications a leve : {e}")

    @_timeout_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        self._timeout_loop.start()

    async def cog_unload(self):
        self._timeout_loop.cancel()


async def setup(
    bot: commands.Bot,
    db,
    *,
    rng: random.Random | None = None,
    henrik_client: HenrikDevClient | None = None,
) -> MatchCog:
    cog = MatchCog(bot, db, rng=rng, henrik_client=henrik_client)
    await bot.add_cog(cog)
    bot.add_view(cog.vote_view)
    return cog
