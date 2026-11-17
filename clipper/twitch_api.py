"""Twitch Helix: find live GTA VI streams (app access token, client-credentials flow)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

HELIX = "https://api.twitch.tv/helix"


@dataclass
class LiveStream:
    login: str
    display_name: str
    title: str
    viewers: int
    game_id: str
    game_name: str
    language: str


class Helix:
    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise RuntimeError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required (dev.twitch.tv/console)")
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = ""
        self._token_exp = 0.0
        self.http = httpx.AsyncClient(timeout=20)
        self._game_ids: dict[str, str] = {}

    async def _auth(self) -> dict:
        if time.time() > self._token_exp - 300:
            r = await self.http.post(
                "https://id.twitch.tv/oauth2/token",
                data={"client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials"},
            )
            r.raise_for_status()
            j = r.json()
            self._token = j["access_token"]
            self._token_exp = time.time() + j.get("expires_in", 3600)
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {self._token}"}

    async def _get(self, path: str, params) -> dict:
        r = await self.http.get(f"{HELIX}{path}", params=params, headers=await self._auth())
        if r.status_code == 401:
            self._token_exp = 0
            r = await self.http.get(f"{HELIX}{path}", params=params, headers=await self._auth())
        r.raise_for_status()
        return r.json()

    async def game_id(self, name: str) -> str:
        if name not in self._game_ids:
            data = (await self._get("/games", {"name": name})).get("data", [])
            if not data:
                data = (await self._get("/search/categories", {"query": name, "first": 5})).get("data", [])
                data = [d for d in data if d["name"].lower() == name.lower()] or data
            if not data:
                raise RuntimeError(f"Twitch category not found: {name}")
            self._game_ids[name] = data[0]["id"]
            log.info("Twitch category %r -> game_id %s", data[0]["name"], data[0]["id"])
        return self._game_ids[name]

    @staticmethod
    def _parse(d: dict) -> LiveStream:
        return LiveStream(
            login=d["user_login"].lower(), display_name=d["user_name"], title=d.get("title", ""),
            viewers=d.get("viewer_count", 0), game_id=d.get("game_id", ""), game_name=d.get("game_name", ""),
            language=d.get("language", ""),
        )

    async def top_streams(self, game_id: str, n: int, languages: list[str]) -> list[LiveStream]:
        out: list[LiveStream] = []
        cursor = None
        while len(out) < n:
            params: list[tuple[str, str | int]] = [("game_id", game_id), ("first", 100), ("type", "live")]
            params += [("language", lang) for lang in languages]
            if cursor:
                params.append(("after", cursor))
            j = await self._get("/streams", params)
            out += [self._parse(d) for d in j.get("data", [])]
            cursor = j.get("pagination", {}).get("cursor")
            if not cursor or not j.get("data"):
                break
        out.sort(key=lambda s: -s.viewers)
        return out[:n]

    async def streams_by_login(self, logins: list[str]) -> list[LiveStream]:
        out: list[LiveStream] = []
        for i in range(0, len(logins), 100):
            params = [("user_login", lg) for lg in logins[i : i + 100]]
            j = await self._get("/streams", params)
            out += [self._parse(d) for d in j.get("data", [])]
        return out

    async def close(self) -> None:
        await self.http.aclose()
