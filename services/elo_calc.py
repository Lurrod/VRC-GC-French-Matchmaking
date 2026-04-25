"""Logique pure de calcul ELO. Aucune dependance Discord ni MongoDB."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


# ── Constantes ────────────────────────────────────────────────────
ELO_START: Final[int] = 0
WIN_ELO:   Final[tuple[int, ...]] = (20, 18, 17, 16, 15)
LOSE_ELO:  Final[tuple[int, ...]] = (10, 10, 12, 13, 15)
MAPS:      Final[tuple[str, ...]] = (
    "Breeze", "Bind", "Lotus", "Fracture", "Split", "Haven", "Pearl",
)

# Limites de validation pour les commandes admin (eviter les valeurs absurdes)
MAX_ELO_MODIFICATION: Final[int] = 10_000
MAX_WIN_LOSS_MODIFICATION: Final[int] = 1_000


@dataclass(frozen=True)
class EloResult:
    old_elo: int
    new_elo: int
    delta: int


def gain_for_rank(rank: int) -> int:
    """Renvoie le gain d'ELO pour la place 0..4 (0 = top frag)."""
    if not 0 <= rank < len(WIN_ELO):
        raise ValueError(f"rank doit etre dans [0, {len(WIN_ELO) - 1}], recu {rank}")
    return WIN_ELO[rank]


def loss_for_rank(rank: int) -> int:
    """Renvoie la perte d'ELO pour la place 0..4."""
    if not 0 <= rank < len(LOSE_ELO):
        raise ValueError(f"rank doit etre dans [0, {len(LOSE_ELO) - 1}], recu {rank}")
    return LOSE_ELO[rank]


def apply_win(current_elo: int, rank: int) -> EloResult:
    gain = gain_for_rank(rank)
    return EloResult(old_elo=current_elo, new_elo=current_elo + gain, delta=gain)


def apply_loss(current_elo: int, rank: int) -> EloResult:
    loss = loss_for_rank(rank)
    new_elo = max(0, current_elo - loss)
    return EloResult(old_elo=current_elo, new_elo=new_elo, delta=-(current_elo - new_elo))


def apply_elo_modification(current_elo: int, action: str, amount: int) -> EloResult:
    """Applique une modification manuelle (admin). Plancher a 0."""
    if amount < 0 or amount > MAX_ELO_MODIFICATION:
        raise ValueError(f"montant doit etre entre 0 et {MAX_ELO_MODIFICATION}")
    if action == "add":
        return EloResult(old_elo=current_elo, new_elo=current_elo + amount, delta=amount)
    if action == "remove":
        new = max(0, current_elo - amount)
        return EloResult(old_elo=current_elo, new_elo=new, delta=-(current_elo - new))
    raise ValueError(f"action inconnue : {action!r} (attendu 'add' ou 'remove')")


def winrate(wins: int, losses: int) -> float:
    """Pourcentage arrondi a 1 decimale, 0.0 si aucune partie."""
    total = wins + losses
    if total == 0:
        return 0.0
    return round((wins / total) * 100, 1)


# ── V2 : ELO change proportionnel a la moyenne du match ──────────
# Serveur reserve aux joueurs Immortal+ : la baseline est l'Immortal 1.
IMMORTAL_FLOOR_ELO: Final[int] = 2400   # Immortal 1 (HenrikDev tier 24 * 100)
ELO_REFERENCE:      Final[int] = IMMORTAL_FLOOR_ELO
ELO_BASE_GAIN:      Final[int] = 20     # gain attendu a avg = ELO_REFERENCE
ELO_BASE_LOSS:      Final[int] = 10     # loss attendu a avg = ELO_REFERENCE


def compute_team_avg_elo(players: list[dict]) -> int:
    """Moyenne arrondie de l'effective_elo d'une liste de joueurs."""
    if not players:
        return 0
    return round(sum(p.get("elo", 0) for p in players) / len(players))


def compute_match_elo_change(avg_match_elo: int) -> tuple[int, int]:
    """
    Renvoie (gain, loss) proportionnels a l'ELO moyen du match.

    Calibre pour un serveur Immortal+ :
      - avg = 2400 (Immortal 1)  -> (20, 10)
      - avg = 2700 (Immortal 3)  -> (22, 11)
      - avg = 3000 (Radiant)     -> (25, 12)
    """
    if avg_match_elo < 0:
        raise ValueError(f"avg_match_elo doit etre >= 0, recu {avg_match_elo}")
    gain = round(ELO_BASE_GAIN * avg_match_elo / ELO_REFERENCE)
    loss = round(ELO_BASE_LOSS * avg_match_elo / ELO_REFERENCE)
    return max(0, gain), max(0, loss)
