"""VOD -> vertical clips, end to end."""
from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable

from shared import chat, llm, media, platforms
from shared.config import settings
from shared.db import Database, dumps
from shared.patterns import learned_patterns_text
from shared.transcribe import transcribe

from . import facecam, highlights, render, tighten

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


class JobError(RuntimeError):
    """An error worth showing to the Discord user as-is."""


async def process_vod(db: Database, job: dict, progress: Progress) -> list[int]:
    p = job["payload"]
    url: str = p["url"]
    count = int(p.get("count") or settings.clips_per_vod)
    layout = p.get("layout", "auto")
    subtitles = bool(p.get("subtitles", True))
    tighten_cuts = bool(p.get("tighten", settings.cut_dead_air))
    streamer = db.one("SELECT * FROM streamers WHERE id=?", (p["streamer_id"],))
    if not streamer:
        raise JobError("Streamer was removed before the job ran.")
    if streamer["permission"] != "allowed":
        raise JobError(f"Clipping is not allowed for {streamer['channel']} (status: {streamer['permission']}).")

    work = settings.work_dir / f"job{job['id']}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        await progress("Reading VOD info…")
        info = await asyncio.to_thread(platforms.extract_info, url)
        if info.get("is_live") or info.get("live_status") == "is_live":
            raise JobError("This stream is still live. It will be clipped once the VOD is finished.")
        vod_title = info.get("title") or "VOD"
        vod_length = float(info.get("duration") or 0)

        await progress(f"Downloading audio for analysis ({_fmt_len(vod_length)})…")
        audio = await asyncio.to_thread(platforms.download_audio, url, work)

        await progress("Scanning audio for hype moments…")
        loud = await asyncio.to_thread(media.loudness_per_second, audio)
        audio_excitement = highlights.excitement_curve(loud)

        messages = None
        if streamer["platform"] in ("twitch", "youtube"):
            await progress("Reading chat replay…")
            messages = await chat.fetch_chat(streamer["platform"], url, info, work)
        chat_curve = None
        if messages:
            chat_curve = chat.chat_excitement(chat.chat_activity(messages, len(loud)))
        excitement = highlights.combine_signals(audio_excitement, chat_curve)
        window = settings.clip_max_seconds + 40
        # Fewer candidates for a local CPU model keeps the selection prompt (and wait) reasonable.
        max_candidates = min(count * 2, 8) if llm.is_local() else min(count * 3, 12)
        windows = highlights.pick_windows(excitement, max_candidates, window)
        if not windows:
            raise JobError("VOD is too short to clip.")

        candidates: list[highlights.Candidate] = []
        for i, (start, end, score) in enumerate(windows, 1):
            await progress(f"Transcribing candidate moment {i}/{len(windows)}…")
            words = await asyncio.to_thread(transcribe, audio, start, end - start)
            reaction = chat.summarize(messages, start, end) if messages else ""
            candidates.append(highlights.Candidate(i, start, end, score, words, reaction))

        await progress("Choosing the best clips…")
        plans: list[highlights.ClipPlan] = []
        if settings.llm_available():
            try:
                patterns = await asyncio.to_thread(learned_patterns_text, db, 10, streamer["channel"])
                plans = await highlights.llm_plans(candidates, count, settings.clip_min_seconds,
                                                   settings.clip_max_seconds, streamer["channel"], vod_title,
                                                   patterns)
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM clip selection failed, using heuristic: %s", exc)
        if not plans:
            plans = highlights.heuristic_plans(candidates, count, settings.clip_min_seconds,
                                               settings.clip_max_seconds)

        detector = facecam.FaceDetector()
        clip_ids: list[int] = []
        for i, plan in enumerate(plans, 1):
            length = plan.end - plan.start
            await progress(f"Clip {i}/{len(plans)}: downloading {_fmt_len(length)} of video…")
            src = await asyncio.to_thread(platforms.download_section, url, plan.start, plan.end,
                                          work / f"clip{i}_src.mp4")
            await progress(f"Clip {i}/{len(plans)}: finding facecam…")
            scene = await asyncio.to_thread(facecam.analyse, src, detector)
            token = secrets.token_urlsafe(12)
            out = settings.clips_dir / f"{token}.mp4"
            keep = [(0.0, length)]
            if tighten_cuts:
                keep = tighten.plan_keep_segments(plan.words, length, loud[int(plan.start): int(plan.end) + 1],
                                                  min_total=min(settings.clip_min_seconds, length))
            words = tighten.remap_words(plan.words, keep)
            final_length = tighten.kept_duration(keep)
            cut_note = f", cutting {length - final_length:.0f}s of dead air" if len(keep) > 1 else ""
            await progress(f"Clip {i}/{len(plans)}: rendering vertical video ({scene.kind}{cut_note})…")
            used_layout = await asyncio.to_thread(render.render, src, out, scene, words, final_length,
                                                  layout, subtitles, keep)
            out.with_suffix(".ass").unlink(missing_ok=True)
            clip_id = db.execute(
                "INSERT INTO clips (job_id, guild_id, streamer_id, vod_url, start_s, end_s, duration, title, reason, "
                "layout, transcript, file_path, token, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job["id"], job["guild_id"], streamer["id"], url, plan.start, plan.end, final_length, plan.title,
                 plan.reason, used_layout, dumps(words), str(out), token, time.time()),
            )
            clip_ids.append(clip_id)
        return clip_ids
    finally:
        shutil.rmtree(work, ignore_errors=True)


def make_preview_for(db: Database, clip: dict, max_bytes: int) -> Path | None:
    full = Path(clip["file_path"])
    if full.stat().st_size <= max_bytes:
        return full
    preview = full.with_name(full.stem + "_preview.mp4")
    if not preview.exists():
        result = render.make_preview(full, preview, media.duration(full), max_bytes)
        if result is None:
            return None
    db.execute("UPDATE clips SET preview_path=? WHERE id=?", (str(preview), clip["id"]))
    return preview


def _fmt_len(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 60}m{s % 60:02d}s"
