"""VOD -> vertical clips, end to end, plus re-edits of existing clips."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np

from shared import chat, llm, media, platforms
from shared.config import settings
from shared.db import Database, dumps
from shared.patterns import learned_patterns_text
from shared.transcribe import Word, transcribe

from . import censor, facecam, highlights, render, tighten

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


class JobError(RuntimeError):
    """An error worth showing to the Discord user as-is."""


TRANSIENT_MARKERS = ("timed out", "timeout", "temporary failure", "temporarily", "connection reset",
                     "connection refused", "connection aborted", "remote end closed", "incompleteread",
                     "http error 5", "http error 429", "too many requests", "502", "503", "504",
                     "unable to download webpage", "network is unreachable", "name resolution")

FRIENDLY_ERRORS = [
    ("sign in to confirm", "YouTube is blocking the server (\"confirm you're not a bot\"). Add a cookies file "
                           "(README → Cookies) and try again."),
    ("subscriber", "This VOD is subscriber-only, so the bot can't download it."),
    ("private video", "This video is private."),
    ("video unavailable", "This video is unavailable (deleted, region-locked or not finished processing)."),
    ("has been removed", "This video has been removed."),
    ("http error 403", "The site refused the download (HTTP 403). It may need a cookies file, or the VOD is "
                       "restricted."),
    ("unsupported url", "That link isn't a VOD the downloader recognises."),
    ("no space left", "The server's disk is full. Discard old clips or lower CLIP_RETENTION_DAYS."),
]


def is_transient(exc: BaseException) -> bool:
    """Network-ish failures worth retrying later (not bad links or blocked content)."""
    if isinstance(exc, JobError):
        return False
    text = f"{type(exc).__name__} {exc}".lower()
    if any(marker in text for marker, _ in FRIENDLY_ERRORS):
        return False
    return any(marker in text for marker in TRANSIENT_MARKERS)


def explain_error(exc: BaseException) -> str:
    if isinstance(exc, JobError):
        return str(exc)
    text = str(exc)
    lowered = text.lower()
    for marker, friendly in FRIENDLY_ERRORS:
        if marker in lowered:
            return f"{friendly}\n-# {text[:300]}"
    return f"{type(exc).__name__}: {text}"


def vod_key(url: str) -> str:
    """Stable identity for a VOD, so different URL spellings of the same VOD match."""
    platform = platforms.detect_platform(url) or "other"
    return f"{platform}:{platforms.vod_id_from_url(url)}"


def resolve_layout(requested: str | None, streamer: dict) -> str:
    if requested and requested != "auto":
        return requested
    return streamer.get("layout") or "auto"


@dataclass
class ClipSpec:
    """Everything needed to (re-)render a clip from a kept source video."""
    source: Path                  # source video file (covers source_words' timeline)
    source_words: list[Word]
    window: tuple[float, float]   # part of the source to use
    loudness: np.ndarray | None   # per-second loudness of the source (for dead-air cuts)
    title: str
    reason: str
    hook_text: str
    layout: str
    subtitles: bool
    tighten: bool


def render_clip(spec: ClipSpec, scene: facecam.SceneLayout, out: Path) -> tuple[str, float, list[Word]]:
    """Cut dead air, bleep slurs, render. Returns (layout used, final length, caption words)."""
    ws, we = spec.window
    length = we - ws
    words = highlights.words_in_range(spec.source_words, ws, we)
    keep = [(0.0, length)]
    if spec.tighten and spec.loudness is not None:
        loud = spec.loudness[int(ws): int(we) + 1]
        keep = tighten.plan_keep_segments(words, length, loud, min_total=min(settings.clip_min_seconds, length))
    words = tighten.remap_words(words, keep)
    final_length = tighten.kept_duration(keep)
    bleeps = censor.find_bleeps(words) if settings.bleep_mode != "off" else []
    caption_words = censor.mask_words(words) if bleeps else words
    source_keep = [(s + ws, e + ws) for s, e in keep]
    used = render.render(spec.source, out, scene, caption_words, final_length, spec.layout, spec.subtitles,
                         source_keep, hook_text=spec.hook_text, bleeps=bleeps)
    out.with_suffix(".ass").unlink(missing_ok=True)
    return used, final_length, caption_words


def save_clip(db: Database, job: dict, streamer: dict, url: str, vod_start: float, spec: ClipSpec,
              out: Path, token: str, used_layout: str, final_length: float, words: list[Word],
              parent_clip_id: int | None = None) -> int:
    return db.execute(
        "INSERT INTO clips (job_id, guild_id, streamer_id, vod_url, start_s, end_s, duration, title, reason, layout, "
        "transcript, file_path, token, hook_text, source_path, source_words, options, parent_clip_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job["id"], job["guild_id"], streamer["id"], url, vod_start + spec.window[0], vod_start + spec.window[1],
         final_length, spec.title, spec.reason, used_layout, dumps(words), str(out), token, spec.hook_text,
         str(spec.source), dumps(spec.source_words), dumps({"subtitles": spec.subtitles, "tighten": spec.tighten}),
         parent_clip_id, time.time()),
    )


async def process_vod(db: Database, job: dict, progress: Progress) -> list[int]:
    p = job["payload"]
    url: str = p["url"]
    count = int(p.get("count") or settings.clips_per_vod)
    subtitles = bool(p.get("subtitles", True))
    tighten_cuts = bool(p.get("tighten", settings.cut_dead_air))
    streamer = db.one("SELECT * FROM streamers WHERE id=?", (p["streamer_id"],))
    if not streamer:
        raise JobError("Streamer was removed before the job ran.")
    if streamer["permission"] != "allowed":
        raise JobError(f"Clipping is not allowed for {streamer['channel']} (status: {streamer['permission']}).")
    layout = resolve_layout(p.get("layout"), streamer)
    # A retried or interrupted run may have rendered clips that were never posted: start clean.
    for old in db.all("SELECT * FROM clips WHERE job_id=? AND message_id IS NULL", (job["id"],)):
        delete_clip_files(db, old)
        db.execute("DELETE FROM clips WHERE id=?", (old["id"],))

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
        viewer_clips: list[dict] = []
        if streamer["platform"] == "twitch":
            viewer_clips = await chat.twitch_vod_clips(streamer["channel"], str(info.get("id") or ""))
            if viewer_clips:
                await progress(f"Found {len(viewer_clips)} clip(s) viewers already made from this VOD…")
        excitement = highlights.combine_signals(
            audio_excitement, chat_curve,
            viewer_clips=chat.clips_curve(viewer_clips, len(loud)) if viewer_clips else None)
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
            reaction = "; ".join(filter(None, [chat.summarize(messages, start, end) if messages else "",
                                               chat.clips_summary(viewer_clips, start, end)]))
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
                log.warning("AI clip selection failed, using heuristic: %s", exc)
        if not plans:
            plans = highlights.heuristic_plans(candidates, count, settings.clip_min_seconds,
                                               settings.clip_max_seconds)

        detector = facecam.FaceDetector()
        clip_ids: list[int] = []
        for i, plan in enumerate(plans, 1):
            length = plan.end - plan.start
            token = secrets.token_urlsafe(12)
            await progress(f"Clip {i}/{len(plans)}: downloading {_fmt_len(length)} of video…")
            # Keep the un-edited source next to the clip so it can be re-edited from Discord.
            src = await asyncio.to_thread(platforms.download_section, url, plan.start, plan.end,
                                          settings.clips_dir / f"{token}_src.mp4")
            await progress(f"Clip {i}/{len(plans)}: finding facecam…")
            scene = await asyncio.to_thread(facecam.analyse, src, detector)
            spec = ClipSpec(src, plan.words, (0.0, length), loud[int(plan.start): int(plan.end) + 2],
                            plan.title, plan.reason, plan.hook_text, layout, subtitles, tighten_cuts)
            out = settings.clips_dir / f"{token}.mp4"
            await progress(f"Clip {i}/{len(plans)}: rendering vertical video ({scene.kind})…")
            used, final_length, words = await asyncio.to_thread(render_clip, spec, scene, out)
            clip_ids.append(save_clip(db, job, streamer, url, plan.start, spec, out, token, used, final_length,
                                      words))
        return clip_ids
    finally:
        shutil.rmtree(work, ignore_errors=True)


def shorter_window(source_words: list[Word], loudness: np.ndarray, current: tuple[float, float],
                   current_final: float, min_len: float) -> tuple[float, float]:
    """Pick a ~30% shorter window inside ``current`` that keeps the liveliest part."""
    target_final = max(min_len, current_final * 0.7)
    if current_final - target_final < 3:
        raise JobError(f"This clip is already about as short as allowed ({int(min_len)}s minimum).")
    cs, ce = current
    # Account for dead-air cuts: the source window needs to be a bit longer than the final length.
    target = min(ce - cs, target_final * (ce - cs) / max(current_final, 1))
    loud = loudness[int(cs): int(ce) + 1] if loudness.size else np.zeros(int(ce - cs) + 1)
    lively = np.clip(loud - np.median(loud), 0, None) if loud.size else loud
    talk = np.zeros_like(lively)
    for w in source_words:
        i = int(w["s"] - cs)
        if 0 <= i < len(talk):
            talk[i] += 1
    score = lively + np.minimum(talk, 3)
    csum = np.concatenate([[0.0], np.cumsum(score)])
    width = int(target)
    best_start, best = 0, -1.0
    for s in range(0, max(1, len(score) - width + 1)):
        value = csum[min(len(score), s + width)] - csum[s]
        if value > best + 1e-9:  # ties keep the earlier start (keeps the original hook)
            best, best_start = value, s
    start = max(cs, highlights.snap_start(source_words, cs + best_start, tolerance=4))
    end = highlights.snap_end(source_words, start + target, tolerance=4)
    if end - start > target + 2:  # snapping must not undo the shortening
        end = start + target
    end = min(ce, max(end, start + min(min_len, ce - cs)))
    return start, end


async def rerender_clip(db: Database, job: dict, progress: Progress) -> list[int]:
    """Re-edit an existing clip: make it shorter and/or change its layout."""
    p = job["payload"]
    clip = db.one("SELECT * FROM clips WHERE id=?", (p["clip_id"],))
    if not clip:
        raise JobError("That clip no longer exists.")
    if not clip.get("source_path") or not Path(clip["source_path"]).exists():
        raise JobError("The source video for this clip has expired, so it can't be re-edited.")
    streamer = db.one("SELECT * FROM streamers WHERE id=?", (clip["streamer_id"],)) or {"id": clip["streamer_id"]}
    source = Path(clip["source_path"])
    source_words = json.loads(clip["source_words"] or "[]")
    options = json.loads(clip.get("options") or "{}")
    vod_start = clip["start_s"]
    # Where the previous clip sits inside its source file. Re-edits of re-edits share the
    # original source, so the stored window is the clip's start/end relative to the source.
    source_start = clip["start_s"] - _source_offset(db, clip)
    window = (source_start, source_start + (clip["end_s"] - clip["start_s"]))
    vod_origin = vod_start - source_start

    await progress("Re-reading the clip…")
    loudness = await asyncio.to_thread(media.loudness_per_second, source)
    if p.get("shorter"):
        window = shorter_window(source_words, loudness, window, clip["duration"] or (window[1] - window[0]),
                                settings.clip_min_seconds)
    layout = p.get("layout") or clip["layout"]
    await progress("Finding facecam…")
    scene = await asyncio.to_thread(facecam.analyse, source)
    spec = ClipSpec(source, source_words, window, loudness, clip["title"], clip["reason"] or "",
                    clip.get("hook_text") or "", layout, options.get("subtitles", True),
                    options.get("tighten", settings.cut_dead_air))
    token = secrets.token_urlsafe(12)
    out = settings.clips_dir / f"{token}.mp4"
    await progress("Rendering the new version…")
    used, final_length, words = await asyncio.to_thread(render_clip, spec, scene, out)
    clip_id = save_clip(db, job, streamer, clip["vod_url"], vod_origin, spec, out, token, used, final_length,
                        words, parent_clip_id=clip["id"])
    return [clip_id]


def _source_offset(db: Database, clip: dict) -> float:
    """VOD time at which this clip's source file starts (follows re-edits back to the original)."""
    root = clip
    while root.get("parent_clip_id"):
        parent = db.one("SELECT * FROM clips WHERE id=?", (root["parent_clip_id"],))
        if not parent or parent.get("source_path") != clip.get("source_path"):
            break
        root = parent
    # The original clip's source starts exactly at its start time (its window began at 0).
    return root["start_s"]


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


def delete_clip_files(db: Database, clip: dict) -> None:
    """Delete a clip's files. The source is kept while another clip still uses it."""
    for key in ("file_path", "preview_path"):
        if clip.get(key):
            Path(clip[key]).unlink(missing_ok=True)
    src = clip.get("source_path")
    if src:
        others = db.one("SELECT COUNT(*) AS n FROM clips WHERE source_path=? AND id<>? AND file_path IS NOT NULL",
                        (src, clip["id"]))
        if not others or others["n"] == 0:
            Path(src).unlink(missing_ok=True)
    db.execute("UPDATE clips SET file_path=NULL, preview_path=NULL WHERE id=?", (clip["id"],))


def _fmt_len(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 60}m{s % 60:02d}s"
