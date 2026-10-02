"""Health checks for /status, and the daily yt-dlp update check."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import aiohttp
import yt_dlp

from . import platforms
from .config import settings

SITES = {
    "Twitch": "https://www.twitch.tv/",
    "YouTube": "https://www.youtube.com/",
    "TikTok": "https://www.tiktok.com/",
    "Instagram": "https://www.instagram.com/",
}


async def _check_site(session: aiohttp.ClientSession, name: str, url: str) -> tuple[str, bool, str]:
    try:
        async with session.get(url, allow_redirects=True) as resp:
            ok = resp.status < 400
            return name, ok, f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return name, False, type(exc).__name__


def _check_kick() -> tuple[str, bool, str]:
    try:
        platforms.kick_api("v2/channels/xqc")
        return "Kick", True, "API OK"
    except Exception as exc:  # noqa: BLE001
        return "Kick", False, str(exc)[:80]


async def check_sites() -> list[tuple[str, bool, str]]:
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=headers) as session:
        results = await asyncio.gather(*(_check_site(session, n, u) for n, u in SITES.items()),
                                       asyncio.to_thread(_check_kick))
    return list(results)


async def check_ai() -> list[tuple[str, bool, str]]:
    out = []
    providers = {settings.provider_for("general"), settings.provider_for("rating")}
    if "ollama" in providers:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(f"{settings.ollama_url}/api/tags") as resp:
                    data = await resp.json(content_type=None)
            have = {m["name"] for m in data.get("models", [])}
            for model in (settings.ollama_text_model, settings.ollama_vision_model):
                present = model in have or f"{model}:latest" in have
                out.append((f"Ollama {model}", True, "downloaded" if present else "downloads on first use"))
        except Exception as exc:  # noqa: BLE001
            out.append(("Ollama", False, f"not reachable ({type(exc).__name__})"))
    if "anthropic" in providers:
        out.append(("Claude", bool(settings.anthropic_api_key),
                    settings.claude_model if settings.anthropic_api_key else "ANTHROPIC_API_KEY missing"))
    if "none" in providers:
        out.append(("AI", True, "turned off for some tasks (provider none)"))
    return out


def disk_report() -> tuple[float, float, float]:
    """(free GB, total GB, GB used by clips)."""
    usage = shutil.disk_usage(settings.data_dir)
    clips = sum(f.stat().st_size for f in Path(settings.clips_dir).glob("*") if f.is_file())
    return usage.free / 1e9, usage.total / 1e9, clips / 1e9


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def ytdlp_version() -> str:
    return yt_dlp.version.__version__


async def ytdlp_update_available() -> str | None:
    """Newest yt-dlp version on PyPI if it's newer than the installed one."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get("https://pypi.org/pypi/yt-dlp/json") as resp:
                latest = (await resp.json(content_type=None))["info"]["version"]
    except Exception:  # noqa: BLE001
        return None
    return latest if _version_tuple(latest) > _version_tuple(ytdlp_version()) else None
