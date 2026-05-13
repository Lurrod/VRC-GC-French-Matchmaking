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
import re
from collections import OrderedDict
from datetime import datetime, UTC
from io import BytesIO

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
# Borne le cache pour eviter une fuite memoire si le bot tourne sur de
# nombreuses guilds (entree par guild_id, jamais purgee). LRU avec
# eviction FIFO du plus ancien acces au-dela de _MAX_GUILDS_TRACKED.
_MAX_GUILDS_TRACKED: int = 1024
_LAST_REFRESH_AT: OrderedDict[tuple[int, str], datetime] = OrderedDict()

# ── Cache des pages rendues ───────────────────────────────────────
# Cache lazy : la page est rendue a la 1ere consultation et stockee
# en bytes (pas en discord.File qui est single-use). A chaque cache
# hit on enveloppe les bytes dans un nouveau BytesIO + File.
#
# Invalidation : `refresh_leaderboard_channel` clear toutes les
# entrees (guild_id, queue_type, *) avant de regenerer la page 1.
# Toute mutation d'ELO passe par cette fonction -> coherence garantie
# tant que ce contrat est respecte.
#
# Cle  : (guild_id, queue_type, page_zero_indexed)
# Val  : (png_bytes, total_pages_at_render_time)
_PAGE_CACHE_MAXSIZE: int = 60   # ~3 queues * 20 pages worst case
_PAGE_CACHE: OrderedDict[tuple[int, str, int], tuple[bytes, int]] = OrderedDict()


def _cache_get(guild_id: int, queue_type: str, page: int) -> tuple[bytes, int] | None:
    key = (guild_id, queue_type, page)
    val = _PAGE_CACHE.get(key)
    if val is not None:
        _PAGE_CACHE.move_to_end(key)
    return val


def _cache_set(
    guild_id: int, queue_type: str, page: int,
    png_bytes: bytes, total_pages: int,
) -> None:
    key = (guild_id, queue_type, page)
    _PAGE_CACHE[key] = (png_bytes, total_pages)
    _PAGE_CACHE.move_to_end(key)
    while len(_PAGE_CACHE) > _PAGE_CACHE_MAXSIZE:
        _PAGE_CACHE.popitem(last=False)


def _cache_invalidate(guild_id: int, queue_type: str) -> int:
    """Supprime toutes les entrees du cache pour ce (guild, queue_type).

    Appele depuis `refresh_leaderboard_channel` quand on vient d'apprendre
    qu'un ELO a change. Retourne le nombre d'entrees supprimees (utile
    pour le debug et les tests)."""
    to_remove = [
        k for k in _PAGE_CACHE
        if k[0] == guild_id and k[1] == queue_type
    ]
    for k in to_remove:
        del _PAGE_CACHE[k]
    return len(to_remove)


def _clear_page_cache_for_tests() -> None:
    """Vide entierement le cache. Utilise uniquement par les tests pour
    isoler les cas (le cache est process-wide, donc partage entre tests)."""
    _PAGE_CACHE.clear()


_PAGE_LABEL_RE = re.compile(r"^\s*Page\s+(\d+)\s*/\s*(\d+)\s*$")
_ATTACH_FILENAME_RE = re.compile(r"^leaderboard_([a-z0-9_\-]+)\.png$", re.IGNORECASE)


