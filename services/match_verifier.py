"""
Verification d'un match du bot via HenrikDev API et calcul des multiplicateurs
ACS pour ajustement individualise de l'ELO.

Flux :
  1. Recuperer l'historique custom recent du lobby leader.
  2. Trouver le match contenant les 10 puuids attendus, demarre apres `after`.
  3. Calculer l'ACS de chaque joueur et son multiplicateur (clampe [0.7, 1.3])
     par rapport a la moyenne d'equipe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Mapping

from services.riot_api import (
    HenrikDevClient,
    MatchPlayerStats,
    MatchSummary,
    RiotApiError,
)


CUSTOM_MODE_NAME: Final[str] = "Custom Game"
DEFAULT_MULT_MIN: Final[float] = 0.7
DEFAULT_MULT_MAX: Final[float] = 1.3


@dataclass(frozen=True)
class PlayerPerformance:
    user_id:    str
    puuid:      str
    acs:        float
    multiplier: float
    win:        bool


@dataclass(frozen=True)
class VerifiedMatch:
    matchid:      str
    started_at:   datetime
    winning_team: str  # "Red" ou "Blue" (vide si nul)
    performances: tuple[PlayerPerformance, ...]


def find_henrik_custom_match(
    client: HenrikDevClient,
    *,
    region: str,
    leader_name: str,
    leader_tag:  str,
    expected_puuids: set[str],
    after: datetime,
    history_size: int = 10,
) -> MatchSummary | None:
    """Cherche un match custom du `leader` qui contient `expected_puuids` et
    qui a demarre apres `after`. Retourne le `MatchSummary` ou None.
    """
    try:
        history = client.get_match_history(
            region, leader_name, leader_tag,
            size=history_size, mode="custom",
        )
    except RiotApiError:
        return None

    for match in history:
        if match.mode != CUSTOM_MODE_NAME:
            continue
        if match.started_at < after:
            continue
        match_puuids = {p.puuid for p in match.players}
        if expected_puuids.issubset(match_puuids):
            return match
    return None


def compute_acs_multipliers(
    match: MatchSummary,
    *,
    team_a_uid_by_puuid: Mapping[str, str],
    team_b_uid_by_puuid: Mapping[str, str],
    mult_min: float = DEFAULT_MULT_MIN,
    mult_max: float = DEFAULT_MULT_MAX,
) -> VerifiedMatch:
    """Calcule l'ACS et le multiplicateur clampe pour chaque joueur, en se
    basant sur la moyenne d'equipe (cote Henrik : Red / Blue, mappee aux
    teams a/b du bot via les puuids fournis)."""
    rounds = max(match.rounds_played, 1)
    if match.rounds_red > match.rounds_blue:
        winning = "Red"
    elif match.rounds_blue > match.rounds_red:
        winning = "Blue"
    else:
        winning = ""  # nul, edge case

    by_puuid: dict[str, MatchPlayerStats] = {p.puuid: p for p in match.players}

    perfs: list[PlayerPerformance] = []
    for team_uids in (team_a_uid_by_puuid, team_b_uid_by_puuid):
        labels = {by_puuid[pu].team for pu in team_uids if pu in by_puuid}
        if len(labels) != 1:
            continue  # equipe incoherente cote Henrik
        side = next(iter(labels))

        team_acs = [
            by_puuid[pu].score / rounds for pu in team_uids if pu in by_puuid
        ]
        if not team_acs:
            continue
        avg_acs = sum(team_acs) / len(team_acs)
        if avg_acs <= 0:
            avg_acs = 1.0

        for pu, uid in team_uids.items():
            stats = by_puuid.get(pu)
            if stats is None:
                continue
            acs  = stats.score / rounds
            raw  = acs / avg_acs
            mult = max(mult_min, min(mult_max, raw))
            perfs.append(PlayerPerformance(
                user_id=uid,
                puuid=pu,
                acs=acs,
                multiplier=mult,
                win=(side == winning),
            ))

    return VerifiedMatch(
        matchid=match.matchid,
        started_at=match.started_at,
        winning_team=winning,
        performances=tuple(perfs),
    )
