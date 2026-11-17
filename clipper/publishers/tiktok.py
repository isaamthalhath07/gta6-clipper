"""TikTok Content Posting API - Direct Post with FILE_UPLOAD (scope video.publish).

Unaudited apps can only post SELF_ONLY (private) and to private accounts; pass TikTok's app audit
to post publicly. Access tokens last 24h; refresh tokens rotate and are persisted after each refresh.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ..config import env
from .base import PostMeta, Publisher, PublishError, PublishResult

API = "https://open.tiktokapis.com/v2"
MB = 1024 * 1024


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok's media transfer rules: chunks 5-64 MB,
    count = floor(size / chunk_size), trailing bytes go into the last chunk (max 128 MB);
    files under 5 MB (or up to 64 MB) go as a single chunk."""
    if size <= 64 * MB:
        return size, 1
    chunk = 10 * MB
    return chunk, size // chunk


class TikTokPublisher(Publisher):
    name = "tiktok"

    def _creds(self) -> tuple[str, str, str]:
        tok = self.state.get_tokens("tiktok")
        return env("TIKTOK_CLIENT_KEY"), env("TIKTOK_CLIENT_SECRET"), tok.get("refresh_token") or env("TIKTOK_REFRESH_TOKEN")

    def configured(self) -> bool:
        return all(self._creds())

    async def _access_token(self) -> str:
        tok = self.state.get_tokens("tiktok")
        if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 300:
            return tok["access_token"]
        key, secret, refresh = self._creds()
        r = await self.http.post(
            f"{API}/oauth/token/", headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"client_key": key, "client_secret": secret, "grant_type": "refresh_token", "refresh_token": refresh},
        )
        j = self.check(r, "tiktok token refresh")
        if "access_token" not in j:
            raise PublishError(f"tiktok token refresh: {j}")
        self.state.set_tokens("tiktok", {
            "access_token": j["access_token"], "expires_at": time.time() + int(j.get("expires_in", 86400)),
            "refresh_token": j.get("refresh_token", refresh), "open_id": j.get("open_id", ""),
        })
        return j["access_token"]

    def _hdr(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}

    @staticmethod
    def _api_ok(j: dict, what: str) -> dict:
        err = j.get("error", {})
        code = err.get("code", "ok")
        if code != "ok":
            retry = code in ("rate_limit_exceeded", "internal_error")
            raise PublishError(f"{what}: {code} {err.get('message', '')}", retryable=retry)
        return j.get("data", {})

    async def _publish(self, video: Path, meta: PostMeta) -> PublishResult:
        token = await self._access_token()
        # creator_info is mandatory before posting: it lists the privacy levels this account allows.
        r = await self.http.post(f"{API}/post/publish/creator_info/query/", headers=self._hdr(token))
        info = self._api_ok(self.check(r, "tiktok creator_info"), "tiktok creator_info")
        options = info.get("privacy_level_options") or ["SELF_ONLY"]
        privacy = self.cfg.privacy_level if self.cfg.privacy_level in options else (
            "SELF_ONLY" if "SELF_ONLY" in options else options[0]
        )
        size = video.stat().st_size
        chunk, count = chunk_plan(size)
        body = {
            "post_info": {
                "title": meta.caption(2200), "privacy_level": privacy,
                "disable_duet": self.cfg.disable_duet or bool(info.get("duet_disabled")),
                "disable_comment": self.cfg.disable_comment or bool(info.get("comment_disabled")),
                "disable_stitch": self.cfg.disable_stitch or bool(info.get("stitch_disabled")),
                "video_cover_timestamp_ms": max(0, meta.cover_ms),
            },
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
        }
        r = await self.http.post(f"{API}/post/publish/video/init/", headers=self._hdr(token), json=body)
        data = self._api_ok(self.check(r, "tiktok init"), "tiktok init")
        publish_id, upload_url = data["publish_id"], data["upload_url"]

        with video.open("rb") as f:
            for i in range(count):
                first = i * chunk
                last = size - 1 if i == count - 1 else first + chunk - 1
                f.seek(first)
                blob = f.read(last - first + 1)
                r = await self.http.put(upload_url, content=blob, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(blob)),
                    "Content-Range": f"bytes {first}-{last}/{size}",
                })
                if r.status_code not in (200, 201, 206):
                    raise PublishError(f"tiktok chunk {i + 1}/{count}: HTTP {r.status_code} {r.text[:300]}",
                                       retryable=r.status_code >= 500)

        status, post_id = "", ""
        for _ in range(60):  # TikTok processes asynchronously
            await asyncio.sleep(5)
            r = await self.http.post(f"{API}/post/publish/status/fetch/", headers=self._hdr(token), json={"publish_id": publish_id})
            d = self._api_ok(self.check(r, "tiktok status"), "tiktok status")
            status = d.get("status", "")
            if status == "PUBLISH_COMPLETE":
                ids = d.get("publicaly_available_post_id") or d.get("publicly_available_post_id") or []
                post_id = str(ids[0]) if ids else ""
                break
            if status == "FAILED":
                raise PublishError(f"tiktok processing failed: {d.get('fail_reason', 'unknown')}")
        if status != "PUBLISH_COMPLETE":
            return PublishResult(self.name, True, remote_id=publish_id, error=f"still processing ({status})")
        url = f"https://m.tiktok.com/v/{post_id}.html" if post_id else ""
        note = "" if privacy == "PUBLIC_TO_EVERYONE" else f"posted as {privacy}"
        return PublishResult(self.name, True, remote_id=post_id or publish_id, url=url, error=note)
