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
from dataclasses import dataclass, replace
from typing import Literal, Sequence

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


# Snake order ABBAABBA. Sur 8 picks, capA pick aux indices 0, 3, 4, 7
# et capB pick aux indices 1, 2, 5, 6. Avec les 2 captains deja en team,
# chaque equipe finit avec 5 joueurs (1 cap + 4 picks).
PICK_SEQUENCE: tuple[Literal["A", "B"], ...] = (
    "A", "B", "B", "A", "A", "B", "B", "A",
)

DraftStatus = Literal["picking", "complete", "cancelled"]


@dataclass(frozen=True)
class DraftState:
    cap_a:       Player
    cap_b:       Player
    team_a:      tuple[Player, ...]
    team_b:      tuple[Player, ...]
    pool:        tuple[Player, ...]
    turn_index:  int
    status:      DraftStatus

    @classmethod
    def initial(
        cls,
        *,
        cap_a: Player,
        cap_b: Player,
        pool: tuple[Player, ...],
    ) -> "DraftState":
        return cls(
            cap_a=cap_a,
            cap_b=cap_b,
            team_a=(cap_a,),
            team_b=(cap_b,),
            pool=tuple(pool),
            turn_index=0,
            status="picking",
        )

    @property
    def is_complete(self) -> bool:
        return self.turn_index >= len(PICK_SEQUENCE)

    @property
    def current_captain(self) -> Player:
        if self.is_complete:
            raise RuntimeError("Draft complet : pas de capitaine courant.")
        side = PICK_SEQUENCE[self.turn_index]
        return self.cap_a if side == "A" else self.cap_b

    def apply_pick(self, player: Player) -> "DraftState":
        """Retourne un nouvel etat avec `player` ajoute a l'equipe du cap courant.

        Raises:
            ValueError si player n'est pas dans pool.
            RuntimeError si draft deja complet ou cancelled.
        """
        if self.status != "picking":
            raise RuntimeError(f"Draft status={self.status}, impossible de pick.")
        if player not in self.pool:
            raise ValueError(f"Joueur {player.id} pas dans le pool.")
        side = PICK_SEQUENCE[self.turn_index]
        new_pool = tuple(p for p in self.pool if p.id != player.id)
        if side == "A":
            new_team_a = self.team_a + (player,)
            new_team_b = self.team_b
        else:
            new_team_a = self.team_a
            new_team_b = self.team_b + (player,)
        new_turn = self.turn_index + 1
        new_status: DraftStatus = "complete" if new_turn >= len(PICK_SEQUENCE) else "picking"
        return replace(
            self,
            team_a=new_team_a,
            team_b=new_team_b,
            pool=new_pool,
            turn_index=new_turn,
            status=new_status,
        )
