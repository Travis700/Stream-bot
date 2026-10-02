"""Find the best 1–2 minute moments in a long VOD, using CPU only.

1. Loudness per second for the whole VOD -> an "excitement" curve (yelling, laughing,
   hype moments stand out from the streamer's normal talking level).
2. Chat replay (Twitch/YouTube) -> a second curve: chat spamming "LMAO"/"CLIP IT" marks a moment.
3. The highest-scoring non-overlapping windows become candidates.
4. Only the candidates are transcribed (transcribing a full 6h VOD on CPU is slow).
5. The AI reads the candidate transcripts + chat reaction and picks/trims the best clips.
   Without AI a heuristic (excitement + how much is being said) picks instead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from shared import llm
from shared.transcribe import Word, words_to_text

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    id: int
    start: float          # absolute VOD seconds of the analysed region
    end: float
    score: float
    words: list[Word] = field(default_factory=list)  # timestamps relative to ``start``
    chat: str = ""        # summary of chat's reaction, if chat replay was available


@dataclass
class ClipPlan:
    start: float          # absolute VOD seconds
    end: float
    title: str
    reason: str
    score: float
    words: list[Word]     # timestamps relative to clip start
    hook_text: str = ""   # short on-screen text for the first seconds


def excitement_curve(loudness: np.ndarray, baseline_seconds: int = 300, smooth_seconds: int = 4) -> np.ndarray:
    """How much louder each second is than the streamer's local baseline (dB above, >= 0)."""
    n = len(loudness)
    if n == 0:
        return loudness
    block = 30
    blocks = [float(np.median(loudness[i:i + block])) for i in range(0, n, block)]
    half = max(1, baseline_seconds // block // 2)
    smoothed_blocks = [float(np.median(blocks[max(0, i - half): i + half + 1])) for i in range(len(blocks))]
    centers = np.arange(len(blocks)) * block + block / 2
    baseline = np.interp(np.arange(n), centers, smoothed_blocks)
    excitement = np.clip(loudness - baseline, 0, None)
    # Silence (dead air, BRB screens) should never look exciting.
    excitement[loudness < -50] = 0
    kernel = np.ones(smooth_seconds) / smooth_seconds
    return np.convolve(excitement, kernel, mode="same")


def combine_signals(audio: np.ndarray, chat: np.ndarray | None, chat_weight: float = 1.2,
                    viewer_clips: np.ndarray | None = None, clips_weight: float = 2.0) -> np.ndarray:
    """Put audio, chat and viewer-clip signals on the same scale and add them."""
    def norm(x: np.ndarray) -> np.ndarray:
        # Scale by the typical size of the non-zero part, so rare spikes don't scale to zero.
        active = x[x > 1e-6]
        scale = float(np.percentile(active, 90)) if active.size else 0.0
        return x / scale if scale > 1e-6 else np.zeros_like(x)

    combined = norm(audio.astype(np.float32))
    if chat is not None and chat.size:
        c = np.zeros_like(combined)
        n = min(len(c), len(chat))
        c[:n] = chat[:n]
        combined = combined + chat_weight * norm(c)
    if viewer_clips is not None and viewer_clips.size and viewer_clips.any():
        v = np.zeros_like(combined)
        n = min(len(v), len(viewer_clips))
        v[:n] = viewer_clips[:n]
        combined = combined + clips_weight * norm(v)
    return combined


def pick_windows(excitement: np.ndarray, count: int, window: int, skip_start: int = 120,
                 skip_end: int = 60, min_gap: int = 30, step: int = 5) -> list[tuple[float, float, float]]:
    """Greedy pick of the highest-scoring non-overlapping windows: (start, end, score)."""
    n = len(excitement)
    last_start = n - skip_end - window
    if last_start <= 0:
        # Short VOD: just use the whole thing as one window.
        return [(0.0, float(min(n, window)), float(excitement.mean()) if n else 0.0)]
    first_start = min(skip_start, max(0, last_start))
    weighted = excitement.astype(np.float64) ** 1.5
    csum = np.concatenate([[0.0], np.cumsum(weighted)])
    starts = np.arange(first_start, last_start + 1, step)
    scores = (csum[starts + window] - csum[starts]) / window
    order = np.argsort(-scores)
    chosen: list[tuple[float, float, float]] = []
    for idx in order:
        s = int(starts[idx])
        if all(s + window + min_gap <= cs or s >= ce + min_gap for cs, ce, _ in chosen):
            chosen.append((float(s), float(s + window), float(scores[idx])))
            if len(chosen) >= count:
                break
    return sorted(chosen)


def _is_boundary_before(words: list[Word], i: int) -> bool:
    if i == 0:
        return True
    gap = words[i]["s"] - words[i - 1]["e"]
    return gap >= 0.5 or words[i - 1]["w"].endswith((".", "?", "!"))


def snap_start(words: list[Word], target: float, tolerance: float = 8.0) -> float:
    """Move ``target`` (relative seconds) to the nearest sentence start within ``tolerance``."""
    best, best_dist = target, tolerance + 1
    for i, w in enumerate(words):
        if abs(w["s"] - target) <= tolerance and _is_boundary_before(words, i):
            dist = abs(w["s"] - target)
            if dist < best_dist:
                best, best_dist = max(0.0, w["s"] - 0.15), dist
    return best


def snap_end(words: list[Word], target: float, tolerance: float = 8.0) -> float:
    best, best_dist = target, tolerance + 1
    for i, w in enumerate(words):
        is_end = i == len(words) - 1 or _is_boundary_before(words, i + 1)
        if is_end and abs(w["e"] - target) <= tolerance:
            dist = abs(w["e"] - target)
            if dist < best_dist:
                best, best_dist = w["e"] + 0.35, dist
    return best


def words_in_range(words: list[Word], start: float, end: float) -> list[Word]:
    """Words within [start, end] (relative to the same origin), re-based to ``start``."""
    return [{"w": w["w"], "s": round(w["s"] - start, 2), "e": round(min(w["e"], end) - start, 2)}
            for w in words if w["s"] >= start - 0.05 and w["s"] < end]


def heuristic_plans(candidates: list[Candidate], count: int, min_len: int, max_len: int) -> list[ClipPlan]:
    target_len = (min_len + max_len) / 2
    ranked = []
    for c in candidates:
        region = c.end - c.start
        talk = len(c.words) / max(region, 1)  # words per second; dead air makes bad clips
        ranked.append((c.score * (0.5 + min(talk, 3.0) / 3.0), c))
    ranked.sort(key=lambda x: -x[0])
    plans = []
    for score, c in ranked[:count]:
        region = c.end - c.start
        rel_start = snap_start(c.words, max(0.0, (region - target_len) / 2))
        rel_end = snap_end(c.words, rel_start + target_len)
        rel_end = min(max(rel_end, rel_start + min_len), rel_start + max_len, region)
        plans.append(ClipPlan(
            start=c.start + rel_start, end=c.start + rel_end,
            title=f"Highlight at {_hms(c.start + rel_start)}",
            reason=f"Loud/hype moment (excitement score {c.score:.1f})",
            score=score, words=words_in_range(c.words, rel_start, rel_end),
        ))
    return plans


SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "start_offset": {"type": "number", "description": "Seconds from the candidate's start"},
                    "end_offset": {"type": "number", "description": "Seconds from the candidate's start"},
                    "title": {"type": "string"},
                    "hook_text": {"type": "string",
                                  "description": "On-screen hook for the first 3-4 seconds: max 7 words, no emoji, "
                                                 "teases the payoff without spoiling it"},
                    "reason": {"type": "string"},
                    "virality": {"type": "integer", "description": "1-10 estimate of short-form potential"},
                },
                "required": ["candidate_id", "start_offset", "end_offset", "title", "hook_text", "reason", "virality"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


async def llm_plans(candidates: list[Candidate], count: int, min_len: int, max_len: int,
                    streamer: str, vod_title: str, learned_patterns: str = "") -> list[ClipPlan]:
    system = (
        "You are an expert short-form video editor who turns livestream VODs into TikTok / Reels / Shorts clips "
        "for streamer fan pages. You pick self-contained moments that hook a scrolling viewer in the first 3 "
        "seconds (a reaction, a bold claim, a question, chaos starting) and pay off before the end: funny moments, "
        "rage, clutch plays, drama, hot takes, wholesome chat interactions, storytimes with a punchline. Avoid "
        "moments that need prior context, dead air, reading long chat lists, ads/BRB screens, and anything that "
        "only works visually if the transcript gives no hint. Clips must be tight: every few seconds something "
        "should happen or be said, with no slow middle stretch. Use the shortest length that keeps the setup and "
        "the payoff; if a moment only works long, cut where it starts to drag rather than padding it. Start each "
        "clip on the beginning of a sentence, ideally right on the hook, and end right after the payoff. When chat "
        "reaction is given, a big spike means viewers loved that moment, and viewer clips with lots of views are "
        "the strongest sign of all."
    )
    blocks = []
    for c in candidates:
        blocks.append(
            f"### Candidate {c.id}  (VOD {_hms(c.start)}–{_hms(c.end)}, length {c.end - c.start:.0f}s, "
            f"excitement {c.score:.1f})\n"
            + (f"Chat reaction: {c.chat}\n" if c.chat else "")
            + (words_to_text(c.words) or "(no speech detected)")
        )
    prompt = (
        f"Streamer: {streamer}\nVOD title: {vod_title}\n\n"
        + (f"What has been working for viral clips recently:\n{learned_patterns}\n\n" if learned_patterns else "")
        + f"Below are {len(candidates)} candidate regions with timestamped transcripts (timestamps are relative to "
        f"the candidate start). Choose up to {count} of the best, non-overlapping clips. Each clip must be between "
        f"{min_len} and {max_len} seconds long and lie inside its candidate region. Skip candidates that would not "
        f"perform; returning fewer clips is fine. Titles should be short, punchy captions (no hashtags).\n\n"
        + "\n\n".join(blocks)
    )
    answer = await llm.ask_json(system, [llm.text_block(prompt)], SELECT_SCHEMA)
    by_id = {c.id: c for c in candidates}
    plans: list[ClipPlan] = []
    for item in answer.get("clips", [])[:count]:
        c = by_id.get(item["candidate_id"])
        if not c:
            continue
        region = c.end - c.start
        rel_start = max(0.0, min(float(item["start_offset"]), region - min_len))
        rel_end = float(item["end_offset"])
        rel_end = min(max(rel_end, rel_start + min_len), rel_start + max_len, region)
        if any(abs(p.start - (c.start + rel_start)) < min_len / 2 for p in plans):
            continue
        plans.append(ClipPlan(
            start=c.start + rel_start, end=c.start + rel_end, title=item["title"].strip()[:120],
            reason=item["reason"].strip()[:500], score=float(item["virality"]),
            words=words_in_range(c.words, rel_start, rel_end), hook_text=(item.get("hook_text") or "").strip()[:80],
        ))
    return plans


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
