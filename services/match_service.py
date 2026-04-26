"""
Logique pure de formation de match (testable sans Discord).

Responsabilites :
  - Construire la liste des Player a partir des IDs en queue
    et des comptes Riot lies (effective_elo).
  - Trouver une categorie 'Match #N' libre.
  - Selectionner map et lobby leader aleatoires.
  - Renvoyer un MatchPlan complet pret a etre poste sur Discord.

Le cog cogs/match.py s'occupe ensuite des side effects (envoi du message,
attache de la VoteView, persistance).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, asdict
from typing import Iterable, Sequence

from services import elo_calc
from services.team_balancer import Player, BalancedTeams, balance_teams


@dataclass(frozen=True)
class MatchPlan:
    teams:           BalancedTeams
    map_name:        str
    lobby_leader:    Player
    category_name:   str | None  # None si aucune categorie libre


def build_players(
    player_ids:     Sequence[str],
    riot_accounts:  dict[str, dict],
    member_names:   dict[str, str],
    bot_elos:       dict[str, int] | None = None,
) -> list[Player]:
    """
    Construit les Player en croisant queue + Riot + ELO serveur + display_names.

    Args:
        player_ids:    IDs Discord (str) en queue
        riot_accounts: dict[user_id_str -> doc Riot] (gate-keep uniquement)
        member_names:  dict[user_id_str -> display_name]
        bot_elos:      dict[user_id_str -> ELO serveur (elo_<guild>.elo)].
                       Source de verite pour le matchmaking.

    Joueur sans compte Riot lie -> ignore (queue rejettera < 10).
    L'ELO utilisee pour le balancing est `bot_elos[uid]` (ELO serveur
    seedee au /link-riot et mise a jour apres chaque match valide).
    """
    bot_elos = bot_elos or {}
    out: list[Player] = []
    for uid in player_ids:
        riot = riot_accounts.get(uid)
        if riot is None:
            continue
        name = member_names.get(uid, riot.get("riot_name", "Unknown"))
        out.append(Player(
            id=int(uid),
            name=name,
            elo=int(bot_elos.get(uid, 0)),
        ))
    return out


def plan_match(
    players:       Sequence[Player],
    *,
    free_category: str | None,
    rng:           random.Random | None = None,
) -> MatchPlan:
    """
    Etape pure : equilibre + map + lobby leader.

    Args:
        players:       exactement 10 joueurs avec effective_elo
        free_category: nom de la categorie 'Match #N' libre (None si aucune)
        rng:           random source (injectable pour les tests)
    """
    if len(players) != 10:
        raise ValueError(f"Il faut 10 joueurs, recu {len(players)}")

    rng = rng or random.Random()
    teams        = balance_teams(players)
    map_name     = rng.choice(elo_calc.MAPS)
    lobby_leader = rng.choice(players)
    return MatchPlan(
        teams=teams,
        map_name=map_name,
        lobby_leader=lobby_leader,
        category_name=free_category,
    )


def serialize_team(team: tuple[Player, ...]) -> list[dict]:
    """Pour stockage MongoDB."""
    return [asdict(p) for p in team]


def find_free_match_category(guild) -> str | None:
    """
    Cherche une categorie 'Match #1/2/3' dont les VCs Team 1 / Team 2 sont vides.
    Renvoie le nom de la categorie ou None si aucune libre.
    """
    free = find_free_match_prep(guild)
    return free[0] if free else None


def find_free_match_prep(guild) -> tuple[str, object] | None:
    """
    Comme find_free_match_category, mais renvoie aussi le salon
    'match-preparation' contenu dans la categorie libre.

    Returns:
        (cat_name, prep_text_channel) ou None si aucune categorie libre
        avec un salon 'match-preparation' configure.
    """
    import discord  # import local pour ne pas alourdir l'import du module
    for i in range(1, 4):
        cat_name = f"Match #{i}"
        category = discord.utils.get(guild.categories, name=cat_name)
        if category is None:
            continue
        team1 = discord.utils.get(category.voice_channels, name="Team 1")
        team2 = discord.utils.get(category.voice_channels, name="Team 2")
        team1_empty = (team1 is None) or (len(team1.members) == 0)
        team2_empty = (team2 is None) or (len(team2.members) == 0)
        if not (team1_empty and team2_empty):
            continue
        prep = discord.utils.get(category.text_channels, name="match-preparation")
        if prep is None:
            continue
        return (cat_name, prep)
    return None
