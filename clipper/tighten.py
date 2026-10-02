"""Jump-cut dead air out of a clip so there are no slow stretches.

A pause is only cut when nobody is speaking AND the whole audio mix (game sound included)
is quiet, so silent-but-intense gameplay is left alone. Clips never drop below the minimum
length (TikTok only pays for videos over 1 minute).
"""
from __future__ import annotations

import numpy as np

from shared.transcribe import Word

Segment = tuple[float, float]


def plan_keep_segments(words: list[Word], duration: float, loudness: np.ndarray | None, min_total: float,
                       min_gap: float = 1.5, pad: float = 0.25) -> list[Segment]:
    """Return the [start, end] pieces of the clip to keep (relative seconds)."""
    if loudness is None or len(loudness) == 0 or len(words) < 2:
        return [(0.0, duration)]
    quiet_level = float(np.median(loudness)) + 1.5
    cuts: list[Segment] = []
    for prev, nxt in zip(words, words[1:]):
        a, b = prev["e"], nxt["s"]
        if b - a < min_gap:
            continue
        seconds = loudness[int(a): int(np.ceil(b)) + 1]
        if seconds.size and float(seconds.max()) <= quiet_level:
            cuts.append((a + pad, b - pad))
    # Biggest pauses first, stop before the clip gets too short.
    total = duration
    accepted: list[Segment] = []
    for cut in sorted(cuts, key=lambda c: c[0] - c[1]):
        length = cut[1] - cut[0]
        if total - length >= min_total:
            accepted.append(cut)
            total -= length
    if not accepted:
        return [(0.0, duration)]
    keep: list[Segment] = []
    pos = 0.0
    for start, end in sorted(accepted):
        keep.append((pos, start))
        pos = end
    keep.append((pos, duration))
    return [(round(s, 3), round(e, 3)) for s, e in keep if e - s > 0.05]


def remap_time(t: float, keep: list[Segment]) -> float:
    """Where time ``t`` of the original clip lands in the tightened clip."""
    out = 0.0
    for start, end in keep:
        if t < start:
            return out
        if t <= end:
            return out + (t - start)
        out += end - start
    return out


def remap_words(words: list[Word], keep: list[Segment]) -> list[Word]:
    return [{"w": w["w"], "s": round(remap_time(w["s"], keep), 2), "e": round(remap_time(w["e"], keep), 2)}
            for w in words]


def kept_duration(keep: list[Segment]) -> float:
    return sum(e - s for s, e in keep)
