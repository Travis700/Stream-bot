"""/selftest: run every stage of the pipeline on a generated test video (no downloads needed).

Checks: ffmpeg, speech synthesis -> speech-to-text, face detector, the AI, dead-air cuts,
rendering with captions + hook text, and the Discord preview size. Produces a real clip
so you can see what the output looks like on your server.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from shared import llm, media
from shared.config import settings
from shared.transcribe import transcribe

from . import censor, facecam, render, tighten

SCRIPT = ("Okay chat, this is the clip bot self test. If you can read these captions, speech to text is working. "
          "Now wait for it. Three. Two. One. Let's go! That was insane!")


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    seconds: float


@dataclass
class SelfTestResult:
    steps: list[Step] = field(default_factory=list)
    video: Path | None = None

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)


def _make_test_video(work: Path) -> Path:
    """A 1920x1080 'stream' with a fake webcam box and synthetic speech (with a pause in it)."""
    speech = work / "speech.wav"
    if shutil.which("espeak-ng"):
        subprocess.run(["espeak-ng", "-s", "150", "-w", str(speech), SCRIPT], check=True, capture_output=True)
    else:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=300:duration=12",
                        str(speech)], check=True)
    out = work / "test_stream.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=20",
        "-f", "lavfi", "-i", "color=c=0x334455:size=480x270:rate=30:duration=20",
        "-i", str(speech),
        "-filter_complex", "[0:v][1:v]overlay=20:790[v];[2:a]adelay=1000|1000,apad=whole_dur=20[a]",
        "-map", "[v]", "-map", "[a]", "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out),
    ], check=True)
    return out


async def _timed(result: SelfTestResult, name: str, coro_or_fn, *args):
    started = time.monotonic()
    try:
        if asyncio.iscoroutinefunction(coro_or_fn):
            value, detail = await coro_or_fn(*args)
        else:
            value, detail = await asyncio.to_thread(coro_or_fn, *args)
        result.steps.append(Step(name, True, detail, time.monotonic() - started))
        return value
    except Exception as exc:  # noqa: BLE001
        result.steps.append(Step(name, False, f"{type(exc).__name__}: {str(exc)[:300]}", time.monotonic() - started))
        return None


async def run(max_preview_bytes: int) -> SelfTestResult:
    result = SelfTestResult()
    work = settings.work_dir / "selftest"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    def make():
        path = _make_test_video(work)
        voice = "synthetic voice" if shutil.which("espeak-ng") else "tone only (espeak-ng missing)"
        return path, f"20s test stream with {voice}"

    video = await _timed(result, "ffmpeg + test video", make)
    if video is None:
        return result

    def stt():
        words = transcribe(video)
        if not words:
            raise RuntimeError("no words recognised")
        return words, f"{len(words)} words with model '{settings.whisper_model}': " \
                      f"“{' '.join(w['w'] for w in words[:8])}…”"

    words = await _timed(result, "Speech-to-text", stt) or []

    def faces():
        detector = facecam.FaceDetector()
        kind = "YuNet" if detector.yunet is not None else ("Haar (fallback)" if detector.haar else "none")
        scene = facecam.analyse(video, detector)
        return scene, f"detector: {kind}; test video has no real face, so layout '{scene.kind}' is expected"

    scene = await _timed(result, "Face detector", faces)

    async def ai():
        if not settings.llm_available():
            return None, f"AI turned off (provider {settings.provider_for()})"
        schema = {"type": "object", "properties": {"hook_text": {"type": "string"}}, "required": ["hook_text"],
                  "additionalProperties": False}
        answer = await llm.ask_json("You write short on-screen hooks for streamer clips.",
                                    [llm.text_block(f"Transcript: {SCRIPT}\nWrite a hook of max 6 words.")],
                                    schema, effort="low", max_tokens=500)
        return answer["hook_text"], f"{settings.provider_for()} replied: “{answer['hook_text'][:80]}”"

    hook = await _timed(result, "AI", ai) or "SELF TEST: DID IT WORK?"

    def make_clip():
        duration = media.duration(video)
        loud = media.loudness_per_second(video)
        keep = tighten.plan_keep_segments(words, duration, loud, min_total=8)
        mapped = tighten.remap_words(words, keep)
        final = tighten.kept_duration(keep)
        out = work / "selftest_clip.mp4"
        used = render.render(video, out, scene or facecam.SceneLayout("nocam", 1920, 1080),
                             censor.mask_words(mapped), final, "auto", True, keep, hook_text=hook,
                             bleeps=censor.find_bleeps(mapped))
        size = media.video_size(out)
        return out, f"{size[0]}x{size[1]}, {final:.1f}s ({duration - final:.1f}s dead air cut), layout {used}"

    clip = await _timed(result, "Render (captions + hook + cuts)", make_clip)
    if clip is None:
        return result

    def preview():
        if clip.stat().st_size <= max_preview_bytes:
            return clip, f"{clip.stat().st_size / 1e6:.1f} MB, fits Discord as-is"
        small = render.make_preview(clip, work / "selftest_preview.mp4", media.duration(clip), max_preview_bytes)
        if small is None:
            raise RuntimeError("could not make a small enough preview")
        return small, f"{small.stat().st_size / 1e6:.1f} MB preview"

    result.video = await _timed(result, "Discord preview", preview)
    return result
