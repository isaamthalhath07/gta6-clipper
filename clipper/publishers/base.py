from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


@dataclass
class PostMeta:
    title: str
    description: str
    hashtags: list[str]
    cover_ms: int = 0

    def caption(self, limit: int) -> str:
        text = f"{self.title}\n\n{self.description}\n\n{' '.join(self.hashtags)}".strip()
        return text[:limit]


@dataclass
class PublishResult:
    platform: str
    ok: bool
    remote_id: str = ""
    url: str = ""
    error: str = ""


class PublishError(Exception):
    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


class Publisher:
    name = "base"

    def __init__(self, state, cfg):
        self.state = state
        self.cfg = cfg
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, read=300, write=300))

    def configured(self) -> bool:
        raise NotImplementedError

    async def _publish(self, video: Path, meta: PostMeta) -> PublishResult:
        raise NotImplementedError

    async def publish(self, video: Path, meta: PostMeta, attempts: int = 3) -> PublishResult:
        if self.state.posts_last_24h(self.name) >= self.cfg.max_posts_per_day:
            return PublishResult(self.name, False, error="daily post cap reached")
        delay = 5.0
        for i in range(attempts):
            try:
                return await self._publish(video, meta)
            except PublishError as e:
                if not e.retryable or i == attempts - 1:
                    return PublishResult(self.name, False, error=str(e))
                log.warning("%s: %s (retrying in %.0fs)", self.name, e, delay)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if i == attempts - 1:
                    return PublishResult(self.name, False, error=f"network: {e}")
                log.warning("%s network error: %s (retrying in %.0fs)", self.name, e, delay)
            await asyncio.sleep(delay)
            delay *= 3
        return PublishResult(self.name, False, error="exhausted retries")

    @staticmethod
    def check(r: httpx.Response, what: str) -> dict:
        if r.status_code >= 400:
            retry = r.status_code == 429 or r.status_code >= 500
            raise PublishError(f"{what}: HTTP {r.status_code} {r.text[:500]}", retryable=retry)
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {}

    async def close(self) -> None:
        await self.http.aclose()