class LeaderboardView(discord.ui.View):
    """Vue paginee persistante pour le leaderboard.

    Persistante = survit aux restarts du bot. Pour que les boutons
    fonctionnent apres un redemarrage, la vue doit (1) avoir des
    `custom_id` stables et (2) etre enregistree dans `on_ready` via
    `bot.add_view(LeaderboardView())`.

    L'etat par-message (queue_type, page courante) n'est PAS stocke
    cote bot : il est recupere depuis le message lui-meme :
      - `queue_type` -> attachement nomme `leaderboard_{qt}.png`
      - page courante -> label du bouton central `Page N / M`

    A chaque clic, on relit la BDD pour afficher des donnees fraiches
    (le leaderboard peut avoir bouge depuis le dernier rendu).
    """

    def __init__(
        self, *,
        page: int = 0,
        total_pages: int = 1,
        queue_type: str | None = None,
    ):
        super().__init__(timeout=None)
        self.page = page
        self.total_pages = total_pages
        self.queue_type = queue_type
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        # Ordre des children = ordre des decorateurs @discord.ui.button :
        # 0=prev_btn, 1=page_btn, 2=next_btn.
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        self.page_btn.label    = f"Page {self.page + 1} / {self.total_pages}"

    @staticmethod
    def _recover_state(message) -> tuple[str | None, int, int]:
        """Reconstitue (queue_type, page_zero_indexee, total_pages) depuis un message.

        Robuste aux mocks et messages incomplets : retourne (None, 0, 1) si
        rien d'exploitable n'est trouve.
        """
        qt: str | None = None
        attachments = getattr(message, "attachments", None)
        if isinstance(attachments, (list, tuple)):
            for att in attachments:
                fn = getattr(att, "filename", "") or ""
                m = _ATTACH_FILENAME_RE.match(fn)
                if m:
                    qt = m.group(1).lower()
                    break

        page0, total = 0, 1
        components = getattr(message, "components", None)
        if isinstance(components, (list, tuple)):
            for row in components:
                children = getattr(row, "children", None)
                if not isinstance(children, (list, tuple)):
                    continue
                for comp in children:
                    label = getattr(comp, "label", None)
                    if not isinstance(label, str):
                        continue
                    m = _PAGE_LABEL_RE.match(label)
                    if m:
                        page0 = max(0, int(m.group(1)) - 1)
                        total = max(1, int(m.group(2)))
                        return qt, page0, total
        return qt, page0, total

    async def _go(self, inter, new_page: int) -> None:
        """Navigue vers `new_page` (index 0-base, absolu)."""
        try:
            queue_type = self.queue_type
            total = self.total_pages

            # Dispatch persistant : l'instance enregistree au niveau bot n'a
            # pas d'etat par-message, on reconstruit depuis le message.
            recovered_from_message = False
            if queue_type is None:
                msg = getattr(inter, "message", None)
                if msg is None:
                    return
                qt, _, rec_total = self._recover_state(msg)
                if qt is None:
                    if not inter.response.is_done():
                        await inter.response.defer()
                    return
                queue_type = qt
                total = rec_total
                recovered_from_message = True

            if new_page < 0 or new_page >= total:
                if not inter.response.is_done():
                    await inter.response.defer()
                return

            if not inter.response.is_done():
                await inter.response.defer()

            # Import tardif : evite la dependance circulaire bot <-> services
            # au moment du chargement des modules.
            from bot import db as _db

            file, new_view = await build_leaderboard_payload(
                inter.guild, _db, queue_type, page=new_page,
            )
            if file is None:
                return

            # Ne muter `self` que si on est sur l'instance par-message
            # (queue_type initial non-None). Sur l'instance globale
            # enregistree, muter polluerait les dispatches suivants entre
            # guilds / queue_types differents.
            if not recovered_from_message:
                self.page = new_page
                if new_view is not None:
                    self.total_pages = getattr(new_view, "total_pages", total)
                self._sync_buttons()

            await inter.followup.edit_message(
                message_id=inter.message.id,
                attachments=[file], view=new_view,
            )
        except Exception:
            logger.exception("leaderboard_refresh exception")

    @discord.ui.button(
        emoji="◀️", style=discord.ButtonStyle.secondary, custom_id="lb:prev",
    )
    async def prev_btn(self, inter, button):
        cur = self.page
        msg = getattr(inter, "message", None)
        if msg is not None:
            _, m_page, _ = self._recover_state(msg)
            # Si le message porte un label "Page N / M" exploitable, il fait
            # autorite sur self.page (qui est 0 sur l'instance enregistree).
            if m_page > 0 or self.queue_type is None:
                cur = m_page
        await self._go(inter, cur - 1)

    @discord.ui.button(
        label="Page 1 / 1", style=discord.ButtonStyle.grey,
        disabled=True, custom_id="lb:page",
    )
    async def page_btn(self, inter, button):
        if not inter.response.is_done():
            await inter.response.defer()

    @discord.ui.button(
        emoji="▶️", style=discord.ButtonStyle.secondary, custom_id="lb:next",
    )
    async def next_btn(self, inter, button):
        cur = self.page
        msg = getattr(inter, "message", None)
        if msg is not None:
            _, m_page, _ = self._recover_state(msg)
            if m_page > 0 or self.queue_type is None:
                cur = m_page
        await self._go(inter, cur + 1)


