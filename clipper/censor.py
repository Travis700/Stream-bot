"""Bleep or mute slurs. TikTok/Instagram often limit the reach of clips with slurs in them.

Only slurs are filtered by default (not normal swearing). Add your own words with the
BLEEP_WORDS setting, e.g. BLEEP_WORDS=fuck,shit to make clips fully clean.
"""
from __future__ import annotations

import re

from shared.config import settings
from shared.transcribe import Word

# Whole-word patterns, matched against the lower-cased word with punctuation removed.
DEFAULT_PATTERNS = [
    r"n[i1!]gg+(a|ah|as|az|er|ers|uh|uhs)?",
    r"f[a@]g+(s|got|gots|gy)?",
    r"retard(s|ed)?",
    r"tranny|trannies",
    r"k[iy]kes?",
    r"ch[i1]nks?",
    r"sp[i1]c?ks?",
    r"wetbacks?",
    r"g[o0]{2}ks?",
    r"beaners?",
    r"coons?",
]


def _normalise(text: str) -> str:
    """Lower-case, drop surrounding punctuation, keep leetspeak symbols inside the word (n!gga, f@g)."""
    text = re.sub(r"^\W+|\W+$", "", text.lower())
    return re.sub(r"[^\w!@]", "", text)


def build_matcher(extra_words: str | None = None) -> re.Pattern[str]:
    extra = [re.escape(w.strip().lower()) + "s?" for w in (extra_words or "").split(",") if w.strip()]
    return re.compile(r"^(" + "|".join(DEFAULT_PATTERNS + extra) + r")$")


def find_bleeps(words: list[Word], matcher: re.Pattern[str] | None = None, pad: float = 0.05
                ) -> list[tuple[float, float]]:
    """Time ranges (in the words' timeline) to bleep."""
    matcher = matcher or build_matcher(settings.bleep_words)
    ranges = []
    for w in words:
        if matcher.match(_normalise(w["w"])):
            ranges.append((max(0.0, w["s"] - pad), w["e"] + pad))
    return ranges


def mask_words(words: list[Word], matcher: re.Pattern[str] | None = None) -> list[Word]:
    """Same words, with filtered ones shown as e.g. 'N****' in captions."""
    matcher = matcher or build_matcher(settings.bleep_words)
    out = []
    for w in words:
        text = w["w"]
        if matcher.match(_normalise(text)):
            letters = re.sub(r"\W", "", text)
            text = (letters[:1] + "*" * max(2, len(letters) - 1)) if letters else "****"
        out.append({**w, "w": text})
    return out


def audio_filter(ranges: list[tuple[float, float]], mode: str) -> tuple[str, str]:
    """Return (mute expression for a volume filter, beep volume expression) for the ranges."""
    if not ranges or mode == "off":
        return "", ""
    expr = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e in ranges)
    return expr, (expr if mode == "beep" else "")
