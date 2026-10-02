"""Decide whether a streamer allows their streams to be clipped.

The bot reads the public text a streamer writes about themselves (Twitch bio + panels,
Kick bio, YouTube channel description), looks for explicit rules about clipping, and
optionally asks Claude to judge ambiguous wording. Anything not explicitly allowed stays
``unknown`` and is NOT clipped until an admin approves it with ``/streamer permission``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
from dataclasses import dataclass

from . import llm, platforms
from .config import settings

log = logging.getLogger(__name__)

DENY_PATTERNS = [
    r"\b(no|not allowed to|do not|don'?t|never)\s+(clip|clipping|clips|re-?upload|repost)\b",
    r"\b(clipping|clips|re-?uploads?|reposting)\s+(is|are)\s+(not\s+allowed|prohibited|forbidden|not\s+permitted|banned)",
    r"\b(clipping|clips|re-?uploads?)\s+(will be|get)\s+(reported|struck|taken down|copyright(ed)?)",
    r"\bunauthori[sz]ed\s+(clips|clipping|re-?uploads?)",
    r"\b(dmca|copyright\s+strike)\b.{0,40}\b(clip|clips|clipping|re-?upload)",
    r"\b(clip|clips|clipping|re-?upload)\b.{0,40}\b(dmca|copyright\s+strike)",
    r"\bonly\s+(my\s+|our\s+)?(official|approved|authori[sz]ed|verified)\s+(clippers?|editors?)\b",
]
ALLOW_PATTERNS = [
    r"\b(clipping|clips|clippers?)\s+(is|are)\s+(allowed|welcome|encouraged|permitted|ok|okay|fine)",
    r"\bfeel\s+free\s+to\s+(clip|use|repost|re-?upload)",
    r"\bclipping\s+(program|campaign|rewards?|bounty)",
    r"\b(you\s+can|you\s+may|anyone\s+can)\s+(clip|use\s+my\s+clips|repost)",
    r"\bclip\s+(me|my\s+streams?|my\s+content|away)\b",
    r"\bclippers?\s+(welcome|wanted|needed)",
    r"\b(vyro|clipping\.com|contentrewards|whop\.com/.{0,30}clip)",
]


@dataclass
class PermissionResult:
    status: str               # allowed | denied | unknown
    evidence: str             # quote or explanation
    sources: list[str]
    text_checked: int          # characters of profile text read


def classify_text(text: str) -> tuple[str, str]:
    """Keyword classifier. A deny rule always beats an allow rule."""
    lowered = text.lower()
    for pattern in DENY_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            return "denied", _snippet(text, match.start(), match.end())
    for pattern in ALLOW_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            return "allowed", _snippet(text, match.start(), match.end())
    return "unknown", ""


def _snippet(text: str, start: int, end: int, pad: int = 60) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return ("…" if s else "") + " ".join(text[s:e].split()) + ("…" if e < len(text) else "")


def _http_json(url: str, data: bytes | None = None, headers: dict | None = None) -> object:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _twitch_text(channel: str) -> tuple[str, list[str]]:
    texts, sources = [], []
    # Twitch's public web GQL endpoint is the only place panels are exposed. It is unofficial and may
    # change; failure just means we read less text.
    query = [{
        "operationName": "ChannelPanels",
        "query": "query ChannelPanels($login: String!) { user(login: $login) { description "
                 "panels { ... on DefaultPanel { title description linkURL } } } }",
        "variables": {"login": channel},
    }]
    try:
        result = _http_json("https://gql.twitch.tv/gql", json.dumps(query).encode(),
                            {"Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko", "Content-Type": "application/json"})
        user = (result[0].get("data") or {}).get("user") or {}  # type: ignore[index]
        if user.get("description"):
            texts.append(user["description"])
            sources.append("twitch bio")
        for panel in user.get("panels") or []:
            panel_text = " ".join(filter(None, [panel.get("title"), panel.get("description")]))
            if panel_text:
                texts.append(panel_text)
        if user.get("panels"):
            sources.append(f"twitch panels ({len(user['panels'])})")
    except Exception as exc:  # noqa: BLE001
        log.warning("Twitch panel lookup failed for %s: %s", channel, exc)
    return "\n\n".join(texts), sources


def _kick_text(channel: str) -> tuple[str, list[str]]:
    try:
        data = platforms.kick_api(f"v2/channels/{channel}")
        user = data.get("user") or {}
        bio = user.get("bio") or ""
        return bio, (["kick bio"] if bio else [])
    except Exception as exc:  # noqa: BLE001
        log.warning("Kick profile lookup failed for %s: %s", channel, exc)
        return "", []


def _youtube_text(channel: str) -> tuple[str, list[str]]:
    try:
        info = platforms.extract_info(platforms.channel_url("youtube", channel), flat=True, limit=1)
        desc = info.get("description") or ""
        return desc, (["youtube description"] if desc else [])
    except Exception as exc:  # noqa: BLE001
        log.warning("YouTube channel lookup failed for %s: %s", channel, exc)
        return "", []


def fetch_profile_text(platform: str, channel: str) -> tuple[str, list[str]]:
    if platform == "twitch":
        return _twitch_text(channel)
    if platform == "kick":
        return _kick_text(channel)
    if platform == "youtube":
        return _youtube_text(channel)
    return "", []


PERMISSION_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["allowed", "denied", "unknown"]},
        "quote": {"type": "string", "description": "Exact quote from the profile text supporting the status, or empty"},
        "explanation": {"type": "string"},
    },
    "required": ["status", "quote", "explanation"],
    "additionalProperties": False,
}


async def _llm_classify(platform: str, channel: str, text: str) -> tuple[str, str] | None:
    system = ("You check whether a live streamer permits fans to clip their streams and repost the clips to "
              "short-form platforms (TikTok, Instagram Reels, YouTube Shorts, Facebook). Only answer 'allowed' or "
              "'denied' when the streamer's own text clearly says so; otherwise answer 'unknown'. 'quote' must be "
              "copied verbatim from the text.")
    content = [llm.text_block(f"Platform: {platform}\nChannel: {channel}\n\nProfile text:\n\"\"\"\n{text[:20000]}\n\"\"\"")]
    try:
        answer = await llm.ask_json(system, content, PERMISSION_SCHEMA, effort="low", max_tokens=2000)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM permission check failed: %s", exc)
        return None
    quote = (answer.get("quote") or "").strip()
    # Never trust an 'allowed' that isn't backed by real text from the profile.
    if answer["status"] != "unknown" and (not quote or quote.lower() not in text.lower()):
        return "unknown", f"The AI suggested {answer['status']} but could not quote the profile."
    return answer["status"], quote or answer.get("explanation", "")


async def check_permission(platform: str, channel: str) -> PermissionResult:
    text, sources = await asyncio.to_thread(fetch_profile_text, platform, channel)
    status, evidence = classify_text(text)
    if status == "unknown" and text.strip() and settings.llm_available():
        judged = await _llm_classify(platform, channel, text)
        if judged:
            status, evidence = judged
    if not evidence:
        evidence = ("No clipping rules found in the public profile." if text.strip()
                    else "Could not read any profile text.")
    return PermissionResult(status, evidence, sources, len(text))
