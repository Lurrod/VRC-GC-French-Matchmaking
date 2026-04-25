"""Acces MongoDB centralise. Toutes les collections passent par ici."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from pymongo import ReturnDocument
from pymongo.database import Database


def get_elo_col(db: Database, guild_id: int | str):
    """Collection ELO d'un guild (1 collection par serveur Discord)."""
    return db[f"elo_{guild_id}"]


def get_bypass_col(db: Database):
    return db["bypass"]


def get_bypass_role(db: Database, guild_id: int | str) -> int | None:
    doc = get_bypass_col(db).find_one({"_id": str(guild_id)})
    return doc["role_id"] if doc else None


def set_bypass_role(db: Database, guild_id: int | str, role_id: int) -> None:
    get_bypass_col(db).update_one(
        {"_id": str(guild_id)},
        {"$set": {"role_id": role_id}},
        upsert=True,
    )


def get_or_create_player(
    col,
    user_id: int | str,
    display_name: str,
    initial_elo: int = 0,
) -> Mapping[str, Any]:
    """
    Recupere le doc joueur, le cree si absent, met a jour le display_name.

    Note : pas atomique — race possible mais low-impact (createur 'gagne').
    Pour Phase 1+, on passera a find_one_and_update avec upsert.
    """
    uid = str(user_id)
    doc = col.find_one({"_id": uid})
    if not doc:
        col.insert_one({
            "_id":    uid,
            "name":   display_name,
            "elo":    initial_elo,
            "wins":   0,
            "losses": 0,
        })
        doc = col.find_one({"_id": uid})
    col.update_one({"_id": uid}, {"$set": {"name": display_name}})
    doc["name"] = display_name
    return doc


# ── V2 : comptes Riot lies ───────────────────────────────────────
def get_riot_col(db: Database, guild_id: int | str):
    """1 collection par guild pour les comptes Riot lies."""
    return db[f"riot_accounts_{guild_id}"]


def link_riot_account(
    db: Database,
    guild_id: int | str,
    user_id: int | str,
    *,
    riot_name: str,
    riot_tag: str,
    riot_region: str,
    puuid: str,
    effective_elo: int,
    peak_elo: int,
    source: str,
) -> None:
    """Enregistre ou met a jour le lien Discord <-> Riot."""
    from datetime import datetime, timezone
    get_riot_col(db, guild_id).update_one(
        {"_id": str(user_id)},
        {"$set": {
            "riot_name":     riot_name,
            "riot_tag":      riot_tag,
            "riot_region":   riot_region,
            "puuid":         puuid,
            "effective_elo": effective_elo,
            "peak_elo":      peak_elo,
            "source":        source,
            "fetched_at":    datetime.now(timezone.utc),
        }},
        upsert=True,
    )


def get_riot_account(db: Database, guild_id: int | str, user_id: int | str) -> Mapping[str, Any] | None:
    return get_riot_col(db, guild_id).find_one({"_id": str(user_id)})


def unlink_riot_account(db: Database, guild_id: int | str, user_id: int | str) -> bool:
    """Renvoie True si une entree a ete supprimee."""
    res = get_riot_col(db, guild_id).delete_one({"_id": str(user_id)})
    return res.deleted_count > 0


# ── V2 : queue 10mans ─────────────────────────────────────────────
QUEUE_SIZE_DEFAULT = 10


def get_queue_col(db: Database, guild_id: int | str):
    return db[f"queue_{guild_id}"]


def get_active_queue(db: Database, guild_id: int | str) -> Mapping[str, Any] | None:
    return get_queue_col(db, guild_id).find_one({"_id": "active"})


