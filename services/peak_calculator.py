"""
Calcul de l'elo effectif d'un joueur (logique pure).

Regle utilisateur :
  - On filtre l'historique aux 6 derniers mois.
  - L'elo effectif = peak (max) parmi ces matches recents.
  - Si aucun match dans les 6 derniers mois -> fallback (MMR courant).
  - Historique vide -> fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


SIX_MONTHS = timedelta(days=180)


@dataclass(frozen=True)
class MatchEntry:
    """Une entree d'historique : elo a la fin du match + date du match."""
    elo:  int
    date: datetime


@dataclass(frozen=True)
class EffectiveEloResult:
    elo:    int
    source: str   # "peak_6m" | "no_recent_history" | "empty"
    peak:   int
    peak_age_days: int | None


def parse_riot_id(riot_id: str) -> tuple[str, str]:
    """
    Parse "Pseudo#TAG" -> ("Pseudo", "TAG"). Tolere les espaces dans le pseudo.

    Raises:
        ValueError si le format est invalide.
    """
    if not isinstance(riot_id, str) or "#" not in riot_id:
        raise ValueError("Format invalide. Attendu : Pseudo#TAG")
    name, _, tag = riot_id.rpartition("#")
    name = name.strip()
    tag  = tag.strip()
    if not name or not tag:
        raise ValueError("Format invalide. Attendu : Pseudo#TAG")
    if len(tag) > 5 or len(name) > 16:
        raise ValueError("Pseudo trop long (max 16) ou tag trop long (max 5)")
    return name, tag


def compute_effective_elo(
    history:  Iterable[MatchEntry],
    *,
    now:      datetime | None = None,
    fallback: int = 0,
) -> EffectiveEloResult:
    """
    Renvoie le peak ELO sur les 6 derniers mois (ou fallback sinon).

    Args:
        history:  iterable de MatchEntry (n'importe quel ordre)
        now:      pour les tests (defaut : datetime.now(UTC))
        fallback: elo retourne si aucun match recent

    Returns:
        EffectiveEloResult avec l'elo (= peak des 6 derniers mois) et la source.
    """
    history = list(history)
    if now is None:
        now = datetime.now(timezone.utc)

    if not history:
        return EffectiveEloResult(elo=fallback, source="empty", peak=0, peak_age_days=None)

    six_months_ago = now - SIX_MONTHS
    recent = [e for e in history if e.date >= six_months_ago]

    if not recent:
        return EffectiveEloResult(
            elo=fallback,
            source="no_recent_history",
            peak=0,
            peak_age_days=None,
        )

    peak_entry = max(recent, key=lambda e: e.elo)
    return EffectiveEloResult(
        elo=peak_entry.elo,
        source="peak_6m",
        peak=peak_entry.elo,
        peak_age_days=(now - peak_entry.date).days,
    )
