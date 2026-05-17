import logging
import os
import sys
from datetime import UTC

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient
from pymongo.collection import Collection

from services import elo_calc, repository
from services.leaderboard_refresh import (
    LeaderboardView,
)
from services.riot_api import HenrikDevClient

# Configuration logging globale : sans ce basicConfig, le root logger
# reste a WARNING par defaut et le format minimaliste de Python est
# utilise (pas de timestamp, pas de niveau, pas de nom de module).
# En prod sur PM2, les logs `logger.info(...)` etaient silencieusement
# perdus. Niveau pilote par la variable d'env LOG_LEVEL (defaut INFO).
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

# ── Charge .env si present (sans planter si python-dotenv absent) ──
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Configuration ──────────────────────────────────────────────
TOKEN     = os.environ.get("DISCORD_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")

# Fail-fast au demarrage si MONGO_URL est absent ou vide. Sans cette
# garde, MongoClient(None, ...) tombe silencieusement sur le defaut
# pymongo `mongodb://localhost:27017/` -- en prod sur Kimsufi ca peut
# faire pointer le bot vers une instance Mongo absente, avec une erreur
# `serverSelectionTimeoutMS` 5 s plus tard et aucun message clair sur
# la cause (env var manquante apres un `pm2 restart` sans --update-env).
if not MONGO_URL:
    raise RuntimeError("MONGO_URL environment variable not set")

ELO_START = elo_calc.ELO_START
MAPS      = list(elo_calc.MAPS)

# Pondération ELO par position de joueur (slot 1..5) pour /win et /lose.
# Le premier slot encaisse le plus gros gain / la plus petite perte.
WIN_DELTAS_BY_SLOT:  tuple[int, ...] = (20, 18, 17, 16, 15)
LOSE_DELTAS_BY_SLOT: tuple[int, ...] = (10, 10, 12, 13, 15)

# ── MongoDB ────────────────────────────────────────────────────
# retryWrites/retryReads sont True par defaut depuis pymongo 4.x mais on les
# explicite pour resilience aux blips reseau. serverSelectionTimeoutMS=5000
# evite de bloquer >30s sur Mongo down -> Discord renvoie "L'application n'a
# pas repondu". connectTimeoutMS=5000 limite le handshake initial.
client: MongoClient = MongoClient(
    MONGO_URL,
    tz_aware=True,
    tzinfo=UTC,
    retryWrites=True,
    retryReads=True,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db     = client["elobot"]

def get_elo_col() -> Collection:
    return repository.get_elo_col(db)

def get_bypass_col() -> Collection:
    return repository.get_bypass_col(db)

def get_player(col, member: discord.Member, queue_type: str):
    return repository.get_or_create_player(
        col, member.id, queue_type, member.display_name, initial_elo=ELO_START,
    )


# Choix slash commun a toutes les commandes ELO/leaderboard.
_QUEUE_CHOICES = [
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC", value="gc"),
]

def get_bypass_role(guild_id):
    return repository.get_bypass_role(db, guild_id)

def set_bypass_role(guild_id, role_id):
    repository.set_bypass_role(db, guild_id, role_id)

def has_access(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = get_bypass_role(interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))

# ── Bot ────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ── Chargement des cogs V2 ────────────────────────────────────────
riot_client = HenrikDevClient()


async def _load_v2_cogs() -> None:
    from cogs.admin import setup as setup_admin
    from cogs.applications import setup as setup_applications
    from cogs.elo_admin import setup as setup_elo_admin
    from cogs.match    import setup as setup_match
    from cogs.prefix_legacy import setup as setup_prefix_legacy
    from cogs.queue_v2 import setup as setup_queue_v2
    from cogs.riot_link import setup as setup_riot_link

    await setup_riot_link(bot, db, riot_client)
    match_cog = await setup_match(bot, db, henrik_client=riot_client)
    await setup_queue_v2(bot, db, on_full=match_cog.on_queue_full)
    await setup_applications(bot, db)
    await setup_admin(bot, db)
    await setup_elo_admin(bot, db)
    await setup_prefix_legacy(bot, db)


@bot.event
async def setup_hook():
    # Charger les cogs essentiels (queue_v2, match, riot_link). Sans eux,
    # le bot demarre en mode degrade (slash commands manquantes, queue
    # inaccessible) sans signaler clairement l'erreur. On log + on
    # raise pour fail fast plutot que de laisser tourner un bot inutile.
    try:
        await _load_v2_cogs()
    except Exception:
        # Re-raise : Discord.py va arreter le startup. Mieux qu'un bot
        # silencieusement cassé en prod.
        logger.critical("[setup_hook] CRITIQUE : echec chargement des cogs", exc_info=True)
        raise


_synced_once = False


@bot.event
async def on_ready():
    global _synced_once

    if _synced_once:
        # on_ready peut etre fire plusieurs fois (reconnects WS) : on sync
        # uniquement au premier ready pour eviter de spammer Discord et de
        # subir le temps de propagation global (~1h) inutilement. Idem
        # pour `add_view` qui referencerait des instances View neuves a
        # chaque reconnect (leak memoire mineur sur reconnects frequents).
        logger.info("Bot reconnecte : %s (sync slash skipped)", bot.user)
        return

    # Premier on_ready uniquement : enregistrement de la view leaderboard.
    # Les autres vues persistantes (Welcome, ApplicationReview, CloseTicket,
    # Report, Queue) sont enregistrees par leurs cogs respectifs lors du
    # setup_hook (cf. cogs/applications.py et cogs/queue_v2.py).
    # LeaderboardView : pagination des messages leaderboard persistants
    # postes dans #leaderboard. Sans cet enregistrement, les boutons
    # prev/next ne fonctionnent plus apres un restart du bot.
    bot.add_view(LeaderboardView())

    # Sync rapide sur une guild specifique si DEV_GUILD_ID est defini.
    # Sinon, sync global (peut prendre jusqu'a 1h pour propager).
    dev_guild_id = os.getenv("DEV_GUILD_ID")
    if dev_guild_id:
        guild = discord.Object(id=int(dev_guild_id))
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        logger.info("Bot connecte : %s (ID: %s)", bot.user, bot.user.id)
        logger.info("%d commandes slash synchronisees sur guild %s.", len(synced), dev_guild_id)
    else:
        synced = await tree.sync()
        logger.info("Bot connecte : %s (ID: %s)", bot.user, bot.user.id)
        logger.info("%d commandes slash synchronisees (global, propagation jusqu'a 1h).", len(synced))
    _synced_once = True

if __name__ == "__main__":
    # Configuration logging : niveau INFO + format avec timestamp et logger
    # name. Permet de filtrer en prod (ex: -e LOG_LEVEL=DEBUG via supervisor).
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    # Split stdout / stderr : DEBUG+INFO -> stdout, WARNING+ -> stderr.
    # PM2 capture stdout -> out.log et stderr -> error.log, donc tant que
    # rien n'est anormal seul out.log se remplit.
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    )
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(lambda r: r.levelno < logging.WARNING)
    stdout_handler.setFormatter(fmt)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))
    root.handlers.clear()
    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable not set")
    bot.run(TOKEN)
