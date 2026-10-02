"""Chat replay as a highlight signal: chat explodes ("LMAO", "KEKW", "CLIP IT") right after good moments.

* Twitch: the public web GQL endpoint the Twitch player uses for chat replay (unofficial; may change).
* YouTube: yt-dlp's ``live_chat`` replay for past livestreams.
* Kick: not supported (no practical chat-replay API), the bot falls back to audio only.

Everything here is best-effort: on any failure the caller gets ``None`` and carries on without chat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from pathlib import Path

import aiohttp
import numpy as np
import yt_dlp

from .platforms import _base_opts

log = logging.getLogger(__name__)

Message = tuple[float, str]  # (seconds into the VOD, text)

TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

# Messages that signal "that was a moment" count extra.
HYPE = re.compile(
    r"\b(clip|clip it|lmao+|lmfao|lol+|kekw|omegalul|lul|icant|pog+|pogchamp|poggers|holy|no way|wtf|"
    r"bro+|w+|l+|\?+|!+|xd+|dead|💀|😂|🤣|aware|monka\w*|pepelaugh|sadge|ratio)\b|[💀😂🤣🔥]",
    re.IGNORECASE,
)
CLIP_WORD = re.compile(r"\bclip\b", re.IGNORECASE)


# ---------------------------------------------------------------- fetching
async def _twitch_chat(vod_id: str, max_seconds: float = 20 * 60) -> list[Message]:
    messages: list[Message] = []
    deadline = asyncio.get_running_loop().time() + max_seconds
    cursor: str | None = None
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        while asyncio.get_running_loop().time() < deadline:
            variables: dict = {"videoID": vod_id}
            if cursor:
                variables["cursor"] = cursor
            else:
                variables["contentOffsetSeconds"] = 0
            body = [{"operationName": "VideoCommentsByOffsetOrCursor", "variables": variables,
                     "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TWITCH_COMMENTS_HASH}}}]
            async with session.post(TWITCH_GQL, json=body, headers=headers) as resp:
                data = await resp.json(content_type=None)
            comments = (((data[0].get("data") or {}).get("video") or {}).get("comments")) if data else None
            if not comments:
                errors = data[0].get("errors") if data else None
                if not messages:
                    raise RuntimeError(f"Twitch chat replay unavailable: {errors}")
                break
            edges = comments.get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                text = "".join(f.get("text", "") for f in (node.get("message") or {}).get("fragments") or [])
                messages.append((float(node.get("contentOffsetSeconds") or 0), text))
            if not edges or not (comments.get("pageInfo") or {}).get("hasNextPage"):
                break
            cursor = edges[-1].get("cursor")
            if not cursor:
                break
            await asyncio.sleep(0.05)
    return messages


def _youtube_chat(url: str, work: Path) -> list[Message]:
    work.mkdir(parents=True, exist_ok=True)
    opts = _base_opts(skip_download=True, writesubtitles=True, subtitleslangs=["live_chat"],
                      outtmpl=str(work / "chat.%(ext)s"))
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = list(work.glob("chat*.live_chat.json"))
    if not files:
        raise RuntimeError("No YouTube chat replay for this video")
    return parse_youtube_chat(files[0])


def parse_youtube_chat(path: Path) -> list[Message]:
    """Parse yt-dlp's .live_chat.json (one JSON object per line)."""
    messages: list[Message] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                replay = json.loads(line).get("replayChatItemAction") or {}
            except json.JSONDecodeError:
                continue
            offset = float(replay.get("videoOffsetTimeMsec") or 0) / 1000
            for action in replay.get("actions") or []:
                item = (action.get("addChatItemAction") or {}).get("item") or {}
                renderer = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                if not renderer:
                    continue
                runs = (renderer.get("message") or {}).get("runs") or []
                text = "".join(r.get("text") or (r.get("emoji") or {}).get("shortcuts", [""])[0] for r in runs)
                messages.append((offset, text))
    return messages


async def fetch_chat(platform: str, url: str, info: dict, work: Path) -> list[Message] | None:
    try:
        if platform == "twitch":
            vod_id = str(info.get("id") or "").lstrip("v")
            messages = await _twitch_chat(vod_id)
        elif platform == "youtube":
            messages = await asyncio.to_thread(_youtube_chat, url, work / "chat")
        else:
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("Chat replay not available for %s: %s", url, exc)
        return None
    log.info("Loaded %d chat messages for %s", len(messages), url)
    duration = float(info.get("duration") or 0)
    if messages and duration and max(t for t, _ in messages) < 0.85 * duration:
        # Only part of the VOD's chat loaded (time limit hit): using it would favour the start of the VOD.
        log.warning("Chat replay only covers part of %s; ignoring chat for this VOD", url)
        return None
    return messages or None


# ---------------------------------------------------------------- analysis
def message_weight(text: str) -> float:
    if CLIP_WORD.search(text):
        return 3.0
    if HYPE.search(text):
        return 1.8
    return 1.0


def chat_activity(messages: list[Message], length: int, delay: float = 8.0) -> np.ndarray:
    """Weighted messages per second, shifted ``delay`` seconds earlier (chat reacts after the moment)."""
    rate = np.zeros(max(length, 1), dtype=np.float32)
    for t, text in messages:
        i = int(t - delay)
        if 0 <= i < length:
            rate[i] += message_weight(text)
    return rate


def chat_excitement(rate: np.ndarray, baseline_seconds: int = 300, smooth_seconds: int = 6) -> np.ndarray:
    """How far chat activity rises above its local normal level (>= 0)."""
    n = len(rate)
    kernel = np.ones(smooth_seconds) / smooth_seconds
    smooth = np.convolve(rate, kernel, mode="same")
    block = 30
    blocks = [float(np.median(smooth[i:i + block])) for i in range(0, n, block)]
    half = max(1, baseline_seconds // block // 2)
    base_blocks = [float(np.median(blocks[max(0, i - half): i + half + 1])) for i in range(len(blocks))]
    centers = np.arange(len(blocks)) * block + block / 2
    baseline = np.interp(np.arange(n), centers, base_blocks)
    return np.clip(smooth - baseline, 0, None) / (baseline + 0.5)


def summarize(messages: list[Message], start: float, end: float, delay: float = 8.0, top: int = 6) -> str:
    """Short description of chat's reaction to [start, end], e.g. 'KEKW ×31, LMAO ×12 (95 msgs)'."""
    window = [text for t, text in messages if start + delay <= t <= end + delay]
    if not window:
        return ""
    counts: Counter[str] = Counter()
    for text in window:
        tokens = {tok for tok in re.findall(r"[\w'?!💀😂🤣🔥]+", text.upper()) if len(tok) <= 20}
        counts.update(tokens)
    common = ", ".join(f"{tok} ×{n}" for tok, n in counts.most_common(top) if n >= 3)
    return f"{len(window)} messages" + (f"; most spammed: {common}" if common else "")
