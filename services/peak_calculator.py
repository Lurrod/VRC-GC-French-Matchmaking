"""
Calcul de l'elo effectif d'un joueur (logique pure).

Regle utilisateur :
  - Si le PEAK elo (max sur tout l'historique fourni) date de moins de 6 mois
    -> on prend ce peak comme elo effectif.
  - Sinon (peak vieux de plus de 6 mois) -> on prend la moyenne d'elo
    sur les 6 derniers mois.
  - Si aucun match dans les 6 derniers mois -> on retombe sur le peak
    historique (seul indicateur disponible).
  - Historique vide -> 0 (joueur unrated).
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
    source: str   # "peak_recent" | "average_6m" | "peak_fallback" | "empty"
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
    Applique la regle 6 mois.

    Args:
        history:  iterable de MatchEntry (n'importe quel ordre)
        now:      pour les tests (defaut : datetime.now(UTC))
        fallback: elo retourne si l'historique est vide

    Returns:
        EffectiveEloResult avec l'elo et l'explication de la source.
    """
    history = list(history)
    if now is None:
        now = datetime.now(timezone.utc)

    if not history:
        return EffectiveEloResult(elo=fallback, source="empty", peak=0, peak_age_days=None)

    # Peak = max sur tout l'historique fourni
    peak_entry = max(history, key=lambda e: e.elo)
    peak_age   = now - peak_entry.date
    peak_age_days = peak_age.days

    # Cas 1 : peak recent -> on le prend
    if peak_age <= SIX_MONTHS:
        return EffectiveEloResult(
            elo=peak_entry.elo,
            source="peak_recent",
            peak=peak_entry.elo,
            peak_age_days=peak_age_days,
        )

    # Cas 2 : peak vieux -> moyenne sur les 6 derniers mois
    six_months_ago = now - SIX_MONTHS
    recent = [e for e in history if e.date >= six_months_ago]

    if not recent:
        # Cas 3 : aucun match recent -> peak en fallback
        return EffectiveEloResult(
            elo=peak_entry.elo,
            source="peak_fallback",
            peak=peak_entry.elo,
            peak_age_days=peak_age_days,
        )

    avg = round(sum(e.elo for e in recent) / len(recent))
    return EffectiveEloResult(
        elo=int(avg),
        source="average_6m",
        peak=peak_entry.elo,
        peak_age_days=peak_age_days,
    )
