"""Acces MongoDB centralise. Toutes les collections passent par ici."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping
from pymongo import ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)


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


def get_elo_col(db: Database, guild_id: int | str) -> Collection:
    """Collection ELO d'un guild (1 collection par serveur Discord)."""
    col = db[f"elo_{guild_id}"]
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
    display_name: str,
    initial_elo: int = 0,
) -> Mapping[str, Any]:
    """Recupere ou cree atomiquement le doc joueur, met a jour le display_name.

    Atomique via find_one_and_update + upsert : empeche le DuplicateKeyError si
    deux callers concurrents (ex: deux /win simultanes) tentent de creer le
    meme joueur. `$setOnInsert` garantit que `elo`/`wins`/`losses` ne sont
    initialises qu'a la premiere creation."""
    uid = str(user_id)
    doc = col.find_one_and_update(
        {"_id": uid},
        {
            "$set": {"name": display_name},
            "$setOnInsert": {
                "elo":    initial_elo,
                "wins":   0,
                "losses": 0,
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc


# ── V2 : comptes Riot lies ───────────────────────────────────────
def get_riot_col(db: Database, guild_id: int | str) -> Collection:
    """1 collection par guild pour les comptes Riot lies."""
    col = db[f"riot_accounts_{guild_id}"]
    _ensure_indexes(col, "riot")
    return col


def find_riot_account_by_puuid(
    db: Database, guild_id: int | str, puuid: str,
) -> Mapping[str, Any] | None:
    """Renvoie le doc riot_account ayant ce puuid, ou None.

    Utilise pour la dedup PUUID dans /link-riot : empeche un meme compte
    Riot d'etre lie a deux comptes Discord differents (multi-account)."""
    if not puuid:
        return None
    return get_riot_col(db, guild_id).find_one({"puuid": puuid})


def link_riot_account(
    db: Database,
    guild_id: int | str,
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

    L'ELO de matchmaking est stockee dans `elo_<guild_id>` ; ce doc ne sert
    plus qu'a (a) verifier qu'un joueur est lie pour rejoindre la queue,
    (b) afficher le rang Riot de reference.
    """
    from datetime import datetime, timezone
    get_riot_col(db, guild_id).update_one(
        {"_id": str(user_id)},
        {"$set": {
            "riot_name":     riot_name,
            "riot_tag":      riot_tag,
            "riot_region":   riot_region,
            "puuid":         puuid,
            "peak_elo":      peak_elo,
            "source":        source,
            "fetched_at":    datetime.now(timezone.utc),
        }},
        upsert=True,
    )


def seed_elo_with_riot_base(
    db: Database,
    guild_id: int | str,
    user_id: int | str,
    *,
    riot_base_elo: int,
    display_name: str,
) -> tuple[int, bool]:
    """Ajoute riot_base_elo a `elo_<guild>.elo`, **une seule fois** par joueur.

    Atomique : protege contre la double comptabilisation si /link-riot est
    rappele apres /unlink-riot. Le seeding est marque par le flag
    `linked_once: True` dans le doc joueur.

    Renvoie (elo_final, seeded_now). seeded_now=False si le joueur avait
    deja ete seede une fois (ELO inchangee).
    """
    col = get_elo_col(db, guild_id)
    uid = str(user_id)

    # 1) Atomique : seed le doc s'il n'est pas encore marque linked_once.
    # Couvre deux cas en une seule operation :
    #   - Doc existant non seede : $inc applique le seed
    #   - Doc absent : upsert cree un doc neuf avec elo = riot_base_elo
    # Si le doc existe deja avec linked_once=True, le filtre echoue et
    # l'upsert leve DuplicateKeyError (gere ci-dessous).
    try:
        res = col.find_one_and_update(
            {"_id": uid, "linked_once": {"$ne": True}},
            {
                "$inc": {"elo": int(riot_base_elo)},
                "$set": {"name": display_name, "linked_once": True},
                "$setOnInsert": {"wins": 0, "losses": 0},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if res is not None:
            return int(res["elo"]), True
    except DuplicateKeyError:
        pass  # Doc existe deja avec linked_once=True : on tombe en branche 2

    # 2) Deja seede : ELO inchangee, on rafraichit juste le display_name
    after = col.find_one_and_update(
        {"_id": uid},
        {"$set": {"name": display_name}},
        return_document=ReturnDocument.AFTER,
    )
    return (int(after["elo"]) if after else 0), False


def get_riot_account(db: Database, guild_id: int | str, user_id: int | str) -> Mapping[str, Any] | None:
    return get_riot_col(db, guild_id).find_one({"_id": str(user_id)})


def unlink_riot_account(db: Database, guild_id: int | str, user_id: int | str) -> bool:
    """Renvoie True si une entree a ete supprimee."""
    res = get_riot_col(db, guild_id).delete_one({"_id": str(user_id)})
    return res.deleted_count > 0


# ── V2 : queue 10mans ─────────────────────────────────────────────
QUEUE_SIZE_DEFAULT = 10


def get_queue_col(db: Database, guild_id: int | str) -> Collection:
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

    # Contrainte de taille atomique : empeche 2 ajouts concurrents (ou 2
    # instances bot en parallele) de depasser max_size meme si la pre-check
    # plus haut a passe pour les deux. Permet le scaling multi-instance.
    updated = col.find_one_and_update(
        {
            "_id": "active",
            "status": "open",
            "players": {"$nin": [uid_str]},
            "$expr": {
                "$lt": [
                    {"$size": {"$ifNull": ["$players", []]}},
                    max_size,
                ],
            },
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
def get_matches_col(db: Database, guild_id: int | str) -> Collection:
    col = db[f"matches_{guild_id}"]
    _ensure_indexes(col, "matches")
    return col


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
    """Enregistre/ecrase le vote d'un user. Renvoie le doc apres maj.

    CAS sur `status: pending` : empeche les votes tardifs sur un match
    deja annule, contesté ou validé. Un retardataire qui clique alors
    que le match est cancelled n'enregistre rien (None) au lieu de
    polluer `votes` apres-coup."""
    if choice not in ("a", "b"):
        raise ValueError(f"choice doit etre 'a' ou 'b', recu {choice!r}")
    return get_matches_col(db, guild_id).find_one_and_update(
        {"_id": match_id, "status": "pending"},
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


def transition_match_status(
    db: Database,
    guild_id: int | str,
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
    from datetime import datetime, timezone
    update: dict[str, Any] = {"status": to_status}
    if to_status in ("validated_a", "validated_b"):
        update["validated_at"] = validated_at or datetime.now(timezone.utc)
    return get_matches_col(db, guild_id).find_one_and_update(
        {"_id": match_id, "status": from_status},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )


def claim_match_for_elo(
    db: Database,
    guild_id: int | str,
    match_id: Any,
) -> Mapping[str, Any] | None:
    """Atomic claim : marque `elo_applied=True` uniquement si non deja applique.

    Empeche la double-application d'ELO si la verification HenrikDev re-tente
    apres un crash entre `apply_match_validation` et `set_match_henrik_verified`.

    Returns:
        Le doc apres claim si on a bien obtenu le verrou, None si deja claime.
    """
    from datetime import datetime, timezone
    return get_matches_col(db, guild_id).find_one_and_update(
        {
            "_id":         match_id,
            "status":      {"$in": ["validated_a", "validated_b"]},
            "elo_applied": {"$ne": True},
        },
        {"$set": {
            "elo_applied":    True,
            "elo_applied_at": datetime.now(timezone.utc),
        }},
        return_document=ReturnDocument.AFTER,
    )


def release_elo_claim(
    db: Database,
    guild_id: int | str,
    match_id: Any,
) -> None:
    """Annule le claim si l'application ELO a echoue (rollback)."""
    get_matches_col(db, guild_id).update_one(
        {"_id": match_id},
        {"$unset": {"elo_applied": "", "elo_applied_at": ""}},
    )


def find_validated_unverified(
    db: Database, guild_id: int | str, cutoff_dt,
) -> list[Mapping[str, Any]]:
    """Matches validated_a/b avec validated_at <= cutoff_dt, sans Henrik
    verifie ET sans ELO deja applique (elo_applied != True).

    Le filtre sur `elo_applied` evite que le tick suivant ne retraite un match
    dont l'ELO a deja ete applique mais dont `henrik_verified` n'a pas ete
    ecrit (crash entre les deux operations)."""
    return list(get_matches_col(db, guild_id).find({
        "status":       {"$in": ["validated_a", "validated_b"]},
        "validated_at": {"$lte": cutoff_dt},
        "elo_applied":  {"$ne": True},
        "$or": [
            {"henrik_verified": {"$exists": False}},
            {"henrik_verified": False},
        ],
    }))


def set_match_henrik_verified(
    db: Database,
    guild_id: int | str,
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
    get_matches_col(db, guild_id).update_one(
        {"_id": match_id}, {"$set": update},
    )


def get_leaderboard_state_col(db: Database, guild_id: int | str) -> Collection:
    """Stocke l'etat du leaderboard auto-refresh (1 doc par guild).

    Permet au refresh de retrouver son message precedent par `message_id`
    persiste plutot que par scan de `chan.history(limit=20)`, qui rate
    les anciens leaderboards si quelqu'un a spamme >=20 messages depuis."""
    return db[f"leaderboard_state_{guild_id}"]


def get_leaderboard_message_id(
    db: Database, guild_id: int | str,
) -> int | None:
    doc = get_leaderboard_state_col(db, guild_id).find_one({"_id": "current"})
    if not doc:
        return None
    mid = doc.get("message_id")
    return int(mid) if mid is not None else None


def set_leaderboard_message_id(
    db: Database, guild_id: int | str, message_id: int,
) -> None:
    get_leaderboard_state_col(db, guild_id).update_one(
        {"_id": "current"},
        {"$set": {"message_id": int(message_id)}},
        upsert=True,
    )


def clear_leaderboard_message_id(
    db: Database, guild_id: int | str,
) -> None:
    get_leaderboard_state_col(db, guild_id).delete_one({"_id": "current"})


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
    from datetime import datetime, timezone
    get_applications_col(db, guild_id).update_one(
        {"_id": str(message_id)},
        {"$setOnInsert": {
            "applicant_id": str(applicant_id),
            "is_staff":     bool(is_staff),
            "status":       "pending",
            "created_at":   datetime.now(timezone.utc),
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
    from datetime import datetime, timezone
    if status not in ("accepted", "refused"):
        raise ValueError(f"status invalide : {status}")
    res = get_applications_col(db, guild_id).update_one(
        {"_id": str(message_id), "status": "pending"},
        {"$set": {
            "status":      status,
            "decided_by":  str(decided_by),
            "decided_at":  datetime.now(timezone.utc),
        }},
    )
    return res.modified_count == 1


def cancel_match_atomically(
    db: Database,
    guild_id: int | str,
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
    return get_matches_col(db, guild_id).find_one_and_update(
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
    guild_id: int | str,
    match_id: Any,
    *,
    match_role_at,
    host_role_at,
) -> None:
    """Persiste les echeances de nettoyage de roles sur le doc match.

    Le `_timeout_loop` scanne ces timestamps et applique les revocations
    quand l'echeance est passee. Permet la reprise apres redemarrage du
    bot (sinon les `asyncio.create_task` sont perdues)."""
    get_matches_col(db, guild_id).update_one(
        {"_id": match_id},
        {"$set": {
            "match_role_cleanup_at": match_role_at,
            "host_role_cleanup_at":  host_role_at,
        }},
    )


def find_pending_match_role_cleanups(
    db: Database, guild_id: int | str, now,
) -> list[Mapping[str, Any]]:
    """Matches dont le cleanup du role Match #N est du et pas encore fait."""
    return list(get_matches_col(db, guild_id).find({
        "match_role_cleanup_at":   {"$lte": now},
        "match_role_cleanup_done": {"$ne": True},
    }))


def find_pending_host_role_cleanups(
    db: Database, guild_id: int | str, now,
) -> list[Mapping[str, Any]]:
    """Matches dont le cleanup du role Match Host est du et pas encore fait."""
    return list(get_matches_col(db, guild_id).find({
        "host_role_cleanup_at":   {"$lte": now},
        "host_role_cleanup_done": {"$ne": True},
    }))


def claim_match_role_cleanup(
    db: Database, guild_id: int | str, match_id: Any,
) -> bool:
    """CAS atomique : marque le cleanup du role Match #N en cours.

    Renvoie True si le claim est obtenu (l'appelant doit faire le cleanup
    et est le seul a y proceder), False si un autre tick l'a deja fait."""
    res = get_matches_col(db, guild_id).update_one(
        {"_id": match_id, "match_role_cleanup_done": {"$ne": True}},
        {"$set": {"match_role_cleanup_done": True}},
    )
    return res.modified_count == 1


def claim_host_role_cleanup(
    db: Database, guild_id: int | str, match_id: Any,
) -> bool:
    """CAS atomique : marque le cleanup du role Match Host en cours."""
    res = get_matches_col(db, guild_id).update_one(
        {"_id": match_id, "host_role_cleanup_done": {"$ne": True}},
        {"$set": {"host_role_cleanup_done": True}},
    )
    return res.modified_count == 1
