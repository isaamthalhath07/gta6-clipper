"""One-time OAuth helpers: `python -m clipper auth youtube|tiktok|instagram`.

Works on headless servers: open the printed URL anywhere, approve, then paste the URL your browser
was redirected to (it's fine if that page fails to load; the code is in the address bar).
"""

from __future__ import annotations

import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import env
from .state import State


def _code_from(redirected: str, expected_state: str) -> str:
    q = parse_qs(urlparse(redirected.strip()).query)
    if q.get("state", [""])[0] != expected_state:
        raise SystemExit("state mismatch - paste the URL from this login attempt")
    if "code" not in q:
        raise SystemExit(f"no ?code= in that URL: {q.get('error', q)}")
    return q["code"][0]


def youtube(state: State) -> None:
    cid, secret = env("YT_CLIENT_ID"), env("YT_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("Set YT_CLIENT_ID and YT_CLIENT_SECRET (Google Cloud OAuth client, type 'Desktop app').")
    redirect = env("YT_REDIRECT_URI", "http://127.0.0.1:8765/")
    st = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "state": st,
        "scope": "https://www.googleapis.com/auth/youtube.upload", "access_type": "offline", "prompt": "consent",
    })
    print(f"\n1) Open and approve with the YouTube channel you post to:\n\n{url}\n")
    code = _code_from(input("2) Paste the full URL you were redirected to: "), st)
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": cid, "client_secret": secret, "redirect_uri": redirect, "grant_type": "authorization_code",
    })
    r.raise_for_status()
    j = r.json()
    if "refresh_token" not in j:
        raise SystemExit("Google did not return a refresh token; revoke the app's access and retry.")
    state.set_tokens("youtube", {"refresh_token": j["refresh_token"], "access_token": j["access_token"],
                                 "expires_at": time.time() + j.get("expires_in", 3600)})
    print("YouTube connected.")


def tiktok(state: State) -> None:
    key, secret, redirect = env("TIKTOK_CLIENT_KEY"), env("TIKTOK_CLIENT_SECRET"), env("TIKTOK_REDIRECT_URI")
    if not (key and secret and redirect):
        raise SystemExit("Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI (must match the app's Login Kit config).")
    st = secrets.token_urlsafe(16)
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
        "client_key": key, "scope": "user.info.basic,video.publish", "response_type": "code",
        "redirect_uri": redirect, "state": st,
    })
    print(f"\n1) Open and approve with the TikTok account you post to:\n\n{url}\n")
    code = _code_from(input("2) Paste the full URL you were redirected to: "), st)
    r = httpx.post("https://open.tiktokapis.com/v2/oauth/token/", headers={"Content-Type": "application/x-www-form-urlencoded"},
                   data={"client_key": key, "client_secret": secret, "code": code, "grant_type": "authorization_code",
                         "redirect_uri": redirect})
    r.raise_for_status()
    j = r.json()
    if "refresh_token" not in j:
        raise SystemExit(f"TikTok token exchange failed: {j}")
    state.set_tokens("tiktok", {"refresh_token": j["refresh_token"], "access_token": j["access_token"],
                                "expires_at": time.time() + int(j.get("expires_in", 86400)), "open_id": j.get("open_id", "")})
    print("TikTok connected.")


def instagram(state: State, short_token: str, api_version: str) -> None:
    app_id, app_secret = env("META_APP_ID"), env("META_APP_SECRET")
    token = short_token
    if app_id and app_secret:
        r = httpx.get(f"https://graph.facebook.com/{api_version}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret, "fb_exchange_token": short_token,
        })
        r.raise_for_status()
        token = r.json()["access_token"]
        print("Exchanged for a long-lived (~60 day) token.")
    else:
        print("META_APP_ID/META_APP_SECRET not set; storing the token as-is (it may be short-lived).")
    r = httpx.get(f"https://graph.facebook.com/{api_version}/me/accounts",
                  params={"fields": "name,instagram_business_account{id,username}", "access_token": token})
    r.raise_for_status()
    accounts = [(p["name"], p["instagram_business_account"]) for p in r.json().get("data", []) if p.get("instagram_business_account")]
    if not accounts:
        raise SystemExit("No Instagram professional account linked to your Facebook Pages.")
    for i, (page, ig) in enumerate(accounts):
        print(f"  [{i}] @{ig.get('username')} (page: {page}, id {ig['id']})")
    idx = int(input("Which account? ") or 0) if len(accounts) > 1 else 0
    state.set_tokens("instagram", {"access_token": token, "ig_user_id": accounts[idx][1]["id"]})
    print(f"Instagram connected as @{accounts[idx][1].get('username')}.")
