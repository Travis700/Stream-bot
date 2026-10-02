"""ffmpeg / ffprobe helpers (blocking)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000


class FFmpegError(RuntimeError):
    pass


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace")[-1500:]
        raise FFmpegError(f"{cmd[0]} failed ({proc.returncode}):\n{tail}")


def probe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return json.loads(proc.stdout)


def video_size(path: Path) -> tuple[int, int]:
    for stream in probe(path)["streams"]:
        if stream.get("codec_type") == "video":
            return int(stream["width"]), int(stream["height"])
    raise FFmpegError(f"No video stream in {path}")


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def loudness_per_second(path: Path) -> np.ndarray:
    """RMS loudness in dBFS for every second of audio, streamed so long VODs fit in memory."""
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    values: list[float] = []
    chunk_bytes = SAMPLE_RATE * 2
    while True:
        buf = proc.stdout.read(chunk_bytes)
        if not buf:
            break
        samples = np.frombuffer(buf[: len(buf) - len(buf) % 2], dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
        values.append(20 * np.log10(max(rms, 1e-5)))
    proc.wait()
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg could not decode audio from {path}")
    return np.array(values, dtype=np.float32)


def load_audio(path: Path, start: float = 0.0, length: float | None = None) -> np.ndarray:
    """Decode a slice of audio to 16 kHz mono float32 (whisper's input format)."""
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(start, 0):.3f}"]
    if length is not None:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.decode(errors="replace")[-800:])
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def extract_frames(path: Path, times: list[float], out_dir: Path, width: int = 768) -> list[Path]:
    """Save JPEG frames at the given timestamps (seconds)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, t in enumerate(times):
        out = out_dir / f"frame_{i:02d}.jpg"
        run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-q:v", "4", str(out)])
        if out.exists():
            frames.append(out)
    return frames
