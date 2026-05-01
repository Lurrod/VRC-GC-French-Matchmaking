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

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Mapping

import discord
from discord import app_commands
from discord.ext import commands, tasks

import asyncio

logger = logging.getLogger(__name__)

from cogs.queue_v2 import _grant_match_role, _revoke_match_role, _revoke_queue_role

MATCH_ROLE_CLEANUP_DELAY_SECONDS: Final[int] = 60
# Ecart d'ELO max entre le joueur sortant et le remplacant. Au-dela, on
# refuse le /match-replace : les equipes du match en cours seraient trop
# desequilibrees pour que le resultat reflete une vraie perf des joueurs.
MAX_REPLACE_ELO_DIFF: Final[int] = 500
MATCH_HOST_ROLE_NAME: Final[str]              = "Match Host"
MATCH_HOST_CLEANUP_DELAY_SECONDS: Final[int]  = 600  # 10 min apres validation

from services import repository
from services.elo_updater import (
    apply_match_validation,
    MatchEloOutcome,
)
from services.leaderboard_refresh import refresh_leaderboard_channel
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

# Circuit breaker Henrik : si N appels consecutifs echouent, on suspend
# les tentatives pendant T minutes pour eviter de saturer les threads
# (chaque appel = ~12s avec retries) et de polluer les logs.
HENRIK_CIRCUIT_FAIL_THRESHOLD: Final[int]   = 3
HENRIK_CIRCUIT_OPEN_MINUTES: Final[int]     = 5

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
        match = await asyncio.to_thread(
            repository.get_match_by_message,
            self.db, inter.guild_id, inter.message.id,
        )
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

        # 4) Enregistre le vote (ecrase un vote precedent). CAS sur
        # status=pending : si le match a ete annule/conteste/valide
        # entre-temps, le vote est rejete proprement.
        updated = await asyncio.to_thread(
            repository.add_match_vote,
            self.db, inter.guild_id, match["_id"], inter.user.id, choice,
        )
        if updated is None:
            await inter.response.send_message(
                "❌ Ce match n'est plus en cours de vote (annule, conteste ou deja valide).",
                ephemeral=True,
            )
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
            transitioned_doc = await asyncio.to_thread(
                lambda: repository.transition_match_status(
                    self.db, inter.guild_id, match["_id"],
                    from_status="pending", to_status=target_status,
                ),
            )
            if transitioned_doc is not None:
                updated = transitioned_doc
            else:
                # Un autre vote concurrent a deja valide. On re-fetch pour
                # afficher l'etat reel sans tirer `on_validated` de notre cote.
                fetched = await asyncio.to_thread(
                    repository.get_match,
                    self.db, inter.guild_id, match["_id"],
                )
                updated = fetched or updated

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
                logger.exception("[vote] on_validated a leve")

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
        # Circuit breaker Henrik : suspend les appels apres N echecs consecutifs.
        # `_henrik_lock` serialise les transitions du compteur/ouverture
        # quand plusieurs verifications tournent en parallele
        # (asyncio.gather sur les guilds).
        self._henrik_consecutive_failures: int = 0
        self._henrik_circuit_open_until: datetime | None = None
        self._henrik_lock: asyncio.Lock = asyncio.Lock()

    # ── Branchement queue full ───────────────────────────────────
    async def on_queue_full(self, interaction: discord.Interaction, queue_doc: dict):
        guild      = interaction.guild
        player_ids = [str(uid) for uid in queue_doc.get("players", [])]

        # Batch 2 requetes Mongo au lieu de 20 (N+1) : on fetch les
        # 10 comptes Riot et les 10 docs ELO en une seule requete chacune.
        # Toutes les ops Mongo sont regroupees dans un seul thread pour
        # ne pas geler l'event loop pendant la formation du match.
        elo_col  = repository.get_elo_col(self.db, guild.id)
        riot_col = repository.get_riot_col(self.db, guild.id)

        def _batch_fetch() -> tuple[dict[str, dict], dict[str, int]]:
            riot_map: dict[str, dict] = {}
            elo_map:  dict[str, int]  = {}
            for doc in riot_col.find({"_id": {"$in": player_ids}}):
                riot_map[str(doc["_id"])] = dict(doc)
            for doc in elo_col.find({"_id": {"$in": player_ids}}):
                elo_map[str(doc["_id"])] = int(doc.get("elo", 0))
            return riot_map, elo_map

        riot_accounts, bot_elos = await asyncio.to_thread(_batch_fetch)

        member_names: dict[str, str] = {}
        for uid in player_ids:
            member = guild.get_member(int(uid))
            if member:
                member_names[uid] = member.display_name

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

        # Ordre de mise en place : on persiste le match (BDD) AVANT
        # d'annoncer sur Discord. Si la persistance echoue (Mongo down,
        # timeout), on ne veut PAS que les 10 joueurs voient un message
        # "Match trouve !" sans match doc associe (boutons morts,
        # /match-cancel ne trouve rien).
        #
        # Etape 1 : persister le match avec message_id=None. C'est le
        # point d'engagement : apres ca, le state machine du match a
        # une source de verite.
        match_id = await asyncio.to_thread(
            repository.create_match,
            self.db,
            guild_id=guild.id,
            team_a=serialize_team(plan.teams.team_a),
            team_b=serialize_team(plan.teams.team_b),
            map_name=plan.map_name,
            lobby_leader_id=plan.lobby_leader.id,
            category_name=plan.category_name,
            message_id=None,
            channel_id=prep_channel.id,
        )

        # Etape 2 : envoyer l'annonce. Le @mention pingera les joueurs
        # en notification meme si match-preparation est gate par le
        # role (ils verront le message une fois le role grant).
        mentions = " ".join(f"<@{p.id}>" for p in players)
        embed    = build_match_embed(plan, guild.name)
        try:
            msg = await prep_channel.send(
                content=f"🎯 Match trouve ! {mentions}",
                embed=embed,
                view=self.vote_view,
            )
        except Exception:
            # L'annonce a echoue : on annule le match doc fraichement
            # cree pour eviter un orphelin que personne ne peut voter
            # (pas de message_id => VoteView introuvable).
            logger.exception("[match] prep_channel.send a leve, rollback match doc")
            matches_col = repository.get_matches_col(self.db, guild.id)
            await asyncio.to_thread(
                matches_col.delete_one, {"_id": match_id},
            )
            await self._fail(
                interaction, queue_doc,
                "Echec de l'envoi de l'annonce match. Match annule.",
            )
            return None

        # Etape 3 : associer le message_id au match doc. Sans ca,
        # `get_match_by_message` (utilise par VoteView) ne retrouve pas
        # le match au moment du vote.
        matches_col = repository.get_matches_col(self.db, guild.id)
        await asyncio.to_thread(
            matches_col.update_one,
            {"_id": match_id}, {"$set": {"message_id": msg.id}},
        )

        # Etape 3 : vider la queue immediatement apres la persistance.
        # Empêche un re-trigger eventuel d'on_queue_full sur la meme queue.
        await asyncio.to_thread(
            repository.delete_active_queue, self.db, guild.id,
        )

        # Etape 4 : grants de role (best-effort). Crash ici laisse roles
        # partiels mais le match doc existe -> /match-cancel nettoie.
        for uid in player_ids:
            member = guild.get_member(int(uid))
            if member is None:
                continue
            await _revoke_queue_role(member)
            await _grant_match_role(member, free_cat_name)

        leader_member = guild.get_member(int(plan.lobby_leader.id))
        if leader_member is not None:
            await _grant_match_role(leader_member, MATCH_HOST_ROLE_NAME)

        # Etape 5 : deplacement vocal Waiting Room -> Waiting Match.
        await self._move_players_to_match_vc(guild, free_cat_name, player_ids)

        # Etape 6 : repose setup-queue (best-effort).
        queue_cog = self.bot.get_cog("QueueCog")
        if queue_cog is not None:
            try:
                await queue_cog.post_queue_message(queue_channel)
            except Exception as e:
                logger.exception("[match] echec re-post setup-queue")
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
        channel = None
        try:
            channel = interaction.guild.get_channel(int(queue_doc["channel_id"]))
            if channel:
                await channel.send(
                    f"⚠️ {reason} Une nouvelle queue a ete reposee.",
                )
        except Exception as e:
            logger.exception("[match] _fail send a leve")
        # Repose une queue fraiche pour eviter d'obliger l'admin a refaire
        # /setup-queue manuellement apres chaque echec de formation.
        if channel is not None:
            queue_cog = self.bot.get_cog("QueueCog")
            if queue_cog is not None:
                try:
                    await queue_cog.post_queue_message(channel)
                except Exception as e:
                    logger.exception("[match] _fail re-post queue a leve")

    # ── Hook : vote valide ───────────────────────────────────────
    async def _on_match_validated(self, inter, match_doc) -> None:
        """
        Vote valide : on NE TOUCHE PAS encore a l'ELO.
        L'ELO sera applique en une seule passe par `_verify_match`
        apres ~HENRIK_VERIFY_DELAY_MINUTES (avec ponderation ACS si
        HenrikDev a retrouve le custom, plat sinon).

        Ordre d'exécution : on planifie les nettoyages de roles AVANT
        toute operation risquee (send Discord), pour garantir qu'un crash
        sur l'annonce ne laisse jamais les roles "Match #N" / "Match Host"
        attribues a vie.
        """
        guild = getattr(inter, "guild", None)

        # Cleanups de roles persistes en base : `_timeout_loop` les
        # appliquera quand l'echeance sera passee. Survit au redemarrage
        # du bot (les anciens `asyncio.create_task` etaient perdus si le
        # bot crashait dans la fenetre de 60s/600s).
        if guild is not None:
            now = datetime.now(timezone.utc)
            try:
                await asyncio.to_thread(
                    repository.schedule_role_cleanups,
                    self.db, guild.id, match_doc["_id"],
                    match_role_at=now + timedelta(seconds=MATCH_ROLE_CLEANUP_DELAY_SECONDS),
                    host_role_at=now + timedelta(seconds=MATCH_HOST_CLEANUP_DELAY_SECONDS),
                )
            except Exception as e:
                logger.exception("[match] schedule role cleanups a leve")

        # 3) Annonce best-effort. Toute erreur ici ne doit pas empecher
        #    le cleanup de tourner.
        if guild is None:
            return
        try:
            elo_log_channel = discord.utils.get(
                guild.text_channels, name="elo-adding",
            )
        except Exception as e:
            logger.exception("[match] lookup elo-adding a leve")
            return
        if elo_log_channel is None:
            return
        try:
            await elo_log_channel.send(
                f"⏳ Match valide ({match_doc.get('status')}). "
                f"Verification HenrikDev a partir de {HENRIK_VERIFY_DELAY_MINUTES} min "
                f"(retry chaque minute, abandon a {HENRIK_VERIFY_TIMEOUT_MINUTES} min)."
            )
        except discord.Forbidden:
            # Le bot n'a pas la permission Send Messages dans #elo-adding.
            # C'est un probleme de config recoltable par l'operateur.
            logger.warning(
                "[match] envoi annonce Henrik refuse (Forbidden) sur #%s "
                "guild=%s — verifier les permissions du bot.",
                elo_log_channel.name, guild.id,
            )
        except discord.HTTPException:
            # Erreur transitoire Discord (5xx, rate limit). On log mais
            # on n'echoue pas le flux ELO.
            logger.exception("[match] envoi annonce Henrik HTTP error")
        except Exception:
            logger.exception("[match] envoi annonce attente Henrik a leve")

    async def _process_role_cleanups(self, *, now: datetime | None = None) -> int:
        """Traite les cleanups de roles dont l'echeance persistee est passee.

        Reprend automatiquement les cleanups perdus suite a un redemarrage
        du bot. Le claim atomique evite les double-traitements en cas de
        ticks concurrents."""
        now = now or datetime.now(timezone.utc)
        # Parallelisation per-guild : meme principe que les autres loops.
        results = await asyncio.gather(
            *[self._process_role_cleanups_for_guild(g, now) for g in self.bot.guilds],
            return_exceptions=True,
        )
        processed = 0
        for r in results:
            if isinstance(r, Exception):
                logger.info(f"[match] _process_role_cleanups (guild) a leve : {r!r}")
                continue
            processed += r
        return processed

    async def _process_role_cleanups_for_guild(self, guild, now: datetime) -> int:
        processed = 0
        # Cleanup role "Match #N" pour les 10 joueurs.
        pending = await asyncio.to_thread(
            repository.find_pending_match_role_cleanups,
            self.db, guild.id, now,
        )
        for match in pending:
            claimed = await asyncio.to_thread(
                repository.claim_match_role_cleanup,
                self.db, guild.id, match["_id"],
            )
            if not claimed:
                continue
            category_name = match.get("category_name")
            if not category_name:
                continue
            for team_key in ("team_a", "team_b"):
                for player in match.get(team_key, []):
                    uid = player.get("id")
                    if uid is None:
                        continue
                    member = guild.get_member(int(uid))
                    if member is not None:
                        await _revoke_match_role(member, category_name)
            processed += 1

        # Cleanup role "Match Host" pour le lobby leader.
        pending_host = await asyncio.to_thread(
            repository.find_pending_host_role_cleanups,
            self.db, guild.id, now,
        )
        for match in pending_host:
            claimed = await asyncio.to_thread(
                repository.claim_host_role_cleanup,
                self.db, guild.id, match["_id"],
            )
            if not claimed:
                continue
            leader_id = match.get("lobby_leader_id")
            if leader_id is None:
                continue
            member = guild.get_member(int(leader_id))
            if member is not None:
                await _revoke_match_role(member, MATCH_HOST_ROLE_NAME)
            processed += 1
        return processed

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

        # Traitement parallelise par guild : sur N guilds, l'execution
        # sequentielle attendrait la fin du scan + transitions de chaque
        # guild avant de passer a la suivante. asyncio.gather rend le
        # tick borne par la guild la plus lente, pas par leur somme.
        results = await asyncio.gather(
            *[self._check_vote_timeouts_for_guild(g, cutoff) for g in self.bot.guilds],
            return_exceptions=True,
        )
        flagged = 0
        for r in results:
            if isinstance(r, Exception):
                logger.info(f"[match] check_vote_timeouts (guild) a leve : {r!r}")
                continue
            flagged += r
        return flagged

    async def _check_vote_timeouts_for_guild(self, guild, cutoff: datetime) -> int:
        flagged = 0
        col = repository.get_matches_col(self.db, guild.id)
        # Scan en thread : `find().toList()` est bloquant et peut iterer
        # sur N matches, gelant l'event loop Discord.
        stale = await asyncio.to_thread(
            lambda c=col: list(c.find({
                "status":     "pending",
                "created_at": {"$lt": cutoff},
            })),
        )
        for match in stale:
            # Re-fetch atomique juste avant la transition pour eviter
            # une race avec un vote qui franchirait le seuil entre le
            # scan initial et maintenant. Sans ce re-fetch, on lirait
            # `match.get("votes")` du snapshot stale -> le tick pourrait
            # transitionner pending->contested alors qu'un vote concurrent
            # vient d'atteindre la majorite, laissant le match coince
            # en `contested` avec ELO jamais applique.
            fresh = await asyncio.to_thread(col.find_one, {"_id": match["_id"]})
            if not fresh or fresh.get("status") != "pending":
                continue
            votes   = fresh.get("votes", {})
            count_a = sum(1 for v in votes.values() if v == "a")
            count_b = sum(1 for v in votes.values() if v == "b")
            # Auto-reparation : on backdate `validated_at` au moment
            # du `created_at` du match. Sans ce backdate, le delai
            # Henrik (~5min apres validated_at) repartirait de 0 ;
            # or le match a deja ete cree il y a > VOTE_TIMEOUT_MINUTES,
            # le custom HenrikDev est deja indexe et la verification
            # peut tourner immediatement au prochain tick.
            repaired_validated_at = fresh.get("created_at")
            if count_a >= MAJORITY_THRESHOLD:
                # Un match peut avoir atteint 7+ votes sans transition
                # (ex: crash bot entre l'ecriture du vote et set_match_status).
                # On recupere ; check_henrik_verifications appliquera
                # l'ELO au prochain tick.
                await asyncio.to_thread(
                    repository.transition_match_status,
                    self.db, guild.id, match["_id"],
                    from_status="pending", to_status="validated_a",
                    validated_at=repaired_validated_at,
                )
                continue
            if count_b >= MAJORITY_THRESHOLD:
                await asyncio.to_thread(
                    repository.transition_match_status,
                    self.db, guild.id, match["_id"],
                    from_status="pending", to_status="validated_b",
                    validated_at=repaired_validated_at,
                )
                continue
            transitioned = await asyncio.to_thread(
                repository.transition_match_status,
                self.db, guild.id, match["_id"],
                from_status="pending", to_status="contested",
            )
            if transitioned is None:
                continue
            await self._handle_timeout(guild, match)
            flagged += 1
        return flagged

    async def _handle_timeout(self, guild, match) -> None:
        # Note : la transition vers "contested" est faite par
        # check_vote_timeouts via transition_match_status (CAS atomique).
        # On entre ici uniquement si la transition a reussi.

        # Retire immediatement le role "Match Host" au lobby leader :
        # le vote n'a pas abouti dans VOTE_TIMEOUT_MINUTES, l'admin reprend la main.
        leader_id = match.get("lobby_leader_id")
        if leader_id is not None:
            leader_member = guild.get_member(int(leader_id))
            if leader_member is not None:
                await _revoke_match_role(leader_member, MATCH_HOST_ROLE_NAME)

        # Retire le role "Match #N" aux 10 joueurs : le match est conteste,
        # ils ne devraient plus voir le salon match-preparation. La categorie
        # se libere pour une nouvelle queue. Sans ce nettoyage, les rôles
        # persistaient jusqu'a un /match-cancel manuel.
        category_name = match.get("category_name")
        if category_name:
            for team_key in ("team_a", "team_b"):
                for player in match.get(team_key, []):
                    uid = player.get("id")
                    if uid is None:
                        continue
                    member = guild.get_member(int(uid))
                    if member is not None:
                        await _revoke_match_role(member, category_name)

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
            logger.exception("[match] _handle_timeout send a leve")

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

        # Parallelisation per-guild : meme principe que check_vote_timeouts.
        results = await asyncio.gather(
            *[
                self._check_henrik_verifications_for_guild(g, start_cutoff, timeout_cutoff)
                for g in self.bot.guilds
            ],
            return_exceptions=True,
        )
        processed = 0
        for r in results:
            if isinstance(r, Exception):
                logger.info(f"[match] check_henrik_verifications (guild) a leve : {r!r}")
                continue
            processed += r
        return processed

    async def _check_henrik_verifications_for_guild(
        self, guild, start_cutoff: datetime, timeout_cutoff: datetime,
    ) -> int:
        processed = 0
        # Scan bloquant -> thread pour ne pas geler l'event loop.
        stale = await asyncio.to_thread(
            repository.find_validated_unverified,
            self.db, guild.id, start_cutoff,
        )
        for match in stale:
            validated_at = match.get("validated_at") or match.get("created_at")
            timed_out = bool(
                validated_at is not None and validated_at <= timeout_cutoff
            )
            try:
                await self._verify_match(guild, match, force_apply=timed_out)
            except Exception as e:
                logger.exception("[match] verify_match a leve")
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
        claimed = await asyncio.to_thread(
            repository.claim_match_for_elo,
            self.db, guild.id, match_doc["_id"],
        )
        if claimed is None:
            return  # Deja applique par un tick precedent.

        try:
            outcome = await asyncio.to_thread(
                apply_match_validation,
                self.db, guild.id, match_doc, multipliers=multipliers,
            )
        except Exception as e:
            logger.exception("[match] apply_match_validation a leve")
            # Rollback du claim pour permettre un retry au prochain tick.
            await asyncio.to_thread(
                repository.release_elo_claim, self.db, guild.id, match_doc["_id"],
            )
            return

        await asyncio.to_thread(
            repository.set_match_henrik_verified,
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
                logger.exception("[match] envoi recap ELO a leve")

        bot_user = self.bot.user
        if bot_user is not None:
            try:
                await refresh_leaderboard_channel(guild, self.db, bot_user.id)
            except Exception as e:
                logger.exception("[match] refresh leaderboard a leve")

    async def _fetch_henrik_multipliers(
        self, guild, match_doc: dict,
    ) -> dict[str, float] | None:
        """Tente de retrouver le custom HenrikDev et de calculer les
        multiplicateurs ACS. Retourne None si pas exploitable."""
        # 10 lookups riot (le leader est l'un des 10 joueurs choisi
        # aleatoirement, on le recupere au passage). Regroupes dans un
        # seul thread pour eviter de geler l'event loop pendant ~10x10ms.
        def _gather_riot_accounts() -> tuple[Mapping[str, Any] | None, dict[str, str], dict[str, str]]:
            leader_uid_local = str(match_doc.get("lobby_leader_id"))
            leader: Mapping[str, Any] | None = None
            a_map: dict[str, str] = {}
            b_map: dict[str, str] = {}
            for player in match_doc.get("team_a", []):
                pid = str(player["id"])
                r = repository.get_riot_account(self.db, guild.id, pid)
                if r and r.get("puuid"):
                    a_map[r["puuid"]] = pid
                if pid == leader_uid_local:
                    leader = r
            for player in match_doc.get("team_b", []):
                pid = str(player["id"])
                r = repository.get_riot_account(self.db, guild.id, pid)
                if r and r.get("puuid"):
                    b_map[r["puuid"]] = pid
                if pid == leader_uid_local:
                    leader = r
            # Fallback : si le leader n'est plus dans les 10 (apres un
            # /match-replace par exemple), lookup direct.
            if leader is None:
                leader = repository.get_riot_account(self.db, guild.id, leader_uid_local)
            return leader, a_map, b_map

        leader_riot, team_a_uid_by_puuid, team_b_uid_by_puuid = await asyncio.to_thread(
            _gather_riot_accounts,
        )
        if not leader_riot:
            return None

        expected = set(team_a_uid_by_puuid) | set(team_b_uid_by_puuid)
        if len(expected) < 10:
            return None

        after = match_doc.get("created_at") or match_doc.get("validated_at")

        # Circuit breaker : si HenrikDev a echoue 3x de suite recemment,
        # on saute pendant 5 min. Sans ce garde, chaque tick (1 min)
        # relance N matches stale × 12s de retries chacun, gelant le
        # ThreadPoolExecutor et faisant overlap les ticks.
        # Lecture serialisee : sans le lock, plusieurs guilds en
        # parallele pouvaient observer un etat intermediaire (cf #17).
        now = datetime.now(timezone.utc)
        async with self._henrik_lock:
            circuit_open = (
                self._henrik_circuit_open_until is not None
                and now < self._henrik_circuit_open_until
            )
        if circuit_open:
            return None

        # `find_henrik_custom_match` fait un appel HTTP synchrone (`requests`).
        # On l'execute dans un thread pour ne pas bloquer l'event loop Discord
        # pendant le timeout (jusqu'a 10s par appel).
        try:
            summary = await asyncio.to_thread(
                find_henrik_custom_match,
                self.henrik_client,
                region=str(leader_riot.get("riot_region", "eu")),
                leader_name=str(leader_riot.get("riot_name", "")),
                leader_tag=str(leader_riot.get("riot_tag", "")),
                expected_puuids=expected,
                after=after,
            )
        except Exception as e:
            async with self._henrik_lock:
                self._henrik_consecutive_failures += 1
                failures = self._henrik_consecutive_failures
                if failures >= HENRIK_CIRCUIT_FAIL_THRESHOLD:
                    self._henrik_circuit_open_until = now + timedelta(
                        minutes=HENRIK_CIRCUIT_OPEN_MINUTES,
                    )
                    just_opened = True
                else:
                    just_opened = False
            if just_opened:
                logger.warning(
                    "[match] Henrik circuit OPEN apres %d echecs consecutifs. "
                    "Reprise dans %d min. Derniere erreur : %r",
                    failures, HENRIK_CIRCUIT_OPEN_MINUTES, e,
                )
            else:
                logger.error(
                    "[match] Henrik echec (%d/%d) : %r",
                    failures, HENRIK_CIRCUIT_FAIL_THRESHOLD, e,
                    exc_info=True,
                )
            return None
        # Succes : reset le compteur d'echecs et ferme le circuit.
        async with self._henrik_lock:
            if (
                self._henrik_consecutive_failures > 0
                or self._henrik_circuit_open_until is not None
            ):
                self._henrik_consecutive_failures = 0
                self._henrik_circuit_open_until = None
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
            logger.exception("[match] check_vote_timeouts a leve")
        try:
            await self.check_henrik_verifications()
        except Exception as e:
            logger.exception("[match] check_henrik_verifications a leve")
        try:
            await self._process_role_cleanups()
        except Exception as e:
            logger.exception("[match] _process_role_cleanups a leve")

    @_timeout_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()

    @_timeout_loop.error
    async def _timeout_loop_error(self, error: BaseException) -> None:
        """Filet de securite : `tasks.loop` meurt silencieusement si une
        exception remonte hors du try/except interne du tick. Sans ce
        handler, les votes en timeout ne seraient plus jamais traites
        jusqu'au prochain redemarrage du bot."""
        # logger.error avec exc_info=tuple : preserve la stack du `error`
        # passe en argument (logger.exception() utilise sys.exc_info() qui
        # n'est pas l'`error` courant ici).
        logger.error(
            "[match] _timeout_loop a leve : %r", error,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            self._timeout_loop.restart()
        except Exception as e:
            logger.exception("[match] _timeout_loop.restart() a leve")

    # ── Slash commands admin (cancel / replace) ─────────────────
    @app_commands.command(
        name="match-cancel",
        description="Annule le match en cours dans ce salon (admin)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        # CAS atomique : si un vote concurrent valide le match ou si
        # _verify_match claim l'ELO entre la lecture et l'ecriture, le
        # cancel echoue proprement plutot que de creer un etat incoherent.
        match = await asyncio.to_thread(
            repository.cancel_match_atomically,
            self.db, interaction.guild_id,
            channel_id=interaction.channel_id,
        )
        if not match:
            await interaction.followup.send(
                "❌ Aucun match annulable trouve dans ce salon "
                "(status pending/validated/contested et ELO non applique).",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        category_name = match.get("category_name")
        for team_key in ("team_a", "team_b"):
            for player in match.get(team_key, []):
                uid = player.get("id")
                if uid is None:
                    continue
                member = guild.get_member(int(uid))
                if member is None:
                    continue
                if category_name:
                    await _revoke_match_role(member, category_name)

        leader_id = match.get("lobby_leader_id")
        if leader_id is not None:
            leader = guild.get_member(int(leader_id))
            if leader is not None:
                await _revoke_match_role(leader, MATCH_HOST_ROLE_NAME)

        try:
            msg_id = match.get("message_id")
            if msg_id and interaction.channel:
                msg = await interaction.channel.fetch_message(int(msg_id))
                await msg.edit(view=None)
        except Exception as e:
            logger.exception("[match-cancel] retrait view a leve")

        await interaction.followup.send(
            f"✅ Match annule. Categorie `{category_name or '?'}` liberee, "
            "roles retires.",
            ephemeral=True,
        )

    @app_commands.command(
        name="match-replace",
        description="Remplace un joueur dans le match en cours (admin)",
    )
    @app_commands.describe(
        quitter="Joueur a remplacer",
        remplacant="Nouveau joueur (doit avoir un compte Riot lie)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match_replace(
        self,
        interaction: discord.Interaction,
        quitter: discord.Member,
        remplacant: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if quitter.id == remplacant.id:
            await interaction.followup.send(
                "❌ Impossible de remplacer un joueur par lui-meme.",
                ephemeral=True,
            )
            return

        matches_col = repository.get_matches_col(self.db, interaction.guild_id)
        match = await asyncio.to_thread(
            matches_col.find_one,
            {"channel_id": interaction.channel_id, "status": "pending"},
        )
        if not match:
            await interaction.followup.send(
                "❌ Aucun match en cours (status pending) dans ce salon.",
                ephemeral=True,
            )
            return

        team_key: str | None = None
        for tk in ("team_a", "team_b"):
            if any(int(p.get("id", 0)) == quitter.id for p in match.get(tk, [])):
                team_key = tk
                break
        if team_key is None:
            await interaction.followup.send(
                f"❌ {quitter.mention} n'est pas dans ce match.",
                ephemeral=True,
            )
            return

        if any(
            int(p.get("id", 0)) == remplacant.id
            for tk in ("team_a", "team_b")
            for p in match.get(tk, [])
        ):
            await interaction.followup.send(
                f"❌ {remplacant.mention} est deja dans ce match.",
                ephemeral=True,
            )
            return

        riot = await asyncio.to_thread(
            repository.get_riot_account,
            self.db, interaction.guild_id, remplacant.id,
        )
        if not riot:
            await interaction.followup.send(
                f"❌ {remplacant.mention} n'a pas de compte Riot lie "
                "(`/link-riot Pseudo#TAG`).",
                ephemeral=True,
            )
            return

        elo_col = repository.get_elo_col(self.db, interaction.guild_id)
        elo_doc = await asyncio.to_thread(
            elo_col.find_one, {"_id": str(remplacant.id)},
        )
        new_elo = int(elo_doc.get("elo", 0)) if elo_doc else 0

        # Refuse le replace si l'ecart est trop grand : les equipes
        # avaient ete equilibrees au moment de la formation, un swap
        # avec un ecart > MAX_REPLACE_ELO_DIFF casse cet equilibre et
        # l'ELO post-match ne refletera pas la vraie perf.
        quitter_player = next(
            (p for p in match[team_key] if int(p.get("id", 0)) == quitter.id),
            None,
        )
        quitter_elo = int(quitter_player.get("elo", 0)) if quitter_player else 0
        elo_diff = abs(quitter_elo - new_elo)
        if elo_diff > MAX_REPLACE_ELO_DIFF:
            await interaction.followup.send(
                f"❌ Ecart d'ELO trop important : {quitter.mention} "
                f"({quitter_elo}) vs {remplacant.mention} ({new_elo}) "
                f"-> diff={elo_diff} > {MAX_REPLACE_ELO_DIFF}. Les equipes "
                "seraient desequilibrees. Annule le match (`/match-cancel`) "
                "et reforme la queue.",
                ephemeral=True,
            )
            return

        new_player = {
            "id":   remplacant.id,
            "name": remplacant.display_name,
            "elo":  new_elo,
        }
        new_team = [
            new_player if int(p.get("id", 0)) == quitter.id else p
            for p in match[team_key]
        ]
        # CAS sur le status : si entre temps un vote a fait passer le
        # match en validated_*/contested, on ne touche plus aux equipes.
        result = await asyncio.to_thread(
            matches_col.update_one,
            {"_id": match["_id"], "status": "pending"},
            {"$set": {team_key: new_team}},
        )
        if result.modified_count != 1:
            await interaction.followup.send(
                "❌ Le match a ete valide ou annule entre temps. "
                "Replace abandonne.",
                ephemeral=True,
            )
            return

        category_name = match.get("category_name")
        if category_name:
            await _revoke_match_role(quitter, category_name)
            await _grant_match_role(remplacant, category_name)

        await interaction.followup.send(
            f"✅ {quitter.mention} remplace par {remplacant.mention} dans "
            f"`{team_key}`. Roles ajustes.",
            ephemeral=True,
        )

    @match_cancel.error
    @match_replace.error
    async def _admin_perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            try:
                await inter.response.send_message(
                    "🚫 Reserve aux administrateurs.", ephemeral=True,
                )
            except discord.InteractionResponded:
                await inter.followup.send(
                    "🚫 Reserve aux administrateurs.", ephemeral=True,
                )

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
