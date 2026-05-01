"""Helpers Riot ID. Logique pure (pas de Discord ni Mongo)."""

from __future__ import annotations


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
