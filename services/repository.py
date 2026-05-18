"""Acces MongoDB centralise. Toutes les collections passent par ici."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from collections.abc import Mapping
from pymongo import ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from datetime import UTC

logger = logging.getLogger(__name__)


# Tuple ordonne des queue types supportes. L'ordre influence l'affichage
# (boucles de pre-post leaderboards, /setup) : Pro en premier, GC en dernier.
QUEUE_TYPES: tuple[str, ...] = ("pro", "open", "gc")


def is_valid_queue_type(queue_type: str) -> bool:
    return queue_type in QUEUE_TYPES


def _check_queue_type(queue_type: str) -> None:
    if not is_valid_queue_type(queue_type):
        raise ValueError(
            f"queue_type invalide : {queue_type!r}. Attendus : {QUEUE_TYPES}"
        )


def player_doc_id(user_id: int | str, queue_type: str) -> str:
    """Compound _id pour un doc joueur dans la collection partagée `elo`."""
    _check_queue_type(queue_type)
    return f"{user_id}:{queue_type}"


def active_queue_id(queue_type: str) -> str:
    """_id pour la queue active d'un type donne dans queue_<guild>."""
    _check_queue_type(queue_type)
    return f"active:{queue_type}"


def leaderboard_state_id(queue_type: str) -> str:
    """_id pour le state du leaderboard d'un type dans leaderboard_state_<guild>."""
    _check_queue_type(queue_type)
    return f"current:{queue_type}"


# Cache des collections deja indexees pour eviter de re-issuer create_index a
# chaque call (idempotent cote Mongo, mais inutile en perf).
_indexed_collections: set[str] = set()


def _ensure_indexes(col, kind: str) -> None:
    """Cree les indexes manquants sur une collection. Idempotent et safe en
    cas d'echec (ex: mongomock partial support, perms manquantes)."""
    name = col.full_name if hasattr(col, "full_name") else f"{kind}:{id(col)}"
    if name in _indexed_collections:
        return
    try:
        if kind == "elo":
            # Tri leaderboard par ELO desc.
            col.create_index([("elo", -1)])
        elif kind == "matches":
            # Lookup vote message + scan timeout + scan verification ELO.
            col.create_index([("message_id", 1)])
            col.create_index([("status", 1), ("created_at", 1)])
            col.create_index([("status", 1), ("validated_at", 1), ("elo_applied", 1)])
        elif kind == "riot":
            # Dedup PUUID : empeche un meme compte Riot d'etre lie a 2
            # comptes Discord (multi-account farming du seed ELO).
            col.create_index([("puuid", 1)], unique=True, sparse=True)
    except Exception as e:
        logger.error(f"[repository] _ensure_indexes({kind}) a leve : {e}", exc_info=True)
    _indexed_collections.add(name)


def get_elo_col(db: Database) -> Collection:
    """Collection ELO partagée entre toutes les guilds.

    Le doc `_id` reste compound `<user_id>:<queue_type>`. Tous les bots utilisant
    la même MongoDB lisent/écrivent ici, peu importe la guild Discord d'origine.
    """
    col = db["elo"]
    _ensure_indexes(col, "elo")
    return col


