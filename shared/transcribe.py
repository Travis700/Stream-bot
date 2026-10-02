"""CPU speech-to-text with faster-whisper (int8, no GPU needed)."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TypedDict

from .config import settings
from .locks import heavy_sync
from .media import load_audio

log = logging.getLogger(__name__)

_model = None
_lock = threading.Lock()


class Word(TypedDict):
    w: str   # word text
    s: float  # start (seconds)
    e: float  # end (seconds)


def _get_model(name: str | None = None):
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            model_name = name or settings.whisper_model
            log.info("Loading whisper model %s (cpu/int8)", model_name)
            _model = WhisperModel(model_name, device="cpu", compute_type="int8",
                                  cpu_threads=settings.whisper_threads,
                                  download_root=str(settings.data_dir / "models"))
        return _model


def transcribe(path: Path, start: float = 0.0, length: float | None = None) -> list[Word]:
    """Transcribe a slice of a media file; timestamps are relative to ``start``."""
    audio = load_audio(path, start, length)
    if audio.size == 0:
        return []
    model = _get_model()
    with _lock, heavy_sync("transcribe"):  # one at a time; the model already uses every core
        segments, _info = model.transcribe(audio, word_timestamps=True, vad_filter=True, beam_size=1,
                                           condition_on_previous_text=False)
        words: list[Word] = []
        for seg in segments:
            for word in seg.words or []:
                text = word.word.strip()
                if text:
                    words.append({"w": text, "s": round(word.start, 2), "e": round(word.end, 2)})
    return words


def words_to_text(words: list[Word], offset: float = 0.0, stamp_every: float = 10.0) -> str:
    """Readable transcript with a [mm:ss] stamp roughly every ``stamp_every`` seconds."""
    parts: list[str] = []
    next_stamp = -1.0
    for word in words:
        t = word["s"] + offset
        if t >= next_stamp:
            m, s = divmod(int(t), 60)
            h, m = divmod(m, 60)
            parts.append(f"\n[{h}:{m:02d}:{s:02d}]" if h else f"\n[{m:02d}:{s:02d}]")
            next_stamp = t + stamp_every
        parts.append(word["w"])
    return " ".join(parts).strip()
