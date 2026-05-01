"""
Client HenrikDev API (Valorant unofficial).

Endpoints utilises :
  - GET /valorant/v1/account/{name}/{tag}
  - GET /valorant/v2/mmr/{region}/{name}/{tag}
  - GET /valorant/v1/mmr-history/{region}/{name}/{tag}

Doc: https://docs.henrikdev.xyz/valorant.html

Sans cle API : ~30 req/min. Avec cle (env HENRIK_API_KEY) : plus eleve.
On cache les reponses 1h pour limiter les appels.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final
from urllib.parse import quote

import requests


BASE_URL: Final[str] = "https://api.henrikdev.xyz/valorant"
DEFAULT_TIMEOUT: Final[int] = 10
CACHE_TTL_SECONDS: Final[int] = 3600  # 1h
RETRY_ATTEMPTS:    Final[int] = 3      # 1 essai initial + 2 retries
RETRY_BACKOFF_BASE: Final[float] = 1.0  # delais : 1s, 2s, 4s


VALID_REGIONS: Final[frozenset[str]] = frozenset({"eu", "na", "ap", "kr", "latam", "br"})


class RiotApiError(Exception):
    """Erreur generique du client."""


class PlayerNotFound(RiotApiError):
    """Pseudo#tag inexistant cote Riot."""


class RateLimited(RiotApiError):
    """API a renvoye 429."""


@dataclass(frozen=True)
class Account:
    puuid:  str
    name:   str
    tag:    str
    region: str


@dataclass(frozen=True)
class CurrentMMR:
    elo:                 int
    tier:                int
    tier_name:           str
    ranking_in_tier:     int
    mmr_change_last:     int


@dataclass(frozen=True)
class HistoricalMatch:
    elo:        int
    tier:       int
    date:       datetime
    mmr_change: int


@dataclass(frozen=True)
class MatchPlayerStats:
    puuid:  str
    name:   str
    tag:    str
    team:   str           # "Red" ou "Blue"
    score:  int           # combat score total
    kills:  int
    deaths: int
    assists: int


@dataclass(frozen=True)
class MatchSummary:
    matchid:       str
    mode:          str    # "Custom Game", "Competitive", etc.
    map_name:      str
    started_at:    datetime
    rounds_played: int
    players:       tuple[MatchPlayerStats, ...]
    rounds_red:    int
    rounds_blue:   int