async def build_leaderboard_payload(
    guild: discord.Guild, db, queue_type: str, *,
    with_view: bool = True,
    view_timeout: float | None = None,   # conserve pour back-compat, ignore (vue toujours persistante)
    page: int = 0,
) -> tuple[discord.File | None, discord.ui.View | None]:
    """Genere file/view pour le leaderboard du queue_type donne, page `page`.

    Utilise un cache lazy (cf. `_PAGE_CACHE`) pour eviter de re-rendre
    une page deja generee. Le cache est invalide depuis
    `refresh_leaderboard_channel` quand un changement d'ELO survient.
    """
    del view_timeout  # parametre conserve pour API stable, vue toujours timeout=None
    repository._check_queue_type(queue_type)

    # Cache lookup AVANT Mongo : si la page demandee est en cache, on
    # economise la query DB + le PIL render. La page renvoyee est l'image
    # exacte rendue lors du dernier render (coherente avec le message
    # actuellement poste dans #leaderboard).
    cached = _cache_get(guild.id, queue_type, page)
    if cached is not None:
        png_bytes, total_pages_cached = cached
        file = discord.File(
            BytesIO(png_bytes), filename=f"leaderboard_{queue_type}.png",
        )
        if not with_view:
            return file, None
        return file, LeaderboardView(
            page=page, total_pages=total_pages_cached, queue_type=queue_type,
        )

    col  = repository.get_elo_col(db, guild.id)
    docs = list(col.find({"queue_type": queue_type})
                  .sort([("elo", -1), ("wins", -1), ("_id", 1)]))
    if not docs:
        return None, None

    all_players = []
    rank = 1
    for doc in docs:
        uid = doc.get("user_id") or doc["_id"].split(":")[0]
        try:
            member = guild.get_member(int(uid))
        except (TypeError, ValueError):
            member = None
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

    if not all_players:
        return None, None

    total_pages = max(1, (len(all_players) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    loop = asyncio.get_running_loop()
    start = page * PAGE_SIZE
    chunk = all_players[start:start + PAGE_SIZE]
    # Le titre du leaderboard inclut le queue_type pour distinguer les
    # 3 leaderboards qui cohabitent dans #leaderboard.
    title = f"Leaderboard {queue_type.upper()} Queue"
    buf = await loop.run_in_executor(
        None,
        lambda: generate_leaderboard(chunk, server_name=f"{guild.name} - {title}"),
    )

    # Stocker les bytes raw dans le cache (pas le discord.File qui est
    # single-use). A chaque cache hit, on wrappera dans un BytesIO frais.
    png_bytes = buf.getvalue()
    _cache_set(guild.id, queue_type, page, png_bytes, total_pages)

    file = discord.File(
        BytesIO(png_bytes), filename=f"leaderboard_{queue_type}.png",
    )

    if not with_view:
        return file, None
    return file, LeaderboardView(
        page=page, total_pages=total_pages, queue_type=queue_type,
    )


async def refresh_leaderboard_channel(
    guild: discord.Guild, db, queue_type: str,
) -> None:
    """Refresh le leaderboard du queue_type donne dans #leaderboard.

    Per-queue debounce : une rafale Pro ne bloque pas un refresh Open."""
    repository._check_queue_type(queue_type)
    now = datetime.now(UTC)
    key = (guild.id, queue_type)
    last = _LAST_REFRESH_AT.get(key)
    if last is not None and (now - last).total_seconds() < _REFRESH_DEBOUNCE_SECONDS:
        _LAST_REFRESH_AT.move_to_end(key)
        return
    _LAST_REFRESH_AT[key] = now
    _LAST_REFRESH_AT.move_to_end(key)
    while len(_LAST_REFRESH_AT) > _MAX_GUILDS_TRACKED:
        _LAST_REFRESH_AT.popitem(last=False)

    # ELO a change pour ce (guild, queue_type) ET on va effectivement
    # rendre une nouvelle page (debounce passe) -> invalide les pages
    # caches pour eviter de servir des donnees perimees. La page 1
    # fraichement rendue ci-dessous repeuplera le cache via _cache_set.
    # Note : si le debounce avait renvoye plus haut, on n'invalide PAS
    # — le message poste reste l'ancien, donc le cache reste coherent.
    _cache_invalidate(guild.id, queue_type)

    needle = LEADERBOARD_CHANNEL_NAME.lower()
    chan = next(
        (c for c in guild.text_channels if needle in (c.name or "").lower()),
        None,
    )
    if chan is None:
        return

    stored_id = repository.get_leaderboard_message_id(db, guild.id, queue_type)
    if stored_id is not None:
        try:
            old_msg = await chan.fetch_message(stored_id)
            await old_msg.delete()
        except discord.NotFound:
            repository.clear_leaderboard_message_id(db, guild.id, queue_type)
        except Exception:
            logger.exception("leaderboard_refresh exception")

    # NO fallback history scan : avec 3 LBs qui cohabitent dans #leaderboard,
    # on ne peut pas identifier "lequel des 3" sans le state persiste. Si
    # aucun stored_id n'existe, on poste juste le nouveau message.

    try:
        file, view = await build_leaderboard_payload(
            guild, db, queue_type, view_timeout=None,
        )
    except Exception:
        logger.exception("leaderboard_refresh exception")
        return
    if file is None:
        return

    try:
        if view is None:
            new_msg = await chan.send(file=file)
        else:
            new_msg = await chan.send(file=file, view=view)
    except Exception:
        logger.exception("leaderboard_refresh exception")
        return

    try:
        repository.set_leaderboard_message_id(db, guild.id, queue_type, new_msg.id)
    except Exception:
        logger.exception("leaderboard_refresh exception")
