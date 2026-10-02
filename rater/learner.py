"""Build a library of high-performing short-form streamer clips and what made them work."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time

from shared import llm, media, platforms
from shared.config import settings
from shared.db import Database, dumps
from shared.transcribe import transcribe, words_to_text

log = logging.getLogger(__name__)

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "streamer": {"type": "string", "description": "Streamer featured in the clip, or 'unknown'"},
        "hook": {"type": "string", "description": "What happens/is said in the first 3 seconds"},
        "topic": {"type": "string"},
        "emotion": {"type": "string", "description": "funny, rage, shock, wholesome, drama, skill, etc."},
        "why_it_worked": {"type": "string", "description": "1-2 sentences"},
        "format_notes": {"type": "string", "description": "Layout, captions, cuts, length, text overlays"},
    },
    "required": ["streamer", "hook", "topic", "emotion", "why_it_worked", "format_notes"],
    "additionalProperties": False,
}


def frame_times(duration: float, n: int = 8) -> list[float]:
    early = [t for t in (0.3, 1.5, 3.0) if t < duration]
    rest = [duration * (i + 1) / (n - len(early) + 1) for i in range(n - len(early))]
    return early + [t for t in rest if t > 3.5]


async def add_reference(db: Database, url: str, min_views: int = 0) -> dict:
    """Download, analyse and store one viral reference clip. Returns the DB row."""
    existing = db.one("SELECT * FROM reference_clips WHERE url=?", (url,))
    if existing and existing["status"] in ("ready", "skipped"):
        return existing
    if not existing:
        db.execute("INSERT INTO reference_clips (url, platform, status, created_at) VALUES (?,?,?,?)",
                   (url, platforms.detect_platform(url), "pending", time.time()))
    work = settings.work_dir / f"ref_{abs(hash(url))}"
    try:
        path, info = await asyncio.to_thread(platforms.download_small, url, work)
        views = info.get("view_count") or 0
        meta = (views, info.get("like_count"), info.get("comment_count"), info.get("duration"),
                (info.get("description") or info.get("title") or "")[:1000], info.get("uploader") or info.get("channel"))
        db.execute("UPDATE reference_clips SET views=?, likes=?, comments=?, duration=?, caption=?, uploader=? "
                   "WHERE url=?", (*meta, url))
        if min_views and views < min_views:
            db.execute("UPDATE reference_clips SET status='skipped', error=? WHERE url=?",
                       (f"only {views} views", url))
            return db.one("SELECT * FROM reference_clips WHERE url=?", (url,))  # type: ignore[return-value]

        duration = float(info.get("duration") or await asyncio.to_thread(media.duration, path))
        words = await asyncio.to_thread(transcribe, path)
        local = llm.is_local("rating")
        frames = await asyncio.to_thread(media.extract_frames, path, frame_times(duration, 5 if local else 8),
                                         work / "frames", 448 if local else 512)
        content = [llm.image_block(f) for f in frames]
        content.append(llm.text_block(
            f"This short-form clip of a livestreamer performed very well.\n"
            f"Platform: {platforms.detect_platform(url)}\nViews: {views:,}\nLikes: {info.get('like_count')}\n"
            f"Comments: {info.get('comment_count')}\nLength: {duration:.0f}s\nPosted by: {meta[5]}\n"
            f"Caption: {meta[4]}\n\nTranscript:\n{words_to_text(words) or '(no speech)'}\n\n"
            "The images are frames from the clip in order (the first three are from the first 3 seconds). "
            "Explain concisely what made it perform, so an editor can repeat it."))
        analysis = await llm.ask_json(
            "You analyse viral TikTok / Instagram Reels / Shorts clips of livestreamers for a clipping team.",
            content, ANALYSIS_SCHEMA, effort="low", max_tokens=4000, purpose="rating")
        db.execute("UPDATE reference_clips SET streamer=?, analysis=?, status='ready', error=NULL WHERE url=?",
                   (analysis.get("streamer"), dumps(analysis), url))
    except Exception as exc:  # noqa: BLE001
        log.warning("Reference %s failed: %s", url, exc)
        db.execute("UPDATE reference_clips SET status='failed', error=? WHERE url=?", (str(exc)[:1000], url))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return db.one("SELECT * FROM reference_clips WHERE url=?", (url,))  # type: ignore[return-value]


async def poll_reference_account(db: Database, account: dict, max_new: int = 5) -> int:
    """Add an account's new, high-view posts to the library."""
    posts = await asyncio.to_thread(platforms.list_recent_videos, account["url"], 30)
    added = 0
    for post in posts:
        if added >= max_new or not post.get("url"):
            break
        if db.one("SELECT id FROM reference_clips WHERE url=?", (post["url"],)):
            continue
        if post.get("views") is not None and post["views"] < account["min_views"]:
            continue
        row = await add_reference(db, post["url"], account["min_views"])
        if row and row["status"] == "ready":
            added += 1
    db.execute("UPDATE reference_accounts SET last_checked=? WHERE id=?", (time.time(), account["id"]))
    return added


def library_summary(db: Database) -> str:
    rows = db.all("SELECT status, COUNT(*) AS n FROM reference_clips GROUP BY status")
    return ", ".join(f"{r['n']} {r['status']}" for r in rows) or "empty"


def analysis_of(row: dict) -> dict:
    try:
        return json.loads(row.get("analysis") or "{}")
    except json.JSONDecodeError:
        return {}
