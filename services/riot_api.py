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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

import requests


BASE_URL: Final[str] = "https://api.henrikdev.xyz/valorant"
DEFAULT_TIMEOUT: Final[int] = 10
CACHE_TTL_SECONDS: Final[int] = 3600  # 1h


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


# ── Cache TTL simple ──────────────────────────────────────────────
class _TTLCache:
    def __init__(self, ttl: int) -> None:
        self._ttl   = ttl
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self._ttl:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)

    def clear(self) -> None:
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

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = self.api_key
        return h

    def _get(self, path: str) -> dict[str, Any]:
        cached = self._cache.get(path)
        if cached is not None:
            return cached

        url = f"{BASE_URL}{path}"
        try:
            resp = self.session.get(url, headers=self._headers(), timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            raise RiotApiError(f"Erreur reseau : {e}") from e

        if resp.status_code == 404:
            raise PlayerNotFound(f"Joueur introuvable : {path}")
        if resp.status_code == 429:
            raise RateLimited("HenrikDev a renvoye 429 (rate limited)")
        if resp.status_code >= 400:
            raise RiotApiError(f"HTTP {resp.status_code} : {resp.text[:200]}")

        try:
            data = resp.json()
        except ValueError as e:
            raise RiotApiError(f"Reponse non-JSON : {e}") from e

        if data.get("status") and data["status"] >= 400:
            raise RiotApiError(f"API status {data['status']}")

        self._cache.set(path, data)
        return data

    # ── Endpoints publics ─────────────────────────────────────────
    def get_account(self, name: str, tag: str) -> Account:
        data = self._get(f"/v1/account/{name}/{tag}")
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
        data = self._get(f"/v2/mmr/{region}/{name}/{tag}")
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
        data = self._get(f"/v1/mmr-history/{region}/{name}/{tag}")
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

    def clear_cache(self) -> None:
        self._cache.clear()
