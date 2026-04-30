"""
Met a jour l'ELO des joueurs (table V1 `elo_<guild_id>`) apres validation
d'un match V2.

Le gain/loss est proportionnel a la moyenne d'effective_elo (Riot) des
10 joueurs du match : avg=1500 -> +20/-10, avg=3000 -> +40/-20.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

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
    Distribue les ELO en une seule passe, **zero-sum garanti** :
    sum(deltas_gagnants) == -sum(deltas_perdants) AVANT le plancher a 0.

    Le total d'une equipe est toujours `n * base_change`. Les multiplicateurs
    redistribuent ce total au sein de l'equipe :
      - gagnant : poids = mult           (mult eleve -> plus gros gain)
      - perdant : poids = (2 - mult)     (mult eleve -> plus faible perte)
    Le total par equipe est donc preserve quoi qu'il arrive aux multiplicateurs
    individuels (clamping inclus).

    Si un joueur est absent du dict, mult=1.0 (poids = 1).
    Si `multipliers` est None, distribution plate (chacun base_change).

    Note : le plancher a 0 ELO sur les perdants (`max(0, old + delta)`) peut
    casser le zero-sum strict si un perdant tombe sous 0 ; c'est volontaire
    (on ne descend jamais sous 0).

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

    changes: list[PlayerEloChange] = []
    for p, delta, mult in zip(winners, winner_deltas, winner_mults):
        changes.append(_apply_player(elo_col, p, delta=delta, win=True, multiplier=mult))
    for p, delta, mult in zip(losers, loser_deltas, loser_mults):
        changes.append(_apply_player(elo_col, p, delta=delta, win=False, multiplier=mult))

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
    col, player: dict, *, delta: int, win: bool, multiplier: float = 1.0,
) -> PlayerEloChange:
    uid  = str(player["id"])
    name = player.get("name", uid)
    doc  = col.find_one({"_id": uid})
    if not doc:
        col.insert_one({
            "_id": uid, "name": name,
            "elo": elo_calc.ELO_START, "wins": 0, "losses": 0,
        })
        doc = {"elo": elo_calc.ELO_START, "wins": 0, "losses": 0}

    old_elo = int(doc.get("elo", 0))
    if win:
        new_elo = old_elo + delta            # delta > 0
        col.update_one(
            {"_id": uid},
            {"$set": {"elo": new_elo, "name": name}, "$inc": {"wins": 1}},
        )
    else:
        new_elo = max(0, old_elo + delta)    # delta < 0, plancher a 0
        col.update_one(
            {"_id": uid},
            {"$set": {"elo": new_elo, "name": name}, "$inc": {"losses": 1}},
        )

    return PlayerEloChange(
        user_id=uid,
        name=name,
        old_elo=old_elo,
        new_elo=new_elo,
        delta=new_elo - old_elo,
        win=win,
        multiplier=multiplier,
    )