# ── Cache TTL simple ──────────────────────────────────────────────
class _TTLCache:
    """Cache TTL thread-safe : protege _store d'acces concurrents
    depuis plusieurs `asyncio.to_thread`."""

    def __init__(self, ttl: int) -> None:
        self._ttl   = ttl
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock  = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.time() - ts > self._ttl:
                # Pop avec defaut : evite KeyError si un autre thread
                # a deja supprime la cle entre temps.
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.time(), value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# ── Client ────────────────────────────────────────────────────────
class HenrikDevClient:
    def __init__(
        self,
        api_key: str | None = None,
        session: requests.Session | None = None,
        cache_ttl: int = CACHE_TTL_SECONDS,
    ) -> None:
        self.api_key = api_key or os.environ.get("HENRIK_API_KEY")
        self.session = session or requests.Session()
        self._cache  = _TTLCache(cache_ttl)
        # `requests.Session` n'est pas safe pour des appels concurrents
        # multi-thread (le pool de connexions urllib3 peut se corrompre).
        # Le bot exporte plusieurs appels Henrik via `asyncio.to_thread`,
        # donc on serialise les requetes via ce lock. Impact perf
        # negligeable (volume Henrik < 1 req/sec sur ce bot).
        self._session_lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = self.api_key
        return h

    def _get(self, path: str, *, cache: bool = True) -> dict[str, Any]:
        """GET HenrikDev. Si `cache=False`, ne lit ni n'ecrit dans le cache TTL.

        Utile pour les endpoints qui doivent rester frais (polling de match
        history pour detecter un custom recent : avec cache 1h, le 1er retry
        renvoie pour toujours la reponse stale 'pas encore indexe')."""
        if cache:
            cached = self._cache.get(path)
            if cached is not None:
                return cached

        url = f"{BASE_URL}{path}"
        last_err: Exception | None = None
        # Retry uniquement sur erreurs reseau et 5xx (transitoires).
        # 404, 429, 4xx autres : pas de retry (echec deterministe).
        for attempt in range(RETRY_ATTEMPTS):
            try:
                with self._session_lock:
                    resp = self.session.get(url, headers=self._headers(), timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as e:
                last_err = e
                if attempt < RETRY_ATTEMPTS - 1:
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise RiotApiError(f"Erreur reseau apres {RETRY_ATTEMPTS} tentatives : {e}") from e

            if resp.status_code == 404:
                raise PlayerNotFound(f"Joueur introuvable : {path}")
            if resp.status_code == 429:
                raise RateLimited("HenrikDev a renvoye 429 (rate limited)")
            if 500 <= resp.status_code < 600:
                last_err = RiotApiError(f"HTTP {resp.status_code} : {resp.text[:200]}")
                if attempt < RETRY_ATTEMPTS - 1:
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise last_err
            if resp.status_code >= 400:
                raise RiotApiError(f"HTTP {resp.status_code} : {resp.text[:200]}")

            try:
                data = resp.json()
            except ValueError as e:
                raise RiotApiError(f"Reponse non-JSON : {e}") from e

            if data.get("status") and data["status"] >= 400:
                # Si HenrikDev renvoie un status applicatif 5xx, on retry aussi.
                if 500 <= int(data["status"]) < 600 and attempt < RETRY_ATTEMPTS - 1:
                    last_err = RiotApiError(f"API status {data['status']}")
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise RiotApiError(f"API status {data['status']}")

            if cache:
                self._cache.set(path, data)
            return data

        # Non-atteignable normalement, mais garde-fou.
        raise RiotApiError(
            f"_get : echec apres {RETRY_ATTEMPTS} tentatives. last_err={last_err}",
        )

    # ── Endpoints publics ─────────────────────────────────────────
    def get_account(self, name: str, tag: str) -> Account:
        data = self._get(f"/v1/account/{quote(name, safe='')}/{quote(tag, safe='')}")
        d = data.get("data", {})
        return Account(
            puuid=d.get("puuid", ""),
            name=d.get("name", name),
            tag=d.get("tag", tag),
            region=d.get("region", "eu"),
        )

    def get_current_mmr(self, region: str, name: str, tag: str) -> CurrentMMR:
        if region not in VALID_REGIONS:
            raise ValueError(f"Region invalide : {region}")
        data = self._get(f"/v2/mmr/{region}/{quote(name, safe='')}/{quote(tag, safe='')}")
        c = data.get("data", {}).get("current_data", {})
        return CurrentMMR(
            elo=int(c.get("elo") or 0),
            tier=int(c.get("currenttier") or 0),
            tier_name=str(c.get("currenttierpatched") or "Unrated"),
            ranking_in_tier=int(c.get("ranking_in_tier") or 0),
            mmr_change_last=int(c.get("mmr_change_to_last_game") or 0),
        )

    def get_mmr_history(
        self, region: str, name: str, tag: str,
    ) -> list[HistoricalMatch]:
        if region not in VALID_REGIONS:
            raise ValueError(f"Region invalide : {region}")
        data = self._get(f"/v1/mmr-history/{region}/{quote(name, safe='')}/{quote(tag, safe='')}")
        out: list[HistoricalMatch] = []
        for entry in data.get("data", []):
            ts = entry.get("date_raw")
            if ts is None:
                continue
            out.append(HistoricalMatch(
                elo=int(entry.get("elo") or 0),
                tier=int(entry.get("currenttier") or 0),
                date=datetime.fromtimestamp(int(ts), tz=timezone.utc),
                mmr_change=int(entry.get("mmr_change_to_last_game") or 0),
            ))
        return out

    def get_match_history(
        self,
        region: str,
        name: str,
        tag: str,
        *,
        size: int = 5,
        mode: str | None = None,
    ) -> list[MatchSummary]:
        """Recupere les matchs recents d'un joueur. `mode` filtre cote API ('custom', etc.)."""
        if region not in VALID_REGIONS:
            raise ValueError(f"Region invalide : {region}")
        safe_name = quote(name, safe="")
        safe_tag = quote(tag, safe="")
        path = f"/v3/matches/{region}/{safe_name}/{safe_tag}?size={int(size)}"
        if mode:
            path += f"&filter={quote(str(mode), safe='')}"
        # Pas de cache : cet endpoint est appele en boucle pour detecter
        # l'apparition d'un custom recent. Avec le TTL de 1h, le 1er retry
        # renverrait pour toujours le stale "pas encore indexe".
        data = self._get(path, cache=False)
        return [_parse_match(entry) for entry in data.get("data", [])]

    def get_match_details(self, matchid: str) -> MatchSummary:
        """Detail complet d'un match a partir de son id."""
        data = self._get(f"/v2/match/{quote(matchid, safe='')}")
        d = data.get("data", {})
        if not d:
            raise RiotApiError(f"Match {matchid} : payload vide")
        return _parse_match(d)

    def clear_cache(self) -> None:
        self._cache.clear()


def _parse_match(entry: dict) -> MatchSummary:
    meta    = entry.get("metadata", {}) or {}
    teams   = entry.get("teams", {}) or {}
    players = (entry.get("players", {}) or {}).get("all_players", []) or []

    started_raw = meta.get("game_start") or 0
    started_at  = datetime.fromtimestamp(int(started_raw), tz=timezone.utc)

    parsed_players: list[MatchPlayerStats] = []
    for p in players:
        stats = p.get("stats", {}) or {}
        parsed_players.append(MatchPlayerStats(
            puuid=p.get("puuid", ""),
            name=p.get("name", ""),
            tag=p.get("tag", ""),
            team=str(p.get("team", "")),
            score=int(stats.get("score") or 0),
            kills=int(stats.get("kills") or 0),
            deaths=int(stats.get("deaths") or 0),
            assists=int(stats.get("assists") or 0),
        ))

    return MatchSummary(
        matchid=str(meta.get("matchid", "")),
        mode=str(meta.get("mode", "")),
        map_name=str(meta.get("map", "")),
        started_at=started_at,
        rounds_played=int(meta.get("rounds_played") or 0),
        players=tuple(parsed_players),
        rounds_red=int((teams.get("red")  or {}).get("rounds_won") or 0),
        rounds_blue=int((teams.get("blue") or {}).get("rounds_won") or 0),
    )
