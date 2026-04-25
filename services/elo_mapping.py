"""
Mapping rang Valorant <-> valeur numerique.

HenrikDev renvoie un `currenttier` numerique (0..27) :
  - 0..2   : Iron 1..3
  - 3..5   : Bronze 1..3
  - ...
  - 24     : Immortal 1
  - 25     : Immortal 2
  - 26     : Immortal 3
  - 27     : Radiant

L'`elo` HenrikDev (`current_data.elo`) = currenttier * 100 + ranking_in_tier
On utilise cet elo numerique pour les comparaisons et la moyenne.
"""

from __future__ import annotations
from typing import Final


TIER_NAMES: Final[tuple[str, ...]] = (
    "Unrated",
    "Iron 1", "Iron 2", "Iron 3",
    "Bronze 1", "Bronze 2", "Bronze 3",
    "Silver 1", "Silver 2", "Silver 3",
    "Gold 1", "Gold 2", "Gold 3",
    "Platinum 1", "Platinum 2", "Platinum 3",
    "Diamond 1", "Diamond 2", "Diamond 3",
    "Ascendant 1", "Ascendant 2", "Ascendant 3",
    "Immortal 1", "Immortal 2", "Immortal 3",
    "Radiant",
)


def tier_to_name(tier: int) -> str:
    """Convertit un currenttier numerique HenrikDev en nom lisible."""
    if 0 <= tier < len(TIER_NAMES):
        return TIER_NAMES[tier]
    return "Unknown"


def elo_to_tier_name(elo: int) -> str:
    """Convertit un elo numerique en nom de tier approximatif."""
    if elo <= 0:
        return "Unrated"
    tier = elo // 100
    return tier_to_name(min(tier, len(TIER_NAMES) - 1))
