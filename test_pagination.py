"""
Tests unitaires de la logique de pagination (sans Discord, sans MongoDB).

Ces tests valident UNIQUEMENT le calcul du nombre de pages et les bornes
de navigation. Ils ne peuvent pas detecter un bug dans l'API Discord.

Usage:
    pip install pytest
    pytest test_pagination.py -v
"""

import pytest


PAGE_SIZE = 15


def total_pages(n_players: int) -> int:
    """Reproduit le calcul du bot (ligne 215 du fichier original)."""
    return max(1, (n_players + PAGE_SIZE - 1) // PAGE_SIZE)


def is_prev_disabled(page: int) -> bool:
    return page == 0


def is_next_disabled(page: int, total: int) -> bool:
    return page >= total - 1


def clamp_page(new_page: int, total: int) -> int | None:
    """Renvoie None si le clic doit etre ignore (hors bornes)."""
    if new_page < 0 or new_page >= total:
        return None
    return new_page


# ── Tests : nombre de pages ───────────────────────────────────────
@pytest.mark.parametrize("n,expected", [
    (0,  1),    # liste vide -> 1 page (default)
    (1,  1),    # 1 joueur -> 1 page
    (15, 1),    # exactement PAGE_SIZE -> 1 page
    (16, 2),    # 1 de plus -> 2 pages
    (29, 2),
    (30, 2),
    (31, 3),
    (100, 7),
    (150, 10),
])
def test_total_pages(n, expected):
    assert total_pages(n) == expected


# ── Tests : etat des boutons ──────────────────────────────────────
def test_prev_disabled_on_first_page():
    assert is_prev_disabled(0) is True


def test_prev_enabled_on_other_pages():
    assert is_prev_disabled(1) is False
    assert is_prev_disabled(5) is False


def test_next_disabled_on_last_page():
    assert is_next_disabled(1, total=2) is True
    assert is_next_disabled(6, total=7) is True


def test_next_enabled_when_more_pages():
    assert is_next_disabled(0, total=2) is False
    assert is_next_disabled(3, total=7) is False


def test_both_disabled_when_only_one_page():
    assert is_prev_disabled(0) is True
    assert is_next_disabled(0, total=1) is True


# ── Tests : navigation (clamp) ────────────────────────────────────
def test_clamp_valid_page():
    assert clamp_page(2, total=5) == 2


def test_clamp_first_page():
    assert clamp_page(0, total=5) == 0


def test_clamp_last_page():
    assert clamp_page(4, total=5) == 4


def test_clamp_below_zero_rejected():
    assert clamp_page(-1, total=5) is None


def test_clamp_above_total_rejected():
    assert clamp_page(5, total=5) is None
    assert clamp_page(99, total=5) is None


# ── Tests : decoupage en chunks ───────────────────────────────────
def test_chunk_first_page():
    players = list(range(30))
    page = 0
    chunk = players[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    assert chunk == list(range(15))


def test_chunk_second_page():
    players = list(range(30))
    page = 1
    chunk = players[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    assert chunk == list(range(15, 30))


def test_chunk_partial_last_page():
    """Cas ou la derniere page n'est pas pleine - ATTENTION, peut casser
    generate_leaderboard si elle n'est pas tolerante aux chunks de taille variable."""
    players = list(range(16))
    chunk_p2 = players[15:30]
    assert len(chunk_p2) == 1, "La page 2 ne contient qu'1 joueur ici"


def test_chunk_empty_when_out_of_bounds():
    players = list(range(15))
    chunk = players[15:30]
    assert chunk == []


import mongomock
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_build_leaderboard_payload_filters_by_queue_type():
    from services.leaderboard_refresh import build_leaderboard_payload
    from services.repository import get_elo_col, player_doc_id

    db = mongomock.MongoClient(tz_aware=True).db
    col = get_elo_col(db, 42)
    col.insert_many([
        {"_id": player_doc_id(1, "pro"), "user_id": "1", "name": "A",
         "elo": 2500, "wins": 5, "losses": 1, "queue_type": "pro"},
        {"_id": player_doc_id(1, "open"), "user_id": "1", "name": "A",
         "elo": 1500, "wins": 1, "losses": 5, "queue_type": "open"},
    ])

    guild = MagicMock()
    guild.id = 42
    guild.name = "TestGuild"
    fake_member = MagicMock()
    fake_member.display_name = "A"
    fake_member.display_avatar.replace.return_value.url = "http://av/1.png"
    guild.get_member.return_value = fake_member

    file_pro, _ = await build_leaderboard_payload(guild, db, queue_type="pro")
    file_open, _ = await build_leaderboard_payload(guild, db, queue_type="open")
    file_gc, _ = await build_leaderboard_payload(guild, db, queue_type="gc")

    assert file_pro is not None
    assert file_open is not None
    assert file_gc is None  # 0 players in GC


# ── Cache de pages rendues ────────────────────────────────────────
#
# Tests du cache lazy ajoute dans services/leaderboard_refresh.py.
# Chaque test commence par vider le cache global (process-wide) pour
# garantir l'isolation entre cas.


def _make_guild_with_member(guild_id: int = 99):
    guild = MagicMock()
    guild.id = guild_id
    guild.name = "TestGuild"
    fake_member = MagicMock()
    fake_member.display_name = "P"
    fake_member.display_avatar.replace.return_value.url = "http://av/p.png"
    guild.get_member.return_value = fake_member
    return guild


def _seed_pro_open(db, guild_id: int, n: int = 1) -> None:
    from services.repository import get_elo_col, player_doc_id
    col = get_elo_col(db, guild_id)
    docs = []
    for i in range(1, n + 1):
        docs.append({
            "_id": player_doc_id(i, "pro"), "user_id": str(i),
            "name": f"P{i}", "elo": 2500 + i, "wins": 1, "losses": 0,
            "queue_type": "pro",
        })
        docs.append({
            "_id": player_doc_id(i, "open"), "user_id": str(i),
            "name": f"P{i}", "elo": 2000 + i, "wins": 1, "losses": 0,
            "queue_type": "open",
        })
    col.insert_many(docs)


@pytest.mark.asyncio
async def test_page_cache_hit_skips_render():
    """2e appel sur la meme page ne doit pas re-rendre via PIL."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import build_leaderboard_payload

    leaderboard_refresh._clear_page_cache_for_tests()

    db = mongomock.MongoClient(tz_aware=True).db
    _seed_pro_open(db, 99)
    guild = _make_guild_with_member(99)

    call_count = {"n": 0}
    real_gen = leaderboard_refresh.generate_leaderboard

    def counting_gen(*a, **kw):
        call_count["n"] += 1
        return real_gen(*a, **kw)

    leaderboard_refresh.generate_leaderboard = counting_gen
    try:
        file1, _ = await build_leaderboard_payload(guild, db, queue_type="pro")
        file2, _ = await build_leaderboard_payload(guild, db, queue_type="pro")
    finally:
        leaderboard_refresh.generate_leaderboard = real_gen

    assert file1 is not None and file2 is not None
    assert call_count["n"] == 1, (
        f"generate_leaderboard appele {call_count['n']}x au lieu de 1 "
        "(cache hit attendu sur le 2eme appel)"
    )


@pytest.mark.asyncio
async def test_page_cache_returns_fresh_bytesio_per_call():
    """Cache stocke des bytes, pas un BytesIO. Chaque hit doit produire
    un discord.File lisible (BytesIO frais cote interne)."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import build_leaderboard_payload

    leaderboard_refresh._clear_page_cache_for_tests()

    db = mongomock.MongoClient(tz_aware=True).db
    _seed_pro_open(db, 99)
    guild = _make_guild_with_member(99)

    file1, _ = await build_leaderboard_payload(guild, db, queue_type="pro")
    file2, _ = await build_leaderboard_payload(guild, db, queue_type="pro")

    # Les deux discord.File doivent etre des objets distincts (BytesIO neufs)
    # et contenir des bytes lisibles -> sinon le 2eme send Discord echoue.
    bytes1 = file1.fp.read()
    bytes2 = file2.fp.read()
    assert bytes1 == bytes2
    assert len(bytes1) > 0


@pytest.mark.asyncio
async def test_page_cache_invalidation_clears_queue_entries():
    """_cache_invalidate(g, qt) doit vider TOUTES les entrees de ce
    (guild, queue_type), peu importe la page."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import (
        build_leaderboard_payload, _cache_invalidate, _PAGE_CACHE,
    )

    leaderboard_refresh._clear_page_cache_for_tests()

    db = mongomock.MongoClient(tz_aware=True).db
    _seed_pro_open(db, 99, n=20)  # > 15 -> 2 pages
    guild = _make_guild_with_member(99)

    await build_leaderboard_payload(guild, db, queue_type="pro", page=0)
    await build_leaderboard_payload(guild, db, queue_type="pro", page=1)
    assert (99, "pro", 0) in _PAGE_CACHE
    assert (99, "pro", 1) in _PAGE_CACHE

    removed = _cache_invalidate(99, "pro")
    assert removed == 2
    assert (99, "pro", 0) not in _PAGE_CACHE
    assert (99, "pro", 1) not in _PAGE_CACHE


@pytest.mark.asyncio
async def test_page_cache_invalidation_is_per_queue():
    """Invalider Pro ne doit PAS toucher Open ou GC."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import (
        build_leaderboard_payload, _cache_invalidate, _PAGE_CACHE,
    )

    leaderboard_refresh._clear_page_cache_for_tests()

    db = mongomock.MongoClient(tz_aware=True).db
    _seed_pro_open(db, 99)
    guild = _make_guild_with_member(99)

    await build_leaderboard_payload(guild, db, queue_type="pro")
    await build_leaderboard_payload(guild, db, queue_type="open")
    assert (99, "pro", 0) in _PAGE_CACHE
    assert (99, "open", 0) in _PAGE_CACHE

    removed = _cache_invalidate(99, "pro")
    assert removed == 1
    assert (99, "pro", 0) not in _PAGE_CACHE
    assert (99, "open", 0) in _PAGE_CACHE, "Open ne doit PAS etre invalide"


@pytest.mark.asyncio
async def test_page_cache_invalidation_is_per_guild():
    """Invalider guild 99 ne doit PAS toucher guild 100."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import (
        build_leaderboard_payload, _cache_invalidate, _PAGE_CACHE,
    )

    leaderboard_refresh._clear_page_cache_for_tests()

    db = mongomock.MongoClient(tz_aware=True).db
    _seed_pro_open(db, 99)
    _seed_pro_open(db, 100)

    await build_leaderboard_payload(_make_guild_with_member(99), db, queue_type="pro")
    await build_leaderboard_payload(_make_guild_with_member(100), db, queue_type="pro")
    assert (99,  "pro", 0) in _PAGE_CACHE
    assert (100, "pro", 0) in _PAGE_CACHE

    _cache_invalidate(99, "pro")
    assert (99,  "pro", 0) not in _PAGE_CACHE
    assert (100, "pro", 0) in _PAGE_CACHE, (
        "Le cache d'une autre guild ne doit pas etre touche"
    )


def test_page_cache_lru_eviction():
    """Au-dela de _PAGE_CACHE_MAXSIZE, le plus ancien (FIFO) est evicte."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import (
        _cache_set, _PAGE_CACHE, _PAGE_CACHE_MAXSIZE,
    )

    leaderboard_refresh._clear_page_cache_for_tests()

    # Remplit jusqu'a la limite : (guild_id varie pour generer des cles uniques)
    for g in range(_PAGE_CACHE_MAXSIZE):
        _cache_set(g, "pro", 0, b"x", 1)
    assert len(_PAGE_CACHE) == _PAGE_CACHE_MAXSIZE
    assert (0, "pro", 0) in _PAGE_CACHE

    # Ajout d'une entree de plus -> le plus ancien (guild=0) est evince
    _cache_set(_PAGE_CACHE_MAXSIZE, "pro", 0, b"x", 1)
    assert len(_PAGE_CACHE) == _PAGE_CACHE_MAXSIZE
    assert (0, "pro", 0) not in _PAGE_CACHE
    assert (_PAGE_CACHE_MAXSIZE, "pro", 0) in _PAGE_CACHE


def test_page_cache_get_promotes_lru_order():
    """Un cache hit doit promouvoir la cle au plus recent (anti-eviction)."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import (
        _cache_set, _cache_get, _PAGE_CACHE, _PAGE_CACHE_MAXSIZE,
    )

    leaderboard_refresh._clear_page_cache_for_tests()

    # Remplit la moitie de la limite avec des cles distinctes
    for g in range(3):
        _cache_set(g, "pro", 0, b"x", 1)

    # Hit sur la plus ancienne (g=0) -> elle devient la plus recente
    assert _cache_get(0, "pro", 0) is not None

    # Verifie qu'elle est bien en fin (most-recent) de l'OrderedDict
    last_key = next(reversed(_PAGE_CACHE))
    assert last_key == (0, "pro", 0)
