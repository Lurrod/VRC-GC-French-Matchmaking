"""
Pro Queue Captain Draft Service.

Module isole pour la pro queue uniquement. Contient :
  - pick_captains : selection des 2 capitaines (top 2 ELO, tie = RNG)
  - DraftState    : etat immutable du draft
  - CaptainDraftSession : orchestration Discord (UI + machine d'etat)

Open et GC queues n'utilisent PAS ce module : elles continuent
de passer par plan_match (auto-balance).
"""
from __future__ import annotations

import random
from typing import Sequence

from services.team_balancer import Player


def pick_captains(
    players: Sequence[Player],
    *,
    rng: random.Random,
) -> tuple[Player, Player]:
    """Designe 2 capitaines : top 2 ELO, tie-break aleatoire.

    Args:
        players: liste de Player (typiquement 10).
        rng: random.Random seede (pour reproductibilite des tests).

    Returns:
        (cap_a, cap_b) : les deux premiers joueurs apres tri ELO
        decroissant avec tie-break aleatoire. cap_a.elo >= cap_b.elo
        sauf si les deux partagent le meme ELO.
    """
    if len(players) < 2:
        raise ValueError(f"Il faut au moins 2 joueurs, recu {len(players)}")

    # Tri par ELO decroissant, RNG sur les egalites.
    # On groupe par ELO et on melange chaque groupe avec rng.
    by_elo: dict[int, list[Player]] = {}
    for p in players:
        by_elo.setdefault(p.elo, []).append(p)
    ordered: list[Player] = []
    for elo in sorted(by_elo.keys(), reverse=True):
        rng.shuffle(by_elo[elo])
        ordered.extend(by_elo[elo])
    return ordered[0], ordered[1]
