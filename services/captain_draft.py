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

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass, replace
from typing import Any, Literal, Sequence

from services.team_balancer import Player

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class DraftResult:
    cap_a:  Player
    cap_b:  Player
    team_a: tuple[Player, ...]   # 5 joueurs incl. cap_a
    team_b: tuple[Player, ...]   # 5 joueurs incl. cap_b

    @classmethod
    def from_state(cls, state: DraftState) -> "DraftResult":
        if state.status != "complete":
            raise ValueError(f"Draft non termine (status={state.status}).")
        return cls(
            cap_a=state.cap_a,
            cap_b=state.cap_b,
            team_a=state.team_a,
            team_b=state.team_b,
        )


class DraftCancelledError(Exception):
    """Leve quand un admin annule le draft via le bouton."""

    def __init__(self, reason: str, actor: Any | None = None):
        super().__init__(reason)
        self.reason = reason
        self.actor = actor


def _has_any_role(user: Any, role_names: tuple[str, ...]) -> bool:
    return any(r.name in role_names for r in getattr(user, "roles", []))


def _build_player_lines(players: tuple[Player, ...]) -> str:
    if not players:
        return "_(vide)_"
    return "\n".join(f"• <@{p.id}> ({p.elo})" for p in players)


def _build_pool_lines(pool: tuple[Player, ...]) -> str:
    if not pool:
        return "_(vide)_"
    ordered = sorted(pool, key=lambda p: p.elo, reverse=True)
    return "\n".join(f"• <@{p.id}> ({p.elo})" for p in ordered)


def _build_sequence_marker(turn_index: int) -> str:
    """Affiche la sequence ABBAABBA avec un curseur sur le pick courant."""
    parts = []
    for i, side in enumerate(PICK_SEQUENCE):
        if i == turn_index:
            parts.append(f"·{side}·")
        else:
            parts.append(side)
    return " ".join(parts)


