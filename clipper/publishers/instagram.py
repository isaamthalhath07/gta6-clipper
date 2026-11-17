"""Instagram Reels via the Graph API resumable upload (Facebook Login for Business).

Needs an Instagram professional (Business/Creator) account linked to a Facebook Page, an access token
with instagram_basic + instagram_content_publish (+ pages_read_engagement), and the IG user id.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import env
from .base import PostMeta, Publisher, PublishError, PublishResult

GRAPH = "https://graph.facebook.com"


class InstagramPublisher(Publisher):
    name = "instagram"

    def _creds(self) -> tuple[str, str]:
        tok = self.state.get_tokens("instagram")
        return tok.get("access_token") or env("IG_ACCESS_TOKEN"), tok.get("ig_user_id") or env("IG_USER_ID")

    def configured(self) -> bool:
        return all(self._creds())

    async def _publish(self, video: Path, meta: PostMeta) -> PublishResult:
        token, ig_user = self._creds()
        v = self.cfg.api_version
        auth = {"Authorization": f"Bearer {token}"}
        r = await self.http.post(f"{GRAPH}/{v}/{ig_user}/media", headers=auth, data={
            "media_type": "REELS", "upload_type": "resumable", "caption": meta.caption(2200),
            "thumb_offset": str(max(0, meta.cover_ms)),
        })
        j = self.check(r, "instagram create container")
        container = j["id"]
        upload_uri = j.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{v}/{container}"

        size = video.stat().st_size
        r = await self.http.post(upload_uri, content=video.read_bytes(), headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
        })
        up = self.check(r, "instagram upload")
        if up and up.get("success") is False:
            raise PublishError(f"instagram upload: {up}", retryable=True)

        status = ""
        for _ in range(60):
            await asyncio.sleep(5)
            r = await self.http.get(f"{GRAPH}/{v}/{container}", headers=auth, params={"fields": "status_code,status"})
            d = self.check(r, "instagram status")
            status = d.get("status_code", "")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"instagram processing {status}: {d.get('status', '')}")
        if status != "FINISHED":
            raise PublishError(f"instagram processing timed out ({status})", retryable=False)

        r = await self.http.post(f"{GRAPH}/{v}/{ig_user}/media_publish", headers=auth, data={"creation_id": container})
        media_id = self.check(r, "instagram publish").get("id", "")
        url = ""
        if media_id:
            r = await self.http.get(f"{GRAPH}/{v}/{media_id}", headers=auth, params={"fields": "permalink"})
            if r.status_code < 400:
                url = r.json().get("permalink", "")
        return PublishResult(self.name, True, remote_id=media_id, url=url)
