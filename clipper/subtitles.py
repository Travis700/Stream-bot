"""Burned-in TikTok-style captions: 1–3 words at a time, active word highlighted."""
from __future__ import annotations

import re
from pathlib import Path

from shared.transcribe import Word

MAX_WORDS = 3
MAX_CHARS = 16
PAUSE_BREAK = 0.45


def clean_word(text: str) -> str:
    text = re.sub(r"[^\w'?!%$#&@+*-]", "", text, flags=re.UNICODE)
    return text.upper()


def chunk_words(words: list[Word]) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        if not clean_word(word["w"]):
            continue
        if current:
            chars = sum(len(clean_word(w["w"])) + 1 for w in current) + len(clean_word(word["w"]))
            pause = word["s"] - current[-1]["e"]
            ends_sentence = current[-1]["w"].rstrip().endswith((".", "?", "!", ","))
            if len(current) >= MAX_WORDS or chars > MAX_CHARS or pause > PAUSE_BREAK or ends_sentence:
                chunks.append(current)
                current = []
        current.append(word)
    if current:
        chunks.append(current)
    return chunks


def ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def clean_hook(text: str, max_chars: int = 60) -> str:
    """Upper-case hook text without emoji (libass can't draw colour emoji)."""
    text = "".join(ch for ch in text if ord(ch) < 0x2190 or 0x3000 <= ord(ch) < 0xD800)
    text = " ".join(text.replace("{", "").replace("}", "").replace("\\", "").split()).upper()
    return text[:max_chars].strip()


def build_ass(words: list[Word], center_y: int, font: str, duration: float,
              width: int = 1080, height: int = 1920, font_size: int = 80,
              hook_text: str = "", hook_y: int = 300, hook_seconds: float = 0.0) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,6,3,5,60,60,0,1
Style: Hook,{font},72,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,16,0,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    highlight = r"{\c&H0000E6FF&}"  # yellow (ASS colours are BGR)
    normal = r"{\c&H00FFFFFF&}"
    lines = []
    hook = clean_hook(hook_text)
    if hook and hook_seconds > 0:
        # Black text on a white box, wrapped onto up to 2-3 lines, pops in.
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(hook_seconds, duration))},Hook,,0,0,0,,"
                     f"{{\\q0\\pos({width // 2},{hook_y})\\fscx90\\fscy90\\t(0,120,\\fscx100\\fscy100)}}{hook}")
    chunks = chunk_words(words)
    for ci, chunk in enumerate(chunks):
        next_start = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else duration
        chunk_end = min(chunk[-1]["e"] + 0.25, next_start, duration)
        tokens = [clean_word(w["w"]) for w in chunk]
        for wi, word in enumerate(chunk):
            start = word["s"]
            end = chunk[wi + 1]["s"] if wi + 1 < len(chunk) else chunk_end
            if end <= start:
                continue
            parts = [(highlight + t + normal) if i == wi else t for i, t in enumerate(tokens)]
            pop = r"{\fscx112\fscy112\t(0,90,\fscx100\fscy100)}" if wi == 0 else ""
            text = pop + " ".join(parts)
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,"
                         f"{{\\pos({width // 2},{center_y})}}{text}")
    return header + "\n".join(lines) + "\n"


def write_ass(path: Path, words: list[Word], center_y: int, font: str, duration: float,
              hook_text: str = "", hook_y: int = 300, hook_seconds: float = 0.0) -> Path:
    path.write_text(build_ass(words, center_y, font, duration, hook_text=hook_text, hook_y=hook_y,
                              hook_seconds=hook_seconds), encoding="utf-8")
    return path
