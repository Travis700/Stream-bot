"""Optional: post approved clips straight to TikTok, Instagram Reels and Facebook Reels.

Each platform only appears in Discord once its keys are in .env (see README):
* TikTok: Content Posting API (direct post). Unaudited apps can only post privately
  (SELF_ONLY) until TikTok reviews the app.
* Instagram / Facebook: Graph API with a Page access token. Meta downloads the video from
  PUBLIC_BASE_URL, so the file server must be reachable from the internet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp

from shared.config import settings
from shared.db import Database

log = logging.getLogger(__name__)

TIKTOK_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_API = "https://open.tiktokapis.com/v2"
MB = 1024 * 1024


class PostError(RuntimeError):
    pass


def enabled_platforms(db: Database) -> list[str]:
    out = []
    if settings.tiktok_client_key and settings.tiktok_client_secret:
        if db.one("SELECT 1 FROM oauth_tokens WHERE service='tiktok'"):
            out.append("tiktok")
    if settings.meta_page_token and settings.instagram_user_id and settings.public_base_url:
        out.append("instagram")
    if settings.meta_page_token and settings.facebook_page_id and settings.public_base_url:
        out.append("facebook")
    return out


def build_caption(clip: dict, max_len: int = 2200) -> str:
    rating = json.loads(clip.get("rating_json") or "{}") if clip.get("rating_json") else {}
    caption = (rating.get("suggested_caption") or clip.get("title") or "").strip()
    tags = [t if t.startswith("#") else f"#{t}" for t in rating.get("hashtags") or []]
    if not tags:
        tags = ["#clips", "#streamer", "#fyp"]
    return f"{caption}\n\n{' '.join(tags[:8])}".strip()[:max_len]


def public_url(clip: dict) -> str:
    if not settings.public_base_url:
        raise PostError("PUBLIC_BASE_URL is not set, so the platform can't fetch the video.")
    return f"{settings.public_base_url}/c/{clip['token']}.mp4"


# ---------------------------------------------------------------- TikTok
_oauth_states: dict[str, float] = {}


def tiktok_redirect_uri() -> str:
    return f"{settings.public_base_url}/oauth/tiktok"


def tiktok_authorize_url() -> str:
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = time.time()
    query = urlencode({"client_key": settings.tiktok_client_key, "scope": "user.info.basic,video.publish",
                       "response_type": "code", "redirect_uri": tiktok_redirect_uri(), "state": state})
    return f"{TIKTOK_AUTH}?{query}"


def check_state(state: str) -> bool:
    created = _oauth_states.pop(state, None)
    return created is not None and time.time() - created < 900


def _save_tiktok_tokens(db: Database, data: dict) -> None:
    db.execute(
        "INSERT INTO oauth_tokens (service, access_token, refresh_token, expires_at, extra) VALUES ('tiktok',?,?,?,?) "
        "ON CONFLICT(service) DO UPDATE SET access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
        "expires_at=excluded.expires_at, extra=excluded.extra",
        (data["access_token"], data["refresh_token"], time.time() + int(data.get("expires_in", 86400)) - 300,
         json.dumps({"open_id": data.get("open_id")})),
    )


async def tiktok_exchange_code(db: Database, code: str) -> None:
    form = {"client_key": settings.tiktok_client_key, "client_secret": settings.tiktok_client_secret,
            "code": code, "grant_type": "authorization_code", "redirect_uri": tiktok_redirect_uri()}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{TIKTOK_API}/oauth/token/", data=form) as resp:
            data = await resp.json(content_type=None)
    if "access_token" not in data:
        raise PostError(f"TikTok login failed: {data}")
    _save_tiktok_tokens(db, data)


async def _tiktok_token(db: Database, session: aiohttp.ClientSession) -> str:
    row = db.one("SELECT * FROM oauth_tokens WHERE service='tiktok'")
    if not row:
        raise PostError("TikTok isn't connected. Run /connect tiktok first.")
    if row["expires_at"] and row["expires_at"] > time.time():
        return row["access_token"]
    form = {"client_key": settings.tiktok_client_key, "client_secret": settings.tiktok_client_secret,
            "grant_type": "refresh_token", "refresh_token": row["refresh_token"]}
    async with session.post(f"{TIKTOK_API}/oauth/token/", data=form) as resp:
        data = await resp.json(content_type=None)
    if "access_token" not in data:
        raise PostError(f"TikTok token refresh failed (reconnect with /connect tiktok): {data}")
    _save_tiktok_tokens(db, data)
    return data["access_token"]


def tiktok_chunks(size: int, chunk_size: int = 10 * MB) -> list[tuple[int, int]]:
    """Byte ranges for TikTok's upload rules: files < 5 MB go in one chunk; otherwise
    floor(size / chunk_size) chunks with the remainder merged into the last one."""
    if size < 5 * MB or size <= chunk_size:
        return [(0, size - 1)]
    count = size // chunk_size
    ranges = [(i * chunk_size, (i + 1) * chunk_size - 1) for i in range(count)]
    ranges[-1] = (ranges[-1][0], size - 1)
    return ranges


async def post_tiktok(db: Database, clip: dict) -> dict:
    path = Path(clip["file_path"])
    size = path.stat().st_size
    chunks = tiktok_chunks(size)
    chunk_size = chunks[0][1] - chunks[0][0] + 1
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800)) as session:
        token = await _tiktok_token(db, session)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        body = {
            "post_info": {"title": build_caption(clip), "privacy_level": settings.tiktok_privacy,
                          "disable_duet": False, "disable_comment": False, "disable_stitch": False},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size,
                            "total_chunk_count": len(chunks)},
        }
        async with session.post(f"{TIKTOK_API}/post/publish/video/init/", json=body, headers=headers) as resp:
            data = await resp.json(content_type=None)
        if (data.get("error") or {}).get("code") not in (None, "ok"):
            raise PostError(f"TikTok refused the upload: {data['error']}")
        publish_id = data["data"]["publish_id"]
        upload_url = data["data"]["upload_url"]
        with path.open("rb") as fh:
            for start, end in chunks:
                fh.seek(start)
                blob = fh.read(end - start + 1)
                put_headers = {"Content-Type": "video/mp4", "Content-Length": str(len(blob)),
                               "Content-Range": f"bytes {start}-{end}/{size}"}
                async with session.put(upload_url, data=blob, headers=put_headers) as resp:
                    if resp.status not in (200, 201, 206):
                        raise PostError(f"TikTok upload failed ({resp.status}): {(await resp.text())[:300]}")
        status = "PROCESSING"
        for _ in range(60):
            await asyncio.sleep(10)
            async with session.post(f"{TIKTOK_API}/post/publish/status/fetch/", json={"publish_id": publish_id},
                                    headers=headers) as resp:
                data = await resp.json(content_type=None)
            status = (data.get("data") or {}).get("status", status)
            if status in ("PUBLISH_COMPLETE", "FAILED"):
                break
        if status == "FAILED":
            raise PostError(f"TikTok processing failed: {data.get('data', {}).get('fail_reason')}")
    note = " (private: SELF_ONLY until TikTok audits your app)" if settings.tiktok_privacy == "SELF_ONLY" else ""
    return {"platform": "tiktok", "external_id": publish_id, "url": None, "note": f"status {status}{note}"}


# ---------------------------------------------------------------- Meta (Instagram + Facebook)
def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{settings.meta_graph_version}/{path.lstrip('/')}"


async def _graph_call(session: aiohttp.ClientSession, method: str, path: str, **params) -> dict:
    params["access_token"] = settings.meta_page_token
    async with session.request(method, _graph(path), params=params) as resp:
        data = await resp.json(content_type=None)
    if "error" in data:
        raise PostError(f"Meta API error: {data['error'].get('message')}")
    return data


async def post_instagram(db: Database, clip: dict) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800)) as session:
        container = await _graph_call(session, "POST", f"{settings.instagram_user_id}/media",
                                      media_type="REELS", video_url=public_url(clip),
                                      caption=build_caption(clip), share_to_feed="true")
        cid = container["id"]
        for _ in range(90):
            await asyncio.sleep(10)
            status = await _graph_call(session, "GET", cid, fields="status_code,status")
            if status.get("status_code") == "FINISHED":
                break
            if status.get("status_code") == "ERROR":
                raise PostError(f"Instagram couldn't process the video: {status.get('status')}")
        else:
            raise PostError("Instagram took too long to process the video.")
        published = await _graph_call(session, "POST", f"{settings.instagram_user_id}/media_publish", creation_id=cid)
        media = await _graph_call(session, "GET", published["id"], fields="permalink")
    return {"platform": "instagram", "external_id": published["id"], "url": media.get("permalink"), "note": ""}


async def post_facebook(db: Database, clip: dict) -> dict:
    page = settings.facebook_page_id
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800)) as session:
        start = await _graph_call(session, "POST", f"{page}/video_reels", upload_phase="start")
        video_id = start["video_id"]
        upload = f"https://rupload.facebook.com/video-upload/{settings.meta_graph_version}/{video_id}"
        headers = {"Authorization": f"OAuth {settings.meta_page_token}", "file_url": public_url(clip)}
        async with session.post(upload, headers=headers) as resp:
            data = await resp.json(content_type=None)
        if not data.get("success"):
            raise PostError(f"Facebook upload failed: {data}")
        await _graph_call(session, "POST", f"{page}/video_reels", upload_phase="finish", video_id=video_id,
                          video_state="PUBLISHED", description=build_caption(clip))
    return {"platform": "facebook", "external_id": video_id, "url": f"https://www.facebook.com/reel/{video_id}",
            "note": ""}


POSTERS = {"tiktok": post_tiktok, "instagram": post_instagram, "facebook": post_facebook}


async def post_clip(db: Database, clip: dict, platform: str) -> dict:
    if not clip.get("file_path") or not Path(clip["file_path"]).exists():
        raise PostError("The clip file has expired.")
    result = await POSTERS[platform](db, clip)
    db.execute("INSERT OR IGNORE INTO clip_posts (clip_id, platform, url, external_id, created_at) VALUES (?,?,?,?,?)",
               (clip["id"], platform, result.get("url") or f"{platform}:{result['external_id']}",
                result["external_id"], time.time()))
    return result
