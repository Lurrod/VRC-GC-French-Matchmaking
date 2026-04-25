"""
Configuration pytest globale.

IMPORTANT : ce fichier est charge AVANT les tests, donc avant l'import de bot.py.
On en profite pour :
  1. Patcher pymongo.MongoClient avec mongomock (in-memory, pas besoin de Mongo)
  2. Definir des variables d'environnement bidons pour que bot.py s'importe
  3. Exposer des fixtures dpytest pour les tests d'integration Discord
"""

import os
import sys
from unittest.mock import patch

import mongomock
import pymongo
import pytest
import pytest_asyncio


# ── 1. Variables d'environnement bidons (avant import de bot.py) ──
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-real")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")


# ── 2. Patch MongoClient AVANT que bot.py soit importe ────────────
# bot.py fait `client = MongoClient(MONGO_URL)` au top-level, donc on
# doit remplacer MongoClient au niveau du module pymongo lui-meme.
_mongo_patcher = patch.object(pymongo, "MongoClient", mongomock.MongoClient)
_mongo_patcher.start()


# ── 3. Reset de la base in-memory entre chaque test ───────────────
@pytest.fixture(autouse=True)
def clean_mongo():
    """Vide toutes les collections mongomock avant chaque test."""
    import bot
    for name in bot.db.list_collection_names():
        bot.db.drop_collection(name)
    yield


# ── 4. Fixture dpytest : bot Discord simule ───────────────────────
@pytest_asyncio.fixture
async def discord_bot():
    """
    Bot Discord simule via dpytest.

    Configure automatiquement :
      - 1 guild "TestGuild"
      - 1 channel texte "general"
      - 3 membres (TestUser0, TestUser1, TestUser2)
    """
    import discord
    import discord.ext.test as dpytest
    import bot as bot_module

    # discord.py 2.x : le loop n'est pas defini avant setup_hook.
    # On le force ici pour que dpytest puisse dispatcher des events.
    await bot_module.bot._async_setup_hook()

    dpytest.configure(
        bot_module.bot,
        guilds=1,
        text_channels=1,
        voice_channels=0,
        members=3,
    )
    yield bot_module.bot
    await dpytest.empty_queue()


# ── 5. Helpers reutilisables ──────────────────────────────────────
@pytest.fixture
def fake_member(discord_bot):
    """Retourne le 1er membre du guild de test."""
    import discord.ext.test as dpytest
    config = dpytest.get_config()
    return config.members[0]


@pytest.fixture
def fake_guild(discord_bot):
    import discord.ext.test as dpytest
    config = dpytest.get_config()
    return config.guilds[0]