def get_bypass_col(db: Database) -> Collection:
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
    queue_type: str,
    display_name: str,
    initial_elo: int = 2000,
) -> Mapping[str, Any]:
    """Recupere ou cree atomiquement le doc joueur d'une queue.

    Le `_id` est `<user_id>:<queue_type>` (compound). Le champ `queue_type`
    est aussi persiste pour permettre les filtres par type (leaderboard,
    /reset-queue) sans regex sur _id."""
    _check_queue_type(queue_type)
    doc_id = player_doc_id(user_id, queue_type)
    return col.find_one_and_update(
        {"_id": doc_id},
        {
            "$set": {"name": display_name},
            "$setOnInsert": {
                "elo":         initial_elo,
                "wins":        0,
                "losses":      0,
                "queue_type":  queue_type,
                "user_id":     str(user_id),
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


# ── V2 : comptes Riot lies ───────────────────────────────────────
def get_riot_col(db: Database) -> Collection:
    """Collection riot link partagée entre toutes les guilds."""
    col = db["riot"]
    _ensure_indexes(col, "riot")
    return col


def find_riot_account_by_puuid(
    db: Database, puuid: str,
) -> Mapping[str, Any] | None:
    """Renvoie le doc riot_account ayant ce puuid, ou None.

    Utilise pour la dedup PUUID dans /link-riot : empeche un meme compte
    Riot d'etre lie a deux comptes Discord differents (multi-account)."""
    if not puuid:
        return None
    return get_riot_col(db).find_one({"puuid": puuid})


def link_riot_account(
    db: Database,
    user_id: int | str,
    *,
    riot_name: str,
    riot_tag: str,
    riot_region: str,
    puuid: str,
    peak_elo: int,
    source: str,
) -> None:
    """Enregistre ou met a jour le lien Discord <-> Riot (metadata uniquement).

    L'ELO de matchmaking est stockee dans la collection partagee `elo` ; ce doc
    ne sert plus qu'a (a) verifier qu'un joueur est lie pour rejoindre la queue,
    (b) afficher le rang Riot de reference.
    """
    from datetime import datetime
    get_riot_col(db).update_one(
        {"_id": str(user_id)},
        {"$set": {
            "riot_name":     riot_name,
            "riot_tag":      riot_tag,
            "riot_region":   riot_region,
            "puuid":         puuid,
            "peak_elo":      peak_elo,
            "source":        source,
            "fetched_at":    datetime.now(UTC),
        }},
        upsert=True,
    )


def get_riot_account(db: Database, user_id: int | str) -> Mapping[str, Any] | None:
    return get_riot_col(db).find_one({"_id": str(user_id)})


def unlink_riot_account(db: Database, user_id: int | str) -> bool:
    """Renvoie True si une entree a ete supprimee."""
    res = get_riot_col(db).delete_one({"_id": str(user_id)})
    return res.deleted_count > 0


# ── V2 : queue 10mans ─────────────────────────────────────────────
QUEUE_SIZE_DEFAULT = 10


def get_queue_col(db: Database, guild_id: int | str) -> Collection:
    return db[f"queue_{guild_id}"]


def get_active_queue(db: Database, guild_id: int | str, queue_type: str) -> Mapping[str, Any] | None:
    _check_queue_type(queue_type)
    return get_queue_col(db, guild_id).find_one({"_id": active_queue_id(queue_type)})


def setup_active_queue(
    db: Database,
    guild_id: int | str,
    queue_type: str,
    channel_id: int,
    message_id: int,
) -> None:
    """Cree (ou remplace) la queue active de ce type pour ce guild."""
    _check_queue_type(queue_type)
    from datetime import datetime
    get_queue_col(db, guild_id).update_one(
        {"_id": active_queue_id(queue_type)},
        {"$set": {
            "channel_id": int(channel_id),
            "message_id": int(message_id),
            "players":    [],
            "status":     "open",
            "queue_type": queue_type,
            "created_at": datetime.now(UTC),
        }},
        upsert=True,
    )


def delete_active_queue(db: Database, guild_id: int | str, queue_type: str) -> bool:
    _check_queue_type(queue_type)
    res = get_queue_col(db, guild_id).delete_one({"_id": active_queue_id(queue_type)})
    return res.deleted_count > 0


def close_active_queue(
    db: Database, guild_id: int | str, queue_type: str,
) -> Mapping[str, Any] | None:
    """Marque la queue de ce type comme 'forming' et renvoie le doc mis a jour.

    Renvoie None si la queue n'existe pas. Utilise find_one_and_update pour
    fusionner write + read en un seul round-trip atomique.
    """
    _check_queue_type(queue_type)
    return get_queue_col(db, guild_id).find_one_and_update(
        {"_id": active_queue_id(queue_type)},
        {"$set": {"status": "forming"}},
        return_document=ReturnDocument.AFTER,
    )


@dataclass(frozen=True)
class QueueResult:
    success: bool
    reason:  str
    queue:   Mapping[str, Any] | None


def add_player_to_queue(
    db: Database,
    guild_id: int | str,
    queue_type: str,
    user_id:  int | str,
    *,
    max_size: int = QUEUE_SIZE_DEFAULT,
) -> QueueResult:
    _check_queue_type(queue_type)
    col = get_queue_col(db, guild_id)
    qid = active_queue_id(queue_type)
    queue = col.find_one({"_id": qid})
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
        {
            "_id": qid,
            "status": "open",
            "players": {"$nin": [uid_str]},
            "$expr": {"$lt": [
                {"$size": {"$ifNull": ["$players", []]}},
                max_size,
            ]},
        },
        {"$push": {"players": uid_str}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        return QueueResult(False, "race", queue)
    return QueueResult(True, "added", updated)


def remove_player_from_queue(
    db: Database,
    guild_id: int | str,
    queue_type: str,
    user_id:  int | str,
) -> QueueResult:
    _check_queue_type(queue_type)
    col = get_queue_col(db, guild_id)
    qid = active_queue_id(queue_type)
    queue = col.find_one({"_id": qid})
    if not queue:
        return QueueResult(False, "no_queue", None)
    uid_str = str(user_id)
    if uid_str not in queue.get("players", []):
        return QueueResult(False, "not_in", queue)
    updated = col.find_one_and_update(
        {"_id": qid},
        {"$pull": {"players": uid_str}},
        return_document=ReturnDocument.AFTER,
    )
    return QueueResult(True, "removed", updated)


def find_player_in_any_queue(
    db: Database, guild_id: int | str, user_id: int | str,
) -> str | None:
    """Renvoie le queue_type ou le user est present, ou None."""
    uid_str = str(user_id)
    col = get_queue_col(db, guild_id)
    for qt in QUEUE_TYPES:
        doc = col.find_one({"_id": active_queue_id(qt), "players": uid_str})
        if doc is not None:
            return qt
    return None


# ── V2 : matches ──────────────────────────────────────────────────
def get_matches_col(db: Database) -> Collection:
    """Collection matches partagée entre toutes les guilds.

    Chaque match porte un champ `origin_guild_id` pour la traçabilité
    (présent uniquement sur les matches créés après le refactor)."""
    col = db["matches"]
    _ensure_indexes(col, "matches")
    return col


def create_match(
    db: Database,
    *,
    queue_type: str,
    origin_guild_id: int,
    team_a:        list[dict],
    team_b:        list[dict],
    map_name:      str,
    lobby_leader_id: int | str,
    category_name: str | None,
    message_id:    int | None,
    channel_id:    int | None,
) -> Any:
    """Insere un nouveau match. Renvoie son _id (ObjectId).

    `queue_type` (kw-only) : "pro" | "open" | "gc". Persiste sur le doc
    pour permettre les filtres par type (leaderboard refresh, /reset-queue,
    Pro Queue Henrik skip).

    `origin_guild_id` (kw-only) : guild Discord d'origine du match, pour
    la traçabilité cross-guild (la collection `matches` est partagée)."""
    _check_queue_type(queue_type)
    from datetime import datetime
    doc: dict[str, Any] = {
        "team_a":          team_a,
        "team_b":          team_b,
        "map":             map_name,
        "queue_type":      queue_type,
        "origin_guild_id": int(origin_guild_id),
        "lobby_leader_id": str(lobby_leader_id),
        "category_name":   category_name,
        "status":          "pending",
        "votes":           {},
        "created_at":      datetime.now(UTC),
        "validated_at":    None,
        "message_id":      int(message_id) if message_id else None,
        "channel_id":      int(channel_id) if channel_id else None,
    }
    res = get_matches_col(db).insert_one(doc)
    return res.inserted_id


def get_match(db: Database, match_id: Any) -> Mapping[str, Any] | None:
    return get_matches_col(db).find_one({"_id": match_id})


def get_match_by_message(db: Database, message_id: int) -> Mapping[str, Any] | None:
    return get_matches_col(db).find_one({"message_id": int(message_id)})


def add_match_vote(
    db: Database,
    match_id: Any,
    user_id: int | str,
    choice: str,
) -> Mapping[str, Any] | None:
    """Enregistre/ecrase le vote d'un user. Renvoie le doc apres maj.

    CAS sur `status: pending` : empeche les votes tardifs sur un match
    deja annule, contesté ou validé. Un retardataire qui clique alors
    que le match est cancelled n'enregistre rien (None) au lieu de
    polluer `votes` apres-coup."""
    if choice not in ("a", "b"):
        raise ValueError(f"choice doit etre 'a' ou 'b', recu {choice!r}")
    return get_matches_col(db).find_one_and_update(
        {"_id": match_id, "status": "pending"},
        {"$set": {f"votes.{user_id}": choice}},
        return_document=ReturnDocument.AFTER,
    )


def set_match_status(
    db: Database,
    match_id: Any,
    status: str,
) -> None:
    from datetime import datetime
    update: dict[str, Any] = {"status": status}
    if status in ("validated_a", "validated_b"):
        update["validated_at"] = datetime.now(UTC)
    get_matches_col(db).update_one({"_id": match_id}, {"$set": update})


def transition_match_status(
    db: Database,
    match_id: Any,
    *,
    from_status: str,
    to_status: str,
    validated_at=None,
) -> Mapping[str, Any] | None:
    """Atomic CAS : passe le match de `from_status` a `to_status` uniquement si
    le doc est encore dans l'etat attendu. Renvoie le doc apres maj, ou None
    si la transition n'a pas eu lieu (concurrent : un autre vote a deja valide).

    Set `validated_at` si la cible est `validated_a` ou `validated_b`. Le
    parametre `validated_at` permet d'override la valeur (utilise par
    l'auto-reparation de `check_vote_timeouts` pour referencer le moment
    ou la majorite a ete reellement atteinte plutot que `now`)."""
    from datetime import datetime
    update: dict[str, Any] = {"status": to_status}
    if to_status in ("validated_a", "validated_b"):
        update["validated_at"] = validated_at or datetime.now(UTC)
    return get_matches_col(db).find_one_and_update(
        {"_id": match_id, "status": from_status},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )


def claim_match_for_elo(
    db: Database,
    match_id: Any,
) -> Mapping[str, Any] | None:
    """Atomic claim : marque `elo_applied=True` uniquement si non deja applique.

    Empeche la double-application d'ELO si la verification HenrikDev re-tente
    apres un crash entre `apply_match_validation` et `set_match_henrik_verified`.

    Returns:
        Le doc apres claim si on a bien obtenu le verrou, None si deja claime.
    """
    from datetime import datetime
    return get_matches_col(db).find_one_and_update(
        {
            "_id":         match_id,
            "status":      {"$in": ["validated_a", "validated_b"]},
            "elo_applied": {"$ne": True},
        },
        {"$set": {
            "elo_applied":    True,
            "elo_applied_at": datetime.now(UTC),
        }},
        return_document=ReturnDocument.AFTER,
    )


def release_elo_claim(
    db: Database,
    match_id: Any,
) -> None:
    """Annule le claim si l'application ELO a echoue (rollback)."""
    get_matches_col(db).update_one(
        {"_id": match_id},
        {"$unset": {"elo_applied": "", "elo_applied_at": ""}},
    )


def find_validated_unverified(
    db: Database, cutoff_dt, *, origin_guild_id: int | None = None,
) -> list[Mapping[str, Any]]:
    """Matches validated_a/b avec validated_at <= cutoff_dt, sans Henrik
    verifie ET sans ELO deja applique (elo_applied != True).

    Le filtre sur `elo_applied` evite que le tick suivant ne retraite un match
    dont l'ELO a deja ete applique mais dont `henrik_verified` n'a pas ete
    ecrit (crash entre les deux operations).

    Si `origin_guild_id` est fourni, le scan est limite aux matches de cette
    guild (multi-guild scoping). Sinon, scanne toutes les guilds (compat
    tests / deploiement single-guild)."""
    filt: dict[str, Any] = {
        "status":       {"$in": ["validated_a", "validated_b"]},
        "validated_at": {"$lte": cutoff_dt},
        "elo_applied":  {"$ne": True},
        "$or": [
            {"henrik_verified": {"$exists": False}},
            {"henrik_verified": False},
        ],
    }
    if origin_guild_id is not None:
        filt["origin_guild_id"] = int(origin_guild_id)
    return list(get_matches_col(db).find(filt))


def set_match_henrik_verified(
    db: Database,
    match_id: Any,
    *,
    found:       bool,
    multipliers: dict[str, float] | None = None,
) -> None:
    update: dict[str, Any] = {
        "henrik_verified": True,
        "henrik_found":    bool(found),
    }
    if multipliers is not None:
        update["henrik_multipliers"] = {str(k): float(v) for k, v in multipliers.items()}
    get_matches_col(db).update_one(
        {"_id": match_id}, {"$set": update},
    )


def get_leaderboard_state_col(db: Database, guild_id: int | str) -> Collection:
    """Stocke l'etat du leaderboard auto-refresh (1 doc par guild).

    Permet au refresh de retrouver son message precedent par `message_id`
    persiste plutot que par scan de `chan.history(limit=20)`, qui rate
    les anciens leaderboards si quelqu'un a spamme >=20 messages depuis."""
    return db[f"leaderboard_state_{guild_id}"]


def get_leaderboard_message_id(
    db: Database, guild_id: int | str, queue_type: str,
) -> int | None:
    _check_queue_type(queue_type)
    doc = get_leaderboard_state_col(db, guild_id).find_one(
        {"_id": leaderboard_state_id(queue_type)}
    )
    if not doc:
        return None
    mid = doc.get("message_id")
    return int(mid) if mid is not None else None


def set_leaderboard_message_id(
    db: Database, guild_id: int | str, queue_type: str, message_id: int,
) -> None:
    _check_queue_type(queue_type)
    get_leaderboard_state_col(db, guild_id).update_one(
        {"_id": leaderboard_state_id(queue_type)},
        {"$set": {"message_id": int(message_id)}},
        upsert=True,
    )


def clear_leaderboard_message_id(
    db: Database, guild_id: int | str, queue_type: str,
) -> None:
    _check_queue_type(queue_type)
    get_leaderboard_state_col(db, guild_id).delete_one(
        {"_id": leaderboard_state_id(queue_type)}
    )


def get_applications_col(db: Database, guild_id: int | str) -> Collection:
    """1 collection par guild pour les candidatures (state machine)."""
    return db[f"applications_{guild_id}"]


def register_application(
    db: Database,
    guild_id: int | str,
    message_id: int | str,
    applicant_id: int | str,
    *,
    is_staff: bool = False,
) -> None:
    """Enregistre une candidature en etat `pending`. `_id` est le message
    Discord (qui porte les boutons accept/refuse). Idempotent via $setOnInsert."""
    from datetime import datetime
    get_applications_col(db, guild_id).update_one(
        {"_id": str(message_id)},
        {"$setOnInsert": {
            "applicant_id": str(applicant_id),
            "is_staff":     bool(is_staff),
            "status":       "pending",
            "created_at":   datetime.now(UTC),
        }},
        upsert=True,
    )


def claim_application_decision(
    db: Database,
    guild_id: int | str,
    message_id: int | str,
    *,
    status: str,
    decided_by: int | str,
) -> bool:
    """CAS atomique : transitionne la candidature de `pending` vers
    `accepted` ou `refused`. Renvoie True si on a obtenu la decision,
    False si un autre admin a deja decide (evite double-traitement :
    role grant + kick concurrents, double DM, etc.)."""
    from datetime import datetime
    if status not in ("accepted", "refused"):
        raise ValueError(f"status invalide : {status}")
    res = get_applications_col(db, guild_id).update_one(
        {"_id": str(message_id), "status": "pending"},
        {"$set": {
            "status":      status,
            "decided_by":  str(decided_by),
            "decided_at":  datetime.now(UTC),
        }},
    )
    return res.modified_count == 1


def cancel_match_atomically(
    db: Database,
    *,
    channel_id: int | str,
) -> Mapping[str, Any] | None:
    """CAS atomique : annule le match du salon `channel_id` si et seulement si
    son status est encore pending/validated/contested et que l'ELO n'a pas
    encore ete applique. Sinon renvoie None.

    Empeche la race entre `match-cancel` et :
      - un vote concurrent qui validerait le match (find_one verrait `pending`
        puis update_one ecraserait le `validated_a` deja transitionne)
      - `_verify_match` qui appliquerait l'ELO (status=cancelled mais
        elo_applied=True : etat incoherent)."""
    return get_matches_col(db).find_one_and_update(
        {
            "channel_id": channel_id,
            "status": {"$in": ["pending", "validated_a", "validated_b", "contested"]},
            "elo_applied": {"$ne": True},
        },
        {"$set": {"status": "cancelled"}},
        return_document=ReturnDocument.BEFORE,
    )


def schedule_role_cleanups(
    db: Database,
    match_id: Any,
    *,
    match_role_at,
    host_role_at,
) -> None:
    """Persiste les echeances de nettoyage de roles sur le doc match.

    Le `_timeout_loop` scanne ces timestamps et applique les revocations
    quand l'echeance est passee. Permet la reprise apres redemarrage du
    bot (sinon les `asyncio.create_task` sont perdues)."""
    get_matches_col(db).update_one(
        {"_id": match_id},
        {"$set": {
            "match_role_cleanup_at": match_role_at,
            "host_role_cleanup_at":  host_role_at,
        }},
    )


def find_pending_match_role_cleanups(
    db: Database, now, *, origin_guild_id: int | None = None,
) -> list[Mapping[str, Any]]:
    """Matches dont le cleanup du role Match #N est du et pas encore fait.

    Si `origin_guild_id` est fourni, le scan est limite aux matches de cette
    guild (multi-guild scoping). Sinon, scanne toutes les guilds (compat
    tests / deploiement single-guild).
    """
    filt: dict[str, Any] = {
        "match_role_cleanup_at":   {"$lte": now},
        "match_role_cleanup_done": {"$ne": True},
    }
    if origin_guild_id is not None:
        filt["origin_guild_id"] = int(origin_guild_id)
    return list(get_matches_col(db).find(filt))


def find_pending_host_role_cleanups(
    db: Database, now, *, origin_guild_id: int | None = None,
) -> list[Mapping[str, Any]]:
    """Matches dont le cleanup du role Match Host est du et pas encore fait.

    Si `origin_guild_id` est fourni, le scan est limite aux matches de cette
    guild (multi-guild scoping). Sinon, scanne toutes les guilds (compat
    tests / deploiement single-guild).
    """
    filt: dict[str, Any] = {
        "host_role_cleanup_at":   {"$lte": now},
        "host_role_cleanup_done": {"$ne": True},
    }
    if origin_guild_id is not None:
        filt["origin_guild_id"] = int(origin_guild_id)
    return list(get_matches_col(db).find(filt))


def claim_match_role_cleanup(
    db: Database, match_id: Any,
) -> bool:
    """CAS atomique : marque le cleanup du role Match #N en cours.

    Renvoie True si le claim est obtenu (l'appelant doit faire le cleanup
    et est le seul a y proceder), False si un autre tick l'a deja fait."""
    res = get_matches_col(db).update_one(
        {"_id": match_id, "match_role_cleanup_done": {"$ne": True}},
        {"$set": {"match_role_cleanup_done": True}},
    )
    return res.modified_count == 1


def claim_host_role_cleanup(
    db: Database, match_id: Any,
) -> bool:
    """CAS atomique : marque le cleanup du role Match Host en cours."""
    res = get_matches_col(db).update_one(
        {"_id": match_id, "host_role_cleanup_done": {"$ne": True}},
        {"$set": {"host_role_cleanup_done": True}},
    )
    return res.modified_count == 1