class CaptainDraftSession:
    """Orchestration du draft : poste le message, gere les interactions,
    retourne un DraftResult quand les 8 picks sont termines (ou leve
    DraftCancelledError si annule par un admin).
    """

    def __init__(
        self,
        *,
        prep_channel: Any,
        cap_a: Player,
        cap_b: Player,
        pool: tuple[Player, ...],
        admin_role_names: tuple[str, ...],
    ):
        self.prep_channel = prep_channel
        self.state = DraftState.initial(cap_a=cap_a, cap_b=cap_b, pool=pool)
        self.admin_role_names = admin_role_names
        self.message: Any | None = None
        self._lock = asyncio.Lock()
        # _done est cree paresseusement dans run() pour eviter
        # asyncio.get_event_loop() hors d'une coroutine (deprecation Python 3.12+).
        self._done: asyncio.Future[DraftResult] | None = None

    async def run(self) -> DraftResult:
        """Bloque jusqu'a la fin du draft (complete OU cancelled).

        Returns: DraftResult si complete.
        Raises: DraftCancelledError si annule.
        """
        loop = asyncio.get_running_loop()
        self._done = loop.create_future()

        embed = self._build_embed()
        view = self._build_view()
        content = (
            f"<@{self.state.cap_a.id}> <@{self.state.cap_b.id}> "
            f"— vous etes capitaines, a vous de drafter !"
        )
        self.message = await self.prep_channel.send(content=content, embed=embed, view=view)
        logger.info(
            "[draft] init cap_a=%s cap_b=%s pool_size=%d",
            self.state.cap_a.id, self.state.cap_b.id, len(self.state.pool),
        )
        return await self._done

    def _build_embed(self) -> Any:
        import discord
        e = discord.Embed(
            title="🎯 [PRO] Captain Draft",
            color=discord.Color.gold(),
        )
        e.add_field(
            name=f"🅰️ Team 1 — Cap. <@{self.state.cap_a.id}>",
            value=_build_player_lines(self.state.team_a),
            inline=False,
        )
        e.add_field(
            name=f"🅱️ Team 2 — Cap. <@{self.state.cap_b.id}>",
            value=_build_player_lines(self.state.team_b),
            inline=False,
        )
        e.add_field(
            name="🎲 Pool disponible (tri ELO ↓)",
            value=_build_pool_lines(self.state.pool),
            inline=False,
        )
        if self.state.is_complete:
            e.set_footer(text="✅ Draft termine")
        elif self.state.status == "picking":
            cur = self.state.current_captain
            seq = _build_sequence_marker(self.state.turn_index)
            e.add_field(
                name=f"⏳ Au tour de <@{cur.id}> — pick #{self.state.turn_index + 1}",
                value=f"Sequence : {seq}",
                inline=False,
            )
        return e

    def _build_view(self) -> Any:
        import discord

        session = self  # capture pour les callbacks

        class _View(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=None)

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                return await session._interaction_check(interaction)

        view = _View()
        if not self.state.is_complete and self.state.status == "picking":
            options = [
                discord.SelectOption(
                    label=p.name[:100],
                    description=f"{p.elo} ELO",
                    value=str(p.id),
                )
                for p in sorted(self.state.pool, key=lambda x: x.elo, reverse=True)
            ]
            select = discord.ui.Select(
                custom_id="pro_draft_pick",
                placeholder="Choisis ton joueur",
                min_values=1, max_values=1,
                options=options,
            )

            async def _select_cb(interaction: discord.Interaction) -> None:
                await session._on_pick(interaction)

            select.callback = _select_cb
            view.add_item(select)

        cancel_btn = discord.ui.Button(
            custom_id="pro_draft_cancel",
            style=discord.ButtonStyle.danger,
            label="❌ Annuler le draft",
            disabled=self.state.status != "picking",
        )

        async def _cancel_cb(interaction: discord.Interaction) -> None:
            await session._on_cancel(interaction)

        cancel_btn.callback = _cancel_cb
        view.add_item(cancel_btn)
        return view

    async def _interaction_check(self, interaction: Any) -> bool:
        cid = interaction.data.get("custom_id", "")
        if cid == "pro_draft_pick":
            # Guard : si le draft est complet (interaction tardive cote Discord),
            # on rejette proprement plutot que de laisser current_captain
            # lever un RuntimeError.
            if self.state.is_complete or interaction.user.id != self.state.current_captain.id:
                await interaction.response.send_message(
                    "⏳ Ce n'est pas ton tour.", ephemeral=True,
                )
                return False
        elif cid == "pro_draft_cancel":
            if not _has_any_role(interaction.user, self.admin_role_names):
                await interaction.response.send_message(
                    "❌ Reserve aux admins.", ephemeral=True,
                )
                return False
        return True

    async def _on_pick(self, interaction: Any) -> None:
        async with self._lock:
            if self.state.status != "picking":
                return
            picked_id_str = interaction.data["values"][0]
            picked_id = int(picked_id_str)
            picked = next(
                (p for p in self.state.pool if p.id == picked_id),
                None,
            )
            if picked is None:
                await interaction.response.send_message(
                    "❌ Joueur deja drafte.", ephemeral=True,
                )
                return
            self.state = self.state.apply_pick(picked)
            logger.info(
                "[draft] pick turn=%d by=%s player=%s",
                self.state.turn_index - 1, interaction.user.id, picked_id,
            )
            embed = self._build_embed()
            view = self._build_view()
            await self.message.edit(embed=embed, view=view)
            if self.state.is_complete and self._done is not None and not self._done.done():
                self._done.set_result(DraftResult.from_state(self.state))
            with contextlib.suppress(Exception):
                await interaction.response.defer()

    async def _on_cancel(self, interaction: Any) -> None:
        async with self._lock:
            if self.state.status != "picking":
                return
            self.state = replace(self.state, status="cancelled")
            actor = interaction.user
            embed = self._build_embed()
            embed.title = "❌ Draft annule"
            embed.description = f"Annule par <@{actor.id}>"
            view = self._build_view()
            await self.message.edit(embed=embed, view=view)
            logger.info("[draft] cancelled by=%s", actor.id)
            with contextlib.suppress(Exception):
                await interaction.response.defer()
            if self._done is not None and not self._done.done():
                self._done.set_exception(DraftCancelledError("admin", actor))
