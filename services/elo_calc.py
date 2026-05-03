"""Logique pure de calcul ELO. Aucune dependance Discord ni MongoDB."""

from __future__ import annotations

from typing import Final


# ── Constantes ────────────────────────────────────────────────────
ELO_START: Final[int] = 0
MAPS:      Final[tuple[str, ...]] = (
    "Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven", "Pearl",
)


# ── V2 : ELO change proportionnel a la moyenne du match ──────────
# Serveur reserve aux joueurs Immortal+ : la baseline est l'Immortal 1.
IMMORTAL_FLOOR_ELO: Final[int] = 2400   # Immortal 1 (HenrikDev tier 24 * 100)
ELO_REFERENCE:      Final[int] = IMMORTAL_FLOOR_ELO
# Zero-sum strict : gain == loss. ELO injectee par match = 0.
ELO_BASE_CHANGE:    Final[int] = 16     # gain et loss attendus a avg = ELO_REFERENCE
# Alias retro-compatibles (utilises par tests/code legacy)
ELO_BASE_GAIN:      Final[int] = ELO_BASE_CHANGE
ELO_BASE_LOSS:      Final[int] = ELO_BASE_CHANGE


def compute_team_avg_elo(players: list[dict]) -> int:
    """Moyenne arrondie de l'effective_elo d'une liste de joueurs."""
    if not players:
        return 0
    return round(sum(p.get("elo", 0) for p in players) / len(players))


def compute_match_elo_change(avg_match_elo: int) -> tuple[int, int]:
    """
    Renvoie (gain, loss) zero-sum strict : gain == loss, proportionnels
    a l'ELO moyen du match.

    Calibre pour un serveur Immortal+ :
      - avg = 2400 (Immortal 1)  -> (15, 15)
      - avg = 2700 (Immortal 3)  -> (17, 17)
      - avg = 3000 (Radiant)     -> (19, 19)
    """
    if avg_match_elo < 0:
        raise ValueError(f"avg_match_elo doit etre >= 0, recu {avg_match_elo}")
    change = round(ELO_BASE_CHANGE * avg_match_elo / ELO_REFERENCE)
    # Plancher a 1 pour garantir une progression meme apres reset global
    # (avg=0 produirait sinon (0, 0) → match joue pour rien).
    change = max(1, change)
    return change, change
