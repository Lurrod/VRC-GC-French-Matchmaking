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

    avg_elo              = elo_calc.compute_team_avg_elo(winners + losers)
    base_gain, base_loss = elo_calc.compute_match_elo_change(avg_elo)

    mults    = multipliers or {}
    weighted = multipliers is not None
    elo_col  = repository.get_elo_col(db, guild_id)

    winner_mults = [float(mults.get(str(p["id"]), 1.0)) for p in winners]
    loser_mults  = [float(mults.get(str(p["id"]), 1.0)) for p in losers]

    winner_deltas = _distribute_team_deltas(
        team_total=+base_gain * len(winners),
        multipliers=winner_mults,
        is_winner=True,
    )
    loser_deltas = _distribute_team_deltas(
        team_total=-base_loss * len(losers),
        multipliers=loser_mults,
        is_winner=False,
    )

    # Pre-fetch des ELO actuels des perdants pour clamper a 0 sans
    # injecter d'ELO dans le systeme. Sans ce traitement, chaque ELO
    # "non-perdable" (ex: perdant a 5 ELO doit perdre 15 -> ne perd que
    # 5, le diff de 10 reste cree dans le systeme via les gains
    # gagnants intacts) genere une inflation cumulative.
    loser_old_elos: list[int] = []
    for p in losers:
        doc = elo_col.find_one({"_id": str(p["id"])})
        loser_old_elos.append(
            int(doc.get("elo", elo_calc.ELO_START)) if doc else elo_calc.ELO_START
        )
    clamped_loser_deltas: list[int] = []
    unrecoverable = 0  # somme >= 0 du "manque a perdre"
    for old_elo, delta in zip(loser_old_elos, loser_deltas):
        # delta est negatif. La perte maximale possible est -old_elo.
        max_loss_delta = -old_elo
        clamped = max(max_loss_delta, delta)  # delta plus proche de 0
        unrecoverable += clamped - delta      # >= 0
        clamped_loser_deltas.append(clamped)

    if unrecoverable > 0:
        # Reduire le total des gains gagnants. Le residu est borne a 0
        # (on ne distribue jamais de pertes aux gagnants).
        new_winner_total = max(0, sum(winner_deltas) - unrecoverable)
        winner_deltas = _distribute_team_deltas(
            team_total=new_winner_total,
            multipliers=winner_mults,
            is_winner=True,
        )

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


def _distribute_team_deltas(
    *,
    team_total: int,
    multipliers: list[float],
    is_winner: bool,
) -> list[int]:
    """Distribue `team_total` (signe) sur n joueurs, ponderee par les multiplicateurs.

    Garantit `sum(deltas) == team_total` exactement, meme apres arrondi entier.

    Le poids individuel est :
      - gagnant : mult            (mult eleve -> part plus grosse du gain)
      - perdant : (2 - mult)      (mult eleve = bonne perf -> part plus petite de la perte)
    """
    n = len(multipliers)
    if n == 0:
        return []
    weights = [m if is_winner else (2.0 - m) for m in multipliers]
    total_weight = sum(weights)
    if total_weight <= 0:
        weights = [1.0] * n
        total_weight = float(n)

    raw     = [team_total * w / total_weight for w in weights]
    deltas  = [int(round(r)) for r in raw]
    diff    = team_total - sum(deltas)
    if diff != 0:
        # Distribue le residu d'arrondi sur les joueurs au plus gros reste
        # fractionnaire, dans le sens approprie.
        residuals = sorted(
            range(n),
            key=lambda i: (raw[i] - deltas[i]) * (1 if diff > 0 else -1),
            reverse=True,
        )
        step = 1 if diff > 0 else -1
        for k in range(abs(diff)):
            deltas[residuals[k % n]] += step
    return deltas


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
