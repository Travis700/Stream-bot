"""Rate a clip 1-10 for short-form potential, using the learned library as reference."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from shared import llm, media
from shared.db import Database
from shared.patterns import format_card, reference_cards
from shared.transcribe import Word, transcribe, words_to_text

from .learner import frame_times

log = logging.getLogger(__name__)

RATING_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "1-10 overall chance of doing well"},
        "hook_score": {"type": "integer", "description": "1-10 strength of the first 3 seconds"},
        "predicted_views": {"type": "string", "description": "Rough range, e.g. '5k-20k'"},
        "verdict": {"type": "string", "description": "One sentence summary"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
        "edit_suggestions": {"type": "array", "items": {"type": "string"},
                             "description": "Concrete fixes, e.g. 'cut the first 8s', 'end at 2:05'"},
        "suggested_caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "best_platform": {"type": "string", "enum": ["tiktok", "instagram", "facebook", "youtube_shorts"]},
    },
    "required": ["score", "hook_score", "predicted_views", "verdict", "strengths", "weaknesses",
                 "edit_suggestions", "suggested_caption", "hashtags", "best_platform"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a short-form video strategist for a team that clips livestreamers (Twitch, Kick, YouTube) and posts "
    "the clips to TikTok, Instagram Reels, Facebook Reels and YouTube Shorts. You predict how well a clip will do. "
    "Judge it like the algorithm and a scrolling viewer would: does the first 1-3 seconds stop the scroll, is it "
    "understandable without context, is there a payoff, does it hold attention for its full length (these clips "
    "are 2-2.5 minutes, so pacing matters), are captions readable and the layout clean on a phone, is the moment "
    "shareable/commentable. Be calibrated and honest: most clips are a 4-6; reserve 9-10 for clips that match or "
    "beat the proven viral references. Use the reference library and past results to calibrate."
)


def calibration_text(db: Database, limit: int = 15) -> str:
    rows = db.all("SELECT title, rating, actual_views FROM clips WHERE actual_views IS NOT NULL AND rating IS NOT NULL "
                  "ORDER BY id DESC LIMIT ?", (limit,))
    return "\n".join(f"- \"{r['title']}\": you rated {r['rating']}/10 → it got {r['actual_views']:,} views"
                     for r in rows)


async def rate_video(db: Database, video: Path, work: Path, words: list[Word] | None = None,
                     title: str = "", streamer: str | None = None) -> dict:
    duration = await asyncio.to_thread(media.duration, video)
    if words is None:
        words = await asyncio.to_thread(transcribe, video)
    frames = await asyncio.to_thread(media.extract_frames, video, frame_times(duration, 10), work, 540)

    cards = await asyncio.to_thread(reference_cards, db, 25, streamer)
    library = "\n".join(format_card(c) for c in cards) or "(library is empty — rely on general knowledge)"
    calibration = await asyncio.to_thread(calibration_text, db)

    content: list[dict] = [
        # Stable, cacheable prefix: the reference library changes rarely.
        llm.text_block(f"Reference library of proven high-view streamer clips:\n{library}", cache=True),
    ]
    if calibration:
        content.append(llm.text_block(f"Your past ratings vs. real results:\n{calibration}"))
    content += [llm.image_block(f) for f in frames]
    content.append(llm.text_block(
        f"Clip to rate. Title/caption idea: {title or '(none)'}\nStreamer: {streamer or 'unknown'}\n"
        f"Length: {duration:.0f}s\nThe images are frames in order (first three are from the first 3 seconds).\n\n"
        f"Transcript:\n{words_to_text(words) or '(no speech)'}"))
    return await llm.ask_json(SYSTEM, content, RATING_SCHEMA, max_tokens=8000)
