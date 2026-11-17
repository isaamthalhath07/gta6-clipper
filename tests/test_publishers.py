"""Exercise each upload flow against a fake API server (request shapes, chunking, polling, token refresh)."""

import asyncio
import json

import httpx
import pytest

from clipper.config import InstagramCfg, TikTokCfg, YouTubeCfg
from clipper.publishers import InstagramPublisher, PostMeta, TikTokPublisher, YouTubePublisher
from clipper.publishers import tiktok as tk
from clipper.state import State

META = PostMeta("Streamer did NOT see this coming", "Live from twitch.tv/streamer", ["#GTA6", "#gaming"], 4200)


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    real = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real(0))


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 1000)
    return p


def run(coro):
    return asyncio.run(coro)


def test_youtube_flow(tmp_path, video, monkeypatch):
    monkeypatch.setenv("YT_CLIENT_ID", "cid")
    monkeypatch.setenv("YT_CLIENT_SECRET", "sec")
    monkeypatch.setenv("YT_REFRESH_TOKEN", "rt")
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.host, req.url.path))
        if req.url.host == "oauth2.googleapis.com":
            assert b"grant_type=refresh_token" in req.content
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if req.method == "POST":
            body = json.loads(req.content)
            assert req.headers["X-Upload-Content-Length"] == "1000"
            assert body["snippet"]["categoryId"] == "20" and "#Shorts" in body["snippet"]["description"]
            return httpx.Response(200, headers={"Location": "https://upload.example/session1"})
        assert req.url.host == "upload.example" and len(req.content) == 1000
        return httpx.Response(200, json={"id": "abc123"})

    pub = YouTubePublisher(State(tmp_path), YouTubeCfg())
    pub.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = run(pub.publish(video, META))
    assert r.ok and r.url == "https://youtube.com/shorts/abc123", r
    assert len(seen) == 3


def test_tiktok_chunked_flow_and_private_fallback(tmp_path, video, monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s")
    monkeypatch.setenv("TIKTOK_REFRESH_TOKEN", "rt0")
    monkeypatch.setattr(tk, "MB", 10)  # shrink limits so a 1000-byte file needs several chunks
    state = State(tmp_path)
    ranges, polls = [], []

    def handler(req: httpx.Request):
        p = req.url.path
        if p == "/v2/oauth/token/":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 86400, "refresh_token": "rt1"})
        if p.endswith("creator_info/query/"):
            return httpx.Response(200, json={"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}})
        if p.endswith("video/init/"):
            b = json.loads(req.content)
            assert b["post_info"]["privacy_level"] == "SELF_ONLY"  # unaudited app: falls back to private
            assert b["post_info"]["video_cover_timestamp_ms"] == 4200
            si = b["source_info"]
            assert si["video_size"] == 1000 and si["total_chunk_count"] == 1000 // si["chunk_size"]
            return httpx.Response(200, json={"data": {"publish_id": "pub1", "upload_url": "https://up.example/u"}, "error": {"code": "ok"}})
        if req.url.host == "up.example":
            ranges.append(req.headers["Content-Range"])
            return httpx.Response(206)
        if p.endswith("status/fetch/"):
            polls.append(1)
            st = "PROCESSING_UPLOAD" if len(polls) < 2 else "PUBLISH_COMPLETE"
            return httpx.Response(200, json={"data": {"status": st, "publicaly_available_post_id": [777]}, "error": {"code": "ok"}})
        raise AssertionError(p)

    pub = TikTokPublisher(state, TikTokCfg())
    pub.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = run(pub.publish(video, META))
    assert r.ok and r.remote_id == "777" and "SELF_ONLY" in r.error
    assert len(ranges) == 10 and ranges[0] == "bytes 0-99/1000" and ranges[-1] == "bytes 900-999/1000"
    assert state.get_tokens("tiktok")["refresh_token"] == "rt1"  # rotated token persisted


def test_instagram_flow(tmp_path, video, monkeypatch):
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("IG_USER_ID", "1789")
    statuses = iter(["IN_PROGRESS", "FINISHED"])

    def handler(req: httpx.Request):
        p = req.url.path
        if p.endswith("/1789/media"):
            assert b"upload_type=resumable" in req.content and b"media_type=REELS" in req.content
            return httpx.Response(200, json={"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v24.0/C1"})
        if req.url.host == "rupload.facebook.com":
            assert req.headers["Authorization"] == "OAuth tok" and req.headers["file_size"] == "1000"
            return httpx.Response(200, json={"success": True, "message": "Upload successful."})
        if p.endswith("/C1"):
            return httpx.Response(200, json={"status_code": next(statuses)})
        if p.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "M9"})
        if p.endswith("/M9"):
            return httpx.Response(200, json={"permalink": "https://instagram.com/reel/xyz"})
        raise AssertionError(p)

    pub = InstagramPublisher(State(tmp_path), InstagramCfg())
    pub.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = run(pub.publish(video, META))
    assert r.ok and r.url == "https://instagram.com/reel/xyz", r


def test_daily_cap_and_non_retryable_errors(tmp_path, video, monkeypatch):
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("IG_USER_ID", "1789")
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad"}})

    state = State(tmp_path)
    pub = InstagramPublisher(state, InstagramCfg(max_posts_per_day=1))
    pub.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = run(pub.publish(video, META))
    assert not r.ok and len(calls) == 1  # 4xx is not retried
    state.add_post("x", "instagram", True)
    r = run(pub.publish(video, META))
    assert not r.ok and "cap" in r.error