def setup_active_queue(
    db: Database,
    guild_id: int | str,
    channel_id: int,
    message_id: int,
) -> None:
    """Cree (ou remplace) la queue active pour ce guild."""
    from datetime import datetime, timezone
    get_queue_col(db, guild_id).update_one(
        {"_id": "active"},
        {"$set": {
            "channel_id": int(channel_id),
            "message_id": int(message_id),
            "players":    [],
            "status":     "open",
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


def delete_active_queue(db: Database, guild_id: int | str) -> bool:
    res = get_queue_col(db, guild_id).delete_one({"_id": "active"})
    return res.deleted_count > 0


def close_active_queue(db: Database, guild_id: int | str) -> None:
    """Marque la queue comme 'forming' (match en cours de formation)."""
    get_queue_col(db, guild_id).update_one(
        {"_id": "active"},
        {"$set": {"status": "forming"}},
    )


@dataclass(frozen=True)
class QueueResult:
    success: bool
    reason:  str
    queue:   Mapping[str, Any] | None


def add_player_to_queue(
    db: Database,
    guild_id: int | str,
    user_id:  int | str,
    *,
    max_size: int = QUEUE_SIZE_DEFAULT,
) -> QueueResult:
    col = get_queue_col(db, guild_id)
    queue = col.find_one({"_id": "active"})
    if not queue:
        return QueueResult(False, "no_queue", None)
    if queue.get("status") != "open":
        return QueueResult(False, "queue_closed", queue)
    players = queue.get("players", [])
    uid_str = str(user_id)
    if uid_str in players:
        return QueueResult(False, "already_in", queue)
    if len(players) >= max_size:
        return QueueResult(False, "queue_full", queue)

    updated = col.find_one_and_update(
        {"_id": "active", "status": "open", "players": {"$nin": [uid_str]}},
        {"$push": {"players": uid_str}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        return QueueResult(False, "race", queue)
    return QueueResult(True, "added", updated)


def remove_player_from_queue(
    db: Database,
    guild_id: int | str,
    user_id:  int | str,
) -> QueueResult:
    col = get_queue_col(db, guild_id)
    queue = col.find_one({"_id": "active"})
    if not queue:
        return QueueResult(False, "no_queue", None)
    uid_str = str(user_id)
    if uid_str not in queue.get("players", []):
        return QueueResult(False, "not_in", queue)

    updated = col.find_one_and_update(
        {"_id": "active"},
        {"$pull": {"players": uid_str}},
        return_document=ReturnDocument.AFTER,
    )
    return QueueResult(True, "removed", updated)


# ── V2 : matches ──────────────────────────────────────────────────
def get_matches_col(db: Database, guild_id: int | str):
    return db[f"matches_{guild_id}"]


def create_match(
    db: Database,
    guild_id: int | str,
    *,
    team_a:        list[dict],
    team_b:        list[dict],
    map_name:      str,
    lobby_leader_id: int | str,
    category_name: str | None,
    message_id:    int | None,
    channel_id:    int | None,
) -> Any:
    """Insere un nouveau match. Renvoie son _id (ObjectId)."""
    from datetime import datetime, timezone
    doc = {
        "team_a":          team_a,
        "team_b":          team_b,
        "map":             map_name,
        "lobby_leader_id": str(lobby_leader_id),
        "category_name":   category_name,
        "status":          "pending",
        "votes":           {},
        "created_at":      datetime.now(timezone.utc),
        "validated_at":    None,
        "message_id":      int(message_id) if message_id else None,
        "channel_id":      int(channel_id) if channel_id else None,
    }
    res = get_matches_col(db, guild_id).insert_one(doc)
    return res.inserted_id


def get_match(db: Database, guild_id: int | str, match_id: Any) -> Mapping[str, Any] | None:
    return get_matches_col(db, guild_id).find_one({"_id": match_id})


def get_match_by_message(db: Database, guild_id: int | str, message_id: int) -> Mapping[str, Any] | None:
    return get_matches_col(db, guild_id).find_one({"message_id": int(message_id)})


def add_match_vote(
    db: Database,
    guild_id: int | str,
    match_id: Any,
    user_id: int | str,
    choice: str,
) -> Mapping[str, Any] | None:
    """Enregistre/ecrase le vote d'un user. Renvoie le doc apres maj."""
    if choice not in ("a", "b"):
        raise ValueError(f"choice doit etre 'a' ou 'b', recu {choice!r}")
    return get_matches_col(db, guild_id).find_one_and_update(
        {"_id": match_id},
        {"$set": {f"votes.{user_id}": choice}},
        return_document=ReturnDocument.AFTER,
    )


def set_match_status(
    db: Database,
    guild_id: int | str,
    match_id: Any,
    status: str,
) -> None:
    from datetime import datetime, timezone
    update = {"status": status}
    if status in ("validated_a", "validated_b"):
        update["validated_at"] = datetime.now(timezone.utc)
    get_matches_col(db, guild_id).update_one({"_id": match_id}, {"$set": update})
