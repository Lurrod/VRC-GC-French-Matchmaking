"""
Met a jour l'ELO des joueurs (table V1 `elo_<guild_id>`) apres validation
d'un match V2.

Le gain/loss est proportionnel a la moyenne d'effective_elo (Riot) des
10 joueurs du match : avg=1500 -> +20/-10, avg=3000 -> +40/-20.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from pymongo import ReturnDocument

from services import elo_calc, repository


VALIDATED_A: Final[str] = "validated_a"
VALIDATED_B: Final[str] = "validated_b"

# Fallback fixe quand HenrikDev ne fournit pas de multiplicateurs ACS
# (custom introuvable apres timeout 30 min, ou extraction impossible :
# teams mixtes Attack/Defense en lobby Valorant). On applique +20/-20
# a plat plutot que la valeur proportionnelle a l'avg ELO du match.
# C'est plus lisible pour les joueurs et evite les variations bizarres
# (15/17/19) selon le tier moyen.
FLAT_FALLBACK_ELO_CHANGE: Final[int] = 16


@dataclass(frozen=True)
class PlayerEloChange:
    user_id:    str
    name:       str
    old_elo:    int
    new_elo:    int
    delta:      int
    win:        bool
    multiplier: float = 1.0


@dataclass(frozen=True)
class MatchEloOutcome:
    avg_elo:     int
    gain:        int
    loss:        int
    changes:     tuple[PlayerEloChange, ...]
    weighted:    bool = False  # True si appel avec multipliers Henrik


def apply_match_validation(
    db,
    guild_id: int | str,
    match_doc: dict,
    multipliers: dict[str, float] | None = None,
) -> MatchEloOutcome:
    """
    Distribue les ELO en une seule passe, **zero-sum strict** :
    sum(deltas_gagnants) + sum(deltas_perdants) == 0, plancher a 0 ELO inclus.

    Le total d'une equipe est `n * base_change`. Les multiplicateurs
    redistribuent ce total au sein de l'equipe :
      - gagnant : poids = mult           (mult eleve -> plus gros gain)
      - perdant : poids = (2 - mult)     (mult eleve -> plus faible perte)
    Le total par equipe est donc preserve quoi qu'il arrive aux multiplicateurs
    individuels.

    Si un joueur est absent du dict, mult=1.0 (poids = 1).
    Si `multipliers` est None, distribution plate (chacun base_change).

    Plancher a 0 : si un perdant a moins d'ELO que la perte calculee, son
    delta est clamp a -old_elo (ne descend pas sous 0). La portion non-
    perdable est retiree des gains gagnants pour preserver le zero-sum
    et eviter une injection nette d'ELO dans le systeme.

    Args:
        db:          Database mongomock/pymongo
        guild_id:    guild Discord
        match_doc:   doc match avec `team_a`, `team_b`, `status` validated_a/b
        multipliers: dict user_id (str) -> multiplicateur ACS (~0.7..1.3)

    Raises:
        ValueError si status != validated_a/b
    """
    status = match_doc.get("status")
    if status not in (VALIDATED_A, VALIDATED_B):
        raise ValueError(f"Match non valide : status={status}")

    if status == VALIDATED_A:
        winners, losers = match_doc["team_a"], match_doc["team_b"]
    else:
        winners, losers = match_doc["team_b"], match_doc["team_a"]

    avg_elo = elo_calc.compute_team_avg_elo(winners + losers)
    if multipliers is None:
        # Pas de donnees Henrik : on applique +16/-16 a plat. La valeur
        # proportionnelle (`compute_match_elo_change`) ne sert plus
        # qu'au cas pondere ACS, ou l'avg du match a un sens (les
        # multiplicateurs distribuent le total).
        base_gain = FLAT_FALLBACK_ELO_CHANGE
        base_loss = FLAT_FALLBACK_ELO_CHANGE
    else:
        base_gain, base_loss = elo_calc.compute_match_elo_change(avg_elo)

    mults    = multipliers or {}
    weighted = multipliers is not None
    elo_col  = repository.get_elo_col(db, guild_id)

    winner_mults = [float(mults.get(str(p["id"]), 1.0)) for p in winners]
    loser_mults  = [float(mults.get(str(p["id"]), 1.0)) for p in losers]

    # Distribution per-joueur ancree sur le multiplicateur :
    #   gagnant : delta = +base * mult        (mult=1.0 -> +base pile)
    #   perdant : delta = -base * (2 - mult)  (mult=1.0 -> -base pile)
    # Le "joueur du milieu" (perf = moyenne d'equipe -> mult=1.0) recoit
    # exactement base, quel que soit le scaling des coequipiers.
    # Quand sum(mults) ≈ n par equipe (cas non-clamp), zero-sum tient
    # encore naturellement ; sinon une legere inflation/deflation est
    # acceptee comme prix de l'ancrage.
    winner_deltas = [
        int(round(+base_gain * m)) for m in winner_mults
    ]
    loser_deltas = [
        int(round(-base_loss * (2.0 - m))) for m in loser_mults
    ]

    # Clamp a 0 pour les perdants : on ne descend jamais sous 0 ELO.
    loser_old_elos: list[int] = []
    for p in losers:
        doc = elo_col.find_one({"_id": str(p["id"])})
        loser_old_elos.append(
            int(doc.get("elo", elo_calc.ELO_START)) if doc else elo_calc.ELO_START
        )
    clamped_loser_deltas: list[int] = []
    for old_elo, delta in zip(loser_old_elos, loser_deltas):
        # delta est negatif. La perte maximale possible est -old_elo.
        max_loss_delta = -old_elo
        clamped_loser_deltas.append(max(max_loss_delta, delta))

    match_id = match_doc.get("_id")
    changes: list[PlayerEloChange] = []
    for p, delta, mult in zip(winners, winner_deltas, winner_mults):
        changes.append(_apply_player(elo_col, p, match_id=match_id, delta=delta, win=True, multiplier=mult))
    for p, delta, mult in zip(losers, clamped_loser_deltas, loser_mults):
        changes.append(_apply_player(elo_col, p, match_id=match_id, delta=delta, win=False, multiplier=mult))

    return MatchEloOutcome(
        avg_elo=avg_elo,
        gain=base_gain,
        loss=base_loss,
        changes=tuple(changes),
        weighted=weighted,
    )


def _apply_player(
    col, player: dict, *, match_id: Any, delta: int, win: bool, multiplier: float = 1.0,
) -> PlayerEloChange:
    """Applique le delta ELO de maniere **idempotente par match**.

    Utilise un set `processed_matches` sur le doc joueur pour eviter la
    double-application si `apply_match_validation` est rejouee apres un
    crash partiel (release_elo_claim suivi d'un nouveau claim au prochain
    tick). Le filtre `processed_matches: {$nin: [match_id]}` rend la mise
    a jour CAS atomique."""
    uid  = str(player["id"])
    name = player.get("name", uid)
    match_id_str = str(match_id) if match_id is not None else None

    # 1) Ensure doc existe (idempotent, ne touche pas elo/wins/losses si deja la).
    col.update_one(
        {"_id": uid},
        {"$setOnInsert": {
            "name": name,
            "elo":  elo_calc.ELO_START,
            "wins": 0,
            "losses": 0,
        }},
        upsert=True,
    )

    inc_field = "wins" if win else "losses"
    update: dict[str, Any] = {
        "$inc": {"elo": delta, inc_field: 1},
        "$set": {"name": name},
    }
    if match_id_str is not None:
        update["$addToSet"] = {"processed_matches": match_id_str}
        filter_q = {"_id": uid, "processed_matches": {"$nin": [match_id_str]}}
    else:
        filter_q = {"_id": uid}

    # 2) CAS atomique : applique uniquement si match pas deja processe.
    pre = col.find_one_and_update(
        filter_q, update, return_document=ReturnDocument.BEFORE,
    )

    if pre is None:
        # Match deja applique pour ce joueur : no-op idempotent.
        cur_doc = col.find_one({"_id": uid})
        cur_elo = int(cur_doc.get("elo", 0)) if cur_doc else 0
        return PlayerEloChange(
            user_id=uid, name=name,
            old_elo=cur_elo, new_elo=cur_elo,
            delta=0, win=win, multiplier=multiplier,
        )

    old_elo = int(pre.get("elo", 0))
    new_elo = old_elo + delta
    return PlayerEloChange(
        user_id=uid,
        name=name,
        old_elo=old_elo,
        new_elo=new_elo,
        delta=delta,
        win=win,
        multiplier=multiplier,
    )
