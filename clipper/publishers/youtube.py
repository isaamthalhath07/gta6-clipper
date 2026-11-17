"""YouTube Shorts via Data API v3 resumable upload (OAuth refresh token, scope youtube.upload)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import env
from .base import PostMeta, Publisher, PublishError, PublishResult

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class YouTubePublisher(Publisher):
    name = "youtube"

    def _creds(self) -> tuple[str, str, str]:
        tok = self.state.get_tokens("youtube")
        return env("YT_CLIENT_ID"), env("YT_CLIENT_SECRET"), tok.get("refresh_token") or env("YT_REFRESH_TOKEN")

    def configured(self) -> bool:
        return all(self._creds())

    async def _access_token(self) -> str:
        tok = self.state.get_tokens("youtube")
        if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 120:
            return tok["access_token"]
        cid, secret, refresh = self._creds()
        r = await self.http.post(TOKEN_URL, data={
            "client_id": cid, "client_secret": secret, "refresh_token": refresh, "grant_type": "refresh_token",
        })
        j = self.check(r, "youtube token refresh")
        self.state.set_tokens("youtube", {"access_token": j["access_token"], "expires_at": time.time() + j.get("expires_in", 3600)})
        return j["access_token"]

    async def _publish(self, video: Path, meta: PostMeta) -> PublishResult:
        token = await self._access_token()
        size = video.stat().st_size
        title = meta.title.replace("<", "").replace(">", "")[:100]
        tags = [h.lstrip("#") for h in meta.hashtags][:15]
        desc = f"{meta.description}\n\n{' '.join(meta.hashtags)} #Shorts"[:4900]
        body = {
            "snippet": {"title": title, "description": desc, "tags": tags, "categoryId": self.cfg.category_id},
            "status": {"privacyStatus": self.cfg.privacy, "selfDeclaredMadeForKids": False},
        }
        r = await self.http.post(
            UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4",
            },
            content=json.dumps(body),
        )
        self.check(r, "youtube init")
        location = r.headers.get("Location")
        if not location:
            raise PublishError("youtube init: no upload URL returned", retryable=True)
        r = await self.http.put(
            location, headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4", "Content-Length": str(size)},
            content=video.read_bytes(),
        )
        j = self.check(r, "youtube upload")
        vid = j.get("id", "")
        if not vid:
            raise PublishError(f"youtube upload: unexpected response {str(j)[:300]}", retryable=True)
        return PublishResult(self.name, True, remote_id=vid, url=f"https://youtube.com/shorts/{vid}")
