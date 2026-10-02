"""Locate a streamer's facecam overlay in a gameplay video (CPU, OpenCV).

Approach: sample frames across the clip, run a face detector on each, keep the face that
stays in the same place in most frames (a webcam overlay doesn't move; faces inside the
game do). Then grow the face box to the webcam overlay's rectangle, snapping to the
overlay's border, which shows up as a strong edge that stays put while the game changes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from shared.config import settings

log = logging.getLogger(__name__)


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def clamp(self, width: int, height: int) -> "Box":
        x = max(0, min(self.x, width - 2))
        y = max(0, min(self.y, height - 2))
        w = max(2, min(self.w, width - x))
        h = max(2, min(self.h, height - y))
        return Box(x, y, w - w % 2, h - h % 2)


@dataclass
class SceneLayout:
    kind: str                 # "gaming" (facecam overlay), "fullcam" (just chatting / IRL), "nocam"
    width: int
    height: int
    face: Box | None = None   # median face box
    cam: Box | None = None    # estimated facecam overlay (gaming only)
    confidence: float = 0.0


class FaceDetector:
    def __init__(self) -> None:
        self.yunet = None
        model = Path(settings.yunet_model)
        if model.exists() and hasattr(cv2, "FaceDetectorYN"):
            self.yunet = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.6, 0.3, 5000)
        elif hasattr(cv2, "CascadeClassifier"):
            log.warning("YuNet model not found at %s; falling back to Haar cascade (less accurate)", model)
            self.haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        else:
            log.error("No face detector available; facecam layouts are disabled")
            self.haar = None

    def detect(self, frame: np.ndarray) -> list[tuple[Box, float]]:
        h, w = frame.shape[:2]
        if self.yunet is not None:
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(frame)
            if faces is None:
                return []
            return [(Box(int(f[0]), int(f[1]), int(f[2]), int(f[3])), float(f[-1])) for f in faces]
        if self.haar is None:
            return []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        found = self.haar.detectMultiScale(gray, 1.1, 6, minSize=(max(24, h // 30), max(24, h // 30)))
        return [(Box(int(x), int(y), int(fw), int(fh)), 0.8) for x, y, fw, fh in found]


def sample_frames(video: Path, count: int = 16) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    frames = []
    if total <= 0:
        ok, frame = cap.read()
        while ok and len(frames) < count:
            frames.append(frame)
            for _ in range(30):
                ok, frame = cap.read()
    else:
        for i in range(count):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int((i + 0.5) * total / count))
            ok, frame = cap.read()
            if ok:
                frames.append(frame)
    cap.release()
    return frames


def _cluster_faces(detections: list[list[tuple[Box, float]]], width: int, height: int) -> tuple[list[Box], int]:
    """Return the largest group of face boxes that sit in (roughly) the same spot."""
    flat = [box for frame in detections for box, _ in frame]
    best: list[Box] = []
    for anchor in flat:
        group = [b for b in flat
                 if abs(b.cx - anchor.cx) < width * 0.06 and abs(b.cy - anchor.cy) < height * 0.08
                 and 0.6 < b.h / max(anchor.h, 1) < 1.6]
        if len(group) > len(best):
            best = group
    frames_with = sum(1 for frame in detections
                      if any(any(b is g for g in best) for b, _ in frame))
    return best, frames_with


def _median_box(boxes: list[Box]) -> Box:
    return Box(int(np.median([b.x for b in boxes])), int(np.median([b.y for b in boxes])),
               int(np.median([b.w for b in boxes])), int(np.median([b.h for b in boxes])))


def _persistent_edges(frames: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Average absolute x/y gradients over all frames; static overlay borders stay strong."""
    gx_acc = gy_acc = None
    for frame in frames:
        if self.haar is None:
            return []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
        gy = np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
        gx_acc = gx if gx_acc is None else np.minimum(gx_acc, gx)  # min = edge present in EVERY frame
        gy_acc = gy if gy_acc is None else np.minimum(gy_acc, gy)
    return gx_acc, gy_acc  # type: ignore[return-value]


def _snap(profile: np.ndarray, lo: int, hi: int, fallback: int) -> int:
    lo, hi = max(0, lo), min(len(profile) - 1, hi)
    if hi <= lo:
        return fallback
    segment = profile[lo:hi]
    idx = int(np.argmax(segment))
    # Require a clear edge, otherwise keep the geometric estimate.
    if segment[idx] > max(8.0, 3.0 * float(np.median(profile))):
        return lo + idx
    return fallback


def estimate_cam_box(face: Box, frames: list[np.ndarray], width: int, height: int) -> Box:
    # Typical webcam overlay: face ~1/3 of the overlay width, head in the upper-middle.
    est_w = face.w * 3.4
    est_h = max(est_w * 9 / 16, face.h * 2.4)
    est = Box(int(face.cx - est_w / 2), int(face.cy - est_h * 0.45), int(est_w), int(est_h))
    gx, gy = _persistent_edges(frames)
    rows = slice(max(0, face.y), min(height, face.y + face.h))
    cols = slice(max(0, face.x), min(width, face.x + face.w))
    col_profile = gx[rows, :].mean(axis=0)   # vertical borders
    row_profile = gy[:, cols].mean(axis=1)   # horizontal borders
    left = _snap(col_profile, int(face.x - face.w * 2.6), int(face.x - face.w * 0.3), est.x)
    right = _snap(col_profile, int(face.x + face.w * 1.3), int(face.x + face.w * 3.6), est.x + est.w)
    top = _snap(row_profile, int(face.y - face.h * 1.8), int(face.y - face.h * 0.2), est.y)
    bottom = _snap(row_profile, int(face.y + face.h * 1.2), int(face.y + face.h * 3.2), est.y + est.h)
    box = Box(left, top, right - left, bottom - top)
    # Overlays touching the frame edge have no border on that side: extend to the edge if close.
    if box.x < width * 0.03:
        box = Box(0, box.y, box.w + box.x, box.h)
    if box.y < height * 0.03:
        box = Box(box.x, 0, box.w, box.h + box.y)
    if width - (box.x + box.w) < width * 0.03:
        box = Box(box.x, box.y, width - box.x, box.h)
    if height - (box.y + box.h) < height * 0.03:
        box = Box(box.x, box.y, box.w, height - box.y)
    aspect = box.w / max(box.h, 1)
    if not 0.8 < aspect < 2.4:  # snapping went wrong; trust the estimate
        box = est
    return box.clamp(width, height)


def analyse(video: Path, detector: FaceDetector | None = None) -> SceneLayout:
    frames = sample_frames(video)
    if not frames:
        raise RuntimeError(f"Could not read frames from {video}")
    height, width = frames[0].shape[:2]
    detector = detector or FaceDetector()
    detections = [detector.detect(f) for f in frames]
    group, frames_with = _cluster_faces(detections, width, height)
    confidence = frames_with / len(frames)
    if not group or confidence < 0.35:
        return SceneLayout("nocam", width, height, confidence=confidence)
    face = _median_box(group)
    if face.h > height * 0.2:
        return SceneLayout("fullcam", width, height, face=face, confidence=confidence)
    cam = estimate_cam_box(face, frames, width, height)
    return SceneLayout("gaming", width, height, face=face, cam=cam, confidence=confidence)
