"""
Helper pour rafraichir automatiquement le leaderboard apres une modification
d'ELO. Supprime le dernier message leaderboard du bot dans le salon
`#leaderboard` puis poste une nouvelle image generee a partir des donnees
courantes de la base.

Utilise par :
  - cogs/match.py (apres application de l'ELO post-match)
  - bot.py /elomodify, /resetelo (slash + prefix)
"""

from __future__ import annotations

import logging

import asyncio
from datetime import datetime, timezone
from typing import Optional, Tuple

import discord

from leaderboard_img import generate_leaderboard
from services import repository

logger = logging.getLogger(__name__)


LEADERBOARD_CHANNEL_NAME = "leaderboard"
LEADERBOARD_FILENAME     = "leaderboard.png"
PAGE_SIZE                = 15

# Debounce per-guild : evite de regenerer + reposter le leaderboard
# en rafale apres N modifs ELO consecutives (ex: /win + /lose + autres
# admin ops). Discord rate-limit le delete+send (~5/min/channel).
_REFRESH_DEBOUNCE_SECONDS: int = 30
_LAST_REFRESH_AT: dict[int, datetime] = {}


async def build_leaderboard_payload(
    guild: discord.Guild, db, *,
    with_view: bool = True,
    view_timeout: float | None = 300,
) -> Tuple[Optional[discord.File], Optional[discord.ui.View]]:
    """Retourne (file, view) prets a etre envoyes. (None, None) si vide.

    `with_view=False` -> pas de pagination (post statique).
    `view_timeout` -> duree de vie des boutons (None = jamais expire,
    pratique pour les posts permanents dans `#leaderboard`)."""
    col  = repository.get_elo_col(db, guild.id)
    # Tri deterministique : ELO desc, puis wins desc (recompense l'activite
    # parmi les ex-aequo), puis _id asc comme tie-breaker final.
    # Sans tie-breaker, l'ordre des ex-aequo est non-deterministique cote
    # MongoDB et /stats peut afficher des rangs incoherents avec le
    # leaderboard.
    docs = list(col.find().sort([("elo", -1), ("wins", -1), ("_id", 1)]))
    if not docs:
        return None, None

    all_players = []
    rank = 1
    for doc in docs:
        uid = doc["_id"]
        try:
            member = guild.get_member(int(uid))
        except (TypeError, ValueError):
            member = None
        # Skip les joueurs ayant quitte le serveur (kick/ban/leave). Sans
        # ce filtre, un joueur seede a 2000 ELO via /link-riot puis parti
        # squatte le leaderboard a vie avec un avatar par defaut.
        if member is None:
            continue
        ava_url = str(member.display_avatar.replace(format="png", size=64).url)
        display_name = member.display_name or doc.get("name", uid)
        all_players.append({
            "rank":       rank,
            "name":       display_name,
            "elo":        doc["elo"],
            "wins":       doc.get("wins", 0),
            "losses":     doc.get("losses", 0),
            "kills":      doc.get("kills", 0),
            "deaths":     doc.get("deaths", 0),
            "avatar_url": ava_url,
        })
        rank += 1

    total_pages = max(1, (len(all_players) + PAGE_SIZE - 1) // PAGE_SIZE)
    loop = asyncio.get_running_loop()

    async def build_page(page: int) -> discord.File:
        start = page * PAGE_SIZE
        chunk = all_players[start:start + PAGE_SIZE]
        buf   = await loop.run_in_executor(
            None,
            lambda: generate_leaderboard(chunk, server_name=guild.name),
        )
        return discord.File(buf, filename=LEADERBOARD_FILENAME)

    class LeaderboardView(discord.ui.View):
        def __init__(self, page: int):
            super().__init__(timeout=view_timeout)
            self.page = page
            self.update_buttons()

        def update_buttons(self):
            self.prev_btn.disabled = self.page == 0
            self.next_btn.disabled = self.page >= total_pages - 1
            self.page_btn.label    = f"Page {self.page + 1} / {total_pages}"

        async def _go(self, inter: discord.Interaction, new_page: int):
            if new_page < 0 or new_page >= total_pages:
                if not inter.response.is_done():
                    await inter.response.defer()
                return
            self.page = new_page
            self.update_buttons()
            try:
                if not inter.response.is_done():
                    await inter.response.defer()
                file = await build_page(self.page)
                await inter.followup.edit_message(
                    message_id=inter.message.id,
                    attachments=[file],
                    view=self,
                )
            except Exception:
                logger.exception("leaderboard_refresh exception")
                try:
                    await inter.followup.send(
                        "Erreur lors du changement de page. Réessaie.",
                        ephemeral=True,
                    )
                except Exception:
                    pass

        @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
        async def prev_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            await self._go(inter, self.page - 1)

        @discord.ui.button(label="Page 1 / 1", style=discord.ButtonStyle.grey, disabled=True)
        async def page_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            pass

        @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
        async def next_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            await self._go(inter, self.page + 1)

    file = await build_page(0)
    if not with_view:
        return file, None
    view = LeaderboardView(page=0)
    return file, view


async def refresh_leaderboard_channel(
    guild: discord.Guild, db, bot_user_id: int,
) -> None:
    """Supprime le dernier leaderboard poste par le bot dans `#leaderboard`
    puis poste une version a jour. Silencieux en cas d'erreur (channel
    introuvable, permissions manquantes, etc.).

    Debounce : si un refresh a ete declenche pour cette guild il y a moins
    de `_REFRESH_DEBOUNCE_SECONDS`, on skip silencieusement pour eviter
    les rafales delete+send qui frappent le rate-limit Discord."""
    now = datetime.now(timezone.utc)
    last = _LAST_REFRESH_AT.get(guild.id)
    if last is not None and (now - last).total_seconds() < _REFRESH_DEBOUNCE_SECONDS:
        return
    _LAST_REFRESH_AT[guild.id] = now

    # On accepte tout salon dont le nom contient "leaderboard" (insensible
    # a la casse) pour supporter les variantes decoratives type
    # "🏆・leaderboard", "leaderboard-elo", etc. Coherent avec
    # `_is_leaderboard_channel` dans bot.py.
    needle = LEADERBOARD_CHANNEL_NAME.lower()
    chan = next(
        (c for c in guild.text_channels if needle in (c.name or "").lower()),
        None,
    )
    if chan is None:
        return

    # Suppression de l'ancien leaderboard : on prefere un fetch direct
    # via le message_id persiste plutot qu'un scan `chan.history(limit=20)`
    # qui rate l'ancien leaderboard si >= 20 messages ont ete postes
    # depuis (spam, hors-sujet, etc.) et provoque un duplicat.
    stored_id = repository.get_leaderboard_message_id(db, guild.id)
    deleted_via_stored = False
    if stored_id is not None:
        try:
            old_msg = await chan.fetch_message(stored_id)
            await old_msg.delete()
            deleted_via_stored = True
        except discord.NotFound:
            # Message deja supprime (manuellement ou par /clear). On le
            # nettoie de l'etat et on continue avec un fallback history.
            repository.clear_leaderboard_message_id(db, guild.id)
        except Exception:
            logger.exception("leaderboard_refresh exception")

    if not deleted_via_stored:
        # Fallback : ancien comportement par scan recent. Sert en
        # premier deploiement (pas encore d'etat persiste) ou apres un
        # clear manuel.
        try:
            async for msg in chan.history(limit=20):
                if msg.author.id != bot_user_id:
                    continue
                if not any(a.filename == LEADERBOARD_FILENAME for a in msg.attachments):
                    continue
                try:
                    await msg.delete()
                except Exception:
                    pass
                break
        except Exception:
            logger.exception("leaderboard_refresh exception")

    try:
        file, view = await build_leaderboard_payload(
            guild, db, view_timeout=None,
        )
    except Exception:
        logger.exception("leaderboard_refresh exception")
        return
    if file is None:
        return

    try:
        new_msg = await chan.send(file=file, view=view)
    except Exception:
        logger.exception("leaderboard_refresh exception")
        return

    # Persiste le message_id du nouveau leaderboard pour le prochain
    # refresh (evite le scan history => duplication possible).
    try:
        repository.set_leaderboard_message_id(db, guild.id, new_msg.id)
    except Exception:
        logger.exception("leaderboard_refresh exception")
