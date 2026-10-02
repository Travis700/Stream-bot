"""Thin wrappers around yt-dlp for Twitch, Kick, YouTube, TikTok and Instagram.

All functions here are blocking; call them through ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import download_range_func

from .config import settings

log = logging.getLogger(__name__)

PLATFORMS = ("twitch", "kick", "youtube")


def detect_platform(url: str) -> str | None:
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    for name, needle in (("twitch", "twitch.tv"), ("kick", "kick.com"), ("youtube", "youtube.com"),
                         ("youtube", "youtu.be"), ("tiktok", "tiktok.com"), ("instagram", "instagram.com"),
                         ("facebook", "facebook.com")):
        if host == needle or host.endswith("." + needle):
            return name
    return None


def parse_channel(platform: str, value: str) -> str:
    """Accept a bare name/handle or a channel URL and return the channel slug."""
    value = value.strip()
    if "://" in value or "." in value.split("/")[0]:
        path = urlparse(value if "://" in value else f"https://{value}").path.strip("/")
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise ValueError(f"Could not find a channel name in {value!r}")
        if platform == "youtube":
            if parts[0] in ("channel", "c", "user") and len(parts) > 1:
                return f"{parts[0]}/{parts[1]}"
            return parts[0] if parts[0].startswith("@") else f"@{parts[0]}"
        return parts[0].lower()
    if platform == "youtube":
        return value if value.startswith("@") or "/" in value else f"@{value}"
    return value.lower()


def channel_url(platform: str, channel: str) -> str:
    if platform == "twitch":
        return f"https://www.twitch.tv/{channel}"
    if platform == "kick":
        return f"https://kick.com/{channel}"
    if platform == "youtube":
        return f"https://www.youtube.com/{channel}"
    raise ValueError(platform)


def vods_url(platform: str, channel: str) -> str:
    if platform == "twitch":
        return f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"
    if platform == "kick":
        return f"https://kick.com/{channel}/videos"
    if platform == "youtube":
        return f"https://www.youtube.com/{channel}/streams"
    raise ValueError(platform)


def _base_opts(**extra: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "noprogress": True, "retries": 5,
                            "fragment_retries": 10, "concurrent_fragment_downloads": 4}
    if settings.ytdlp_cookies and Path(settings.ytdlp_cookies).exists():
        opts["cookiefile"] = settings.ytdlp_cookies
    opts.update(extra)
    return opts


def extract_info(url: str, flat: bool = False, limit: int | None = None) -> dict[str, Any]:
    opts = _base_opts(skip_download=True)
    if flat:
        opts["extract_flat"] = "in_playlist"
    if limit:
        opts["playlistend"] = limit
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def list_recent_videos(url: str, limit: int = 10) -> list[dict[str, Any]]:
    """List the newest entries of a channel/profile page (VOD list, TikTok profile, ...)."""
    info = extract_info(url, flat=True, limit=limit)
    entries = [e for e in (info.get("entries") or []) if e]
    out = []
    for e in entries[:limit]:
        video_url = e.get("url") or e.get("webpage_url")
        if video_url and not video_url.startswith("http"):
            video_url = e.get("webpage_url") or video_url
        out.append({
            "id": str(e.get("id")),
            "url": video_url,
            "title": e.get("title"),
            "views": e.get("view_count"),
            "duration": e.get("duration"),
            "timestamp": e.get("timestamp"),
            "live_status": e.get("live_status"),
        })
    return out


def download_audio(url: str, out_dir: Path) -> Path:
    """Download the lowest-bandwidth stream that carries audio (used for highlight analysis)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = _base_opts(
        format="bestaudio/worstvideo*+bestaudio/worst",
        outtmpl=str(out_dir / "source_audio.%(ext)s"),
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


def download_section(url: str, start: float, end: float, out_path: Path, max_height: int = 1080) -> Path:
    """Download only [start, end] seconds of a VOD at up to ``max_height``p."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    opts = _base_opts(
        format=f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best",
        outtmpl=str(out_path.with_suffix("")) + ".%(ext)s",
        download_ranges=download_range_func(None, [(start, end)]),
        force_keyframes_at_cuts=True,
        merge_output_format="mp4",
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if not path.exists():
        # merge_output_format may change the extension
        candidates = sorted(out_path.parent.glob(out_path.stem + ".*"))
        if not candidates:
            raise FileNotFoundError(f"yt-dlp produced no file for {url}")
        path = candidates[0]
    return path


def download_small(url: str, out_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Download a short-form clip at low resolution (for analysis only, deleted afterwards)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = _base_opts(
        format="best[height<=720][vcodec!=none][acodec!=none]/bestvideo[height<=720]+bestaudio/best",
        outtmpl=str(out_dir / "ref.%(ext)s"),
        merge_output_format="mp4",
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        if not path.exists():
            path = next(out_dir.glob("ref.*"))
        return path, ydl.sanitize_info(info)


_VOD_ID = re.compile(r"/videos?/(\d+)")


def vod_id_from_url(url: str) -> str:
    match = _VOD_ID.search(url)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    if "v=" in parsed.query:
        return parsed.query.split("v=")[1].split("&")[0]
    return parsed.path.strip("/").split("/")[-1]


def timestamp_url(platform: str, url: str, seconds: float) -> str:
    s = int(seconds)
    if platform == "twitch":
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{url}{'&' if '?' in url else '?'}t={h}h{m}m{sec}s"
    if platform == "youtube":
        return f"{url}{'&' if '?' in url else '?'}t={s}s"
    if platform == "kick":
        return f"{url}{'&' if '?' in url else '?'}t={s}"
    return url


def kick_api(path: str) -> Any:
    """GET kick.com/api/<path>. Kick sits behind Cloudflare, so impersonate a browser when possible."""
    url = f"https://kick.com/api/{path.lstrip('/')}"
    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(url, impersonate="chrome", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except ImportError:
        import json
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())


def list_vods(platform: str, channel: str, limit: int = 5) -> list[dict[str, Any]]:
    """Newest finished-or-ongoing past broadcasts for a channel."""
    if platform == "kick":
        data = kick_api(f"v2/channels/{channel}/videos")
        items = data if isinstance(data, list) else data.get("data", [])
        out = []
        for item in items[:limit]:
            video = item.get("video") or {}
            uuid = video.get("uuid") or item.get("uuid")
            if not uuid:
                continue
            out.append({
                "id": uuid,
                "url": f"https://kick.com/{channel}/videos/{uuid}",
                "title": item.get("session_title") or video.get("title"),
                "views": item.get("viewer_count") or video.get("views"),
                "duration": (item.get("duration") or 0) / 1000 or None,
                "live_status": "is_live" if item.get("is_live") else "was_live",
            })
        return out
    return list_recent_videos(vods_url(platform, channel), limit)


def channel_from_info(platform: str, info: dict[str, Any]) -> str | None:
    """The channel slug/handle that owns a VOD, as used by parse_channel()."""
    if platform == "twitch":
        value = info.get("uploader_id") or info.get("uploader")
        return value.lower() if value else None
    if platform == "kick":
        value = info.get("channel") or info.get("uploader")
        return value.lower() if value else None
    if platform == "youtube":
        handle = info.get("uploader_id") or ""
        if handle.startswith("@"):
            return handle
        if info.get("channel_id"):
            return f"channel/{info['channel_id']}"
    return None
