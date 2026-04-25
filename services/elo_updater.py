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
    user_id: str
    name:    str
    old_elo: int
    new_elo: int
    delta:   int
    win:     bool


@dataclass(frozen=True)
class MatchEloOutcome:
    avg_elo: int
    gain:    int
    loss:    int
    changes: tuple[PlayerEloChange, ...]


def apply_match_validation(db, guild_id: int | str, match_doc: dict) -> MatchEloOutcome:
    """
    Distribue +gain aux gagnants et -loss aux perdants.

    Args:
        db:        Database mongomock/pymongo
        guild_id:  guild Discord
        match_doc: doc match avec `team_a`, `team_b`, `status` validated_a/b

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

    avg_elo    = elo_calc.compute_team_avg_elo(winners + losers)
    gain, loss = elo_calc.compute_match_elo_change(avg_elo)

    elo_col  = repository.get_elo_col(db, guild_id)
    changes: list[PlayerEloChange] = []

    for p in winners:
        change = _apply_player(elo_col, p, delta=+gain, win=True)
        changes.append(change)

    for p in losers:
        change = _apply_player(elo_col, p, delta=-loss, win=False)
        changes.append(change)

    return MatchEloOutcome(
        avg_elo=avg_elo,
        gain=gain,
        loss=loss,
        changes=tuple(changes),
    )


def _apply_player(col, player: dict, *, delta: int, win: bool) -> PlayerEloChange:
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
    )
