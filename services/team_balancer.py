"""
Equilibrage de 2 equipes de 5 a partir de 10 joueurs.

Algorithme : brute-force optimal.
  - C(10,5) = 252 partitions, mais on fixe le joueur 0 dans team A pour eviter
    les doublons symetriques -> C(9,4) = 126 candidates.
  - Score primaire   : minimiser |sum(A.elo) - sum(B.elo)|
  - Tie-breaker      : minimiser |max(A.elo) - max(B.elo)| (pas de stack solo)
  - Tie-breaker 2    : ordre lexicographique des IDs (deterministe)

Complexite : O(126 * 10) ~= 1.3k operations. Largement sous la milliseconde.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Iterable, Final


TEAM_SIZE:    Final[int] = 5
TOTAL_PLAYERS: Final[int] = 10


@dataclass(frozen=True)
class Player:
    id:   int        # Discord user id
    name: str
    elo:  int


@dataclass(frozen=True)
class BalancedTeams:
    team_a:    tuple[Player, ...]
    team_b:    tuple[Player, ...]
    elo_diff:  int   # |sum(A) - sum(B)|
    peak_diff: int   # |max(A) - max(B)|

    @property
    def total_a(self) -> int:
        return sum(p.elo for p in self.team_a)

    @property
    def total_b(self) -> int:
        return sum(p.elo for p in self.team_b)


def balance_teams(players: Iterable[Player]) -> BalancedTeams:
    """
    Renvoie la repartition la plus equilibree.

    Raises:
        ValueError si len(players) != 10 ou si IDs en doublon.
    """
    pool = tuple(players)
    if len(pool) != TOTAL_PLAYERS:
        raise ValueError(f"Il faut exactement {TOTAL_PLAYERS} joueurs, recu {len(pool)}")
    if len({p.id for p in pool}) != TOTAL_PLAYERS:
        raise ValueError("Doublons d'ID detectes dans la liste de joueurs")

    best: BalancedTeams | None = None
    best_key: tuple[int, int, tuple[int, ...]] | None = None

    # On fixe pool[0] dans team A pour ne generer que des partitions uniques.
    # On choisit 4 autres parmi pool[1..9] -> C(9,4) = 126 combinaisons.
    indices_rest = range(1, TOTAL_PLAYERS)
    for combo in itertools.combinations(indices_rest, TEAM_SIZE - 1):
        a_idx = (0, *combo)
        a_set = set(a_idx)
        team_a = tuple(pool[i] for i in a_idx)
        team_b = tuple(pool[i] for i in range(TOTAL_PLAYERS) if i not in a_set)

        sum_a = sum(p.elo for p in team_a)
        sum_b = sum(p.elo for p in team_b)
        elo_diff = abs(sum_a - sum_b)

        max_a = max(p.elo for p in team_a)
        max_b = max(p.elo for p in team_b)
        peak_diff = abs(max_a - max_b)

        # Tie-breaker 2 : ordre des IDs (deterministe pour tests)
        id_signature = tuple(sorted(p.id for p in team_a))
        key = (elo_diff, peak_diff, id_signature)

        if best_key is None or key < best_key:
            best_key = key
            best = BalancedTeams(
                team_a=team_a,
                team_b=team_b,
                elo_diff=elo_diff,
                peak_diff=peak_diff,
            )

    assert best is not None
    return best


def format_teams(teams: BalancedTeams) -> str:
    """Format texte compact pour log/debug."""
    a_str = ", ".join(f"{p.name}({p.elo})" for p in teams.team_a)
    b_str = ", ".join(f"{p.name}({p.elo})" for p in teams.team_b)
    return (
        f"Team A [{teams.total_a}] : {a_str}\n"
        f"Team B [{teams.total_b}] : {b_str}\n"
        f"diff={teams.elo_diff}  peak_diff={teams.peak_diff}"
    )
