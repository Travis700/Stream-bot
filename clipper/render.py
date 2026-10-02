"""Turn a 16:9 stream clip into a 1080x1920 vertical video with captions (CPU x264)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from shared.config import settings
from shared.locks import heavy_sync
from shared.media import probe, run
from shared.transcribe import Word

from . import censor, tighten
from .facecam import Box, SceneLayout
from .subtitles import write_ass

log = logging.getLogger(__name__)

OUT_W, OUT_H = 1080, 1920
LAYOUTS = ("auto", "split", "fullcam", "fit")


@dataclass
class RenderPlan:
    layout: str
    filter_graph: str
    caption_y: int
    hook_y: int


def _even(v: float) -> int:
    v = int(round(v))
    return v - v % 2


def fit_crop(src_w: int, src_h: int, aspect: float) -> tuple[int, int]:
    """Largest (w, h) with w/h == aspect that fits in the source."""
    w, h = src_h * aspect, src_h
    if w > src_w:
        w, h = src_w, src_w / aspect
    return _even(w), _even(h)


def crop_box_to_aspect(box: Box, aspect: float) -> Box:
    """Trim a box (keeping its center) so w/h == aspect."""
    w, h = box.w, box.h
    if w / h > aspect:
        w = h * aspect
    else:
        h = w / aspect
    return Box(_even(box.cx - w / 2), _even(box.cy - h / 2), _even(w), _even(h))


def game_crop_x(src_w: int, crop_w: int, cam: Box | None) -> int:
    """Pick the gameplay crop's x so it keeps the action centred but avoids the facecam."""
    center = (src_w - crop_w) // 2
    if cam is None:
        return _even(center)
    options = {center, cam.x + cam.w, cam.x - crop_w}
    options = {min(max(0, x), src_w - crop_w) for x in options}

    def overlap(x: int) -> int:
        return max(0, min(x + crop_w, cam.x + cam.w) - max(x, cam.x))

    # Only move off-centre when the cam would cover a big chunk of the crop.
    if overlap(center) < crop_w * 0.15:
        return _even(center)
    best = min(options, key=lambda x: (overlap(x), abs(x - center)))
    return _even(best)


def crop_x_expression(keys: list[tuple[float, float]], crop_w: int, src_w: int) -> str:
    """ffmpeg expression for a crop's x that pans linearly between keyframes [(t, centre_x)]."""
    xs = [min(max(cx - crop_w / 2, 0), src_w - crop_w) for _, cx in keys]
    ts = [t for t, _ in keys]
    expr = f"{xs[-1]:.1f}"
    for i in range(len(keys) - 2, -1, -1):
        t0, t1, x0, x1 = ts[i], ts[i + 1], xs[i], xs[i + 1]
        if t1 <= t0:
            continue
        segment = f"{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.2f})/{t1 - t0:.2f}"
        expr = f"if(lt(t,{t1:.2f}),{segment},{expr})"
    return f"if(lt(t,{ts[0]:.2f}),{xs[0]:.1f},{expr})"


def plan_layout(scene: SceneLayout, requested: str = "auto",
                track: list[tuple[float, float]] | None = None) -> RenderPlan:
    src_w, src_h = scene.width, scene.height
    layout = requested
    if layout == "auto":
        layout = {"gaming": "split", "fullcam": "fullcam"}.get(scene.kind, "fit")
    if layout == "split" and scene.cam is None:
        layout = "fit"
    if layout == "fullcam" and scene.face is None:
        layout = "fit"

    if layout == "split":
        cam = scene.cam
        assert cam is not None
        top_h = _even(min(max(OUT_W * cam.h / cam.w, 560), 800))
        bottom_h = OUT_H - top_h
        cam_crop = crop_box_to_aspect(cam, OUT_W / top_h).clamp(src_w, src_h)
        gw, gh = fit_crop(src_w, src_h, OUT_W / bottom_h)
        gx = game_crop_x(src_w, gw, cam)
        gy = _even((src_h - gh) / 2)
        graph = (
            f"[src]split=2[s1][s2];"
            f"[s1]crop={cam_crop.w}:{cam_crop.h}:{cam_crop.x}:{cam_crop.y},scale={OUT_W}:{top_h},setsar=1[cam];"
            f"[s2]crop={gw}:{gh}:{gx}:{gy},scale={OUT_W}:{bottom_h},setsar=1[game];"
            f"[cam][game]vstack=inputs=2[stack]"
        )
        # Hook sits in the top of the gameplay, under the captions on the seam.
        return RenderPlan("split", graph, caption_y=top_h, hook_y=top_h + 230)

    if layout == "fullcam":
        face = scene.face
        assert face is not None
        cw, ch = fit_crop(src_w, src_h, OUT_W / OUT_H)
        if track and len(track) >= 2:
            x = f"'{crop_x_expression(track, cw, src_w)}'"  # pan to follow the streamer
        else:
            x = str(_even(min(max(face.cx - cw / 2, 0), src_w - cw)))
        graph = f"[src]crop={cw}:{ch}:{x}:0,scale={OUT_W}:{OUT_H},setsar=1[stack]"
        return RenderPlan("fullcam", graph, caption_y=int(OUT_H * 0.72), hook_y=330)

    # fit: zoomed 4:3 centre of the stream over a blurred, darkened copy of itself.
    fw, fh = fit_crop(src_w, src_h, 4 / 3)
    fx, fy = _even((src_w - fw) / 2), _even((src_h - fh) / 2)
    fg_h = _even(OUT_W * 3 / 4)
    fg_y = _even((OUT_H - fg_h) / 2)
    graph = (
        f"[src]split=2[a][b];"
        f"[a]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H},boxblur=24:2,eq=brightness=-0.12,setsar=1[bg];"
        f"[b]crop={fw}:{fh}:{fx}:{fy},scale={OUT_W}:{fg_h},setsar=1[fg];"
        f"[bg][fg]overlay=0:{fg_y}[stack]"
    )
    return RenderPlan("fit", graph, caption_y=min(OUT_H - 220, fg_y + fg_h + 150), hook_y=max(260, fg_y - 170))


def _filter_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def source_graph(keep: list[tuple[float, float]], has_audio: bool) -> str:
    """Filters that cut the kept pieces out of input 0 and join them into [src] (and [asrc])."""
    if len(keep) == 1:
        s, e = keep[0]
        graph = f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[src]"
        if has_audio:
            graph += f";[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[asrc]"
        return graph
    parts, inputs = [], ""
    for i, (s, e) in enumerate(keep):
        parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        inputs += f"[v{i}]"
        if has_audio:
            parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
            inputs += f"[a{i}]"
    outputs = "[src][asrc]" if has_audio else "[src]"
    parts.append(f"{inputs}concat=n={len(keep)}:v=1:a={int(has_audio)}{outputs}")
    return ";".join(parts)


def render(source: Path, out: Path, scene: SceneLayout, words: list[Word], duration: float,
           layout: str = "auto", subtitles: bool = True, keep: list[tuple[float, float]] | None = None,
           hook_text: str = "", bleeps: list[tuple[float, float]] | None = None) -> str:
    """Render the vertical clip. Returns the layout actually used.

    ``keep`` lists the pieces of ``source`` to keep (jump cuts). ``words``, ``duration`` and
    ``bleeps`` (time ranges to bleep/mute) must already be on the tightened timeline.
    """
    track = None
    if scene.track:
        # The face track is on the source timeline; move it onto the cut timeline.
        segments = keep or [(0.0, duration)]
        inside = [(t, x) for t, x in scene.track if any(s <= t <= e for s, e in segments)]
        track = [(tighten.remap_time(t, segments), x) for t, x in inside]
    plan = plan_layout(scene, layout, track)
    has_audio = any(st.get("codec_type") == "audio" for st in probe(source)["streams"])
    graph = source_graph(keep or [(0.0, duration)], has_audio) + ";" + plan.filter_graph
    last = "[stack]"
    hook_seconds = settings.hook_text_seconds if hook_text else 0.0
    if (subtitles and words) or hook_seconds:
        ass = write_ass(out.with_suffix(".ass"), words if subtitles else [], plan.caption_y,
                        settings.subtitle_font, duration, hook_text=hook_text, hook_y=plan.hook_y,
                        hook_seconds=hook_seconds)
        fonts = Path(settings.fonts_dir).resolve()
        graph += f";[stack]ass=filename='{_filter_path(ass)}':fontsdir='{_filter_path(fonts)}'[subbed]"
        last = "[subbed]"
    graph += f";{last}fps={settings.render_fps},format=yuv420p[v]"

    inputs = ["-i", str(source)]
    audio_args: list[str] = []
    if has_audio:
        mute_expr, beep_expr = censor.audio_filter(bleeps or [], settings.bleep_mode)
        chain = f"volume=enable='{mute_expr}':volume=0," if mute_expr else ""
        graph += f";[asrc]{chain}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[anorm]"
        if beep_expr:
            inputs += ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000"]
            graph += (f";[1:a]atrim=0:{duration:.3f},volume='0.25*({beep_expr})':eval=frame[beep]"
                      f";[anorm][beep]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            graph += ";[anorm]anull[aout]"
        audio_args = ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
    with heavy_sync("render"):
        run([
            "ffmpeg", "-v", "error", "-y", *inputs,
            "-filter_complex", graph, "-map", "[v]", *audio_args,
            "-c:v", "libx264", "-preset", settings.render_preset, "-crf", "20", "-profile:v", "high",
            "-movflags", "+faststart", str(out),
        ])
    return plan.layout


def make_preview(source: Path, out: Path, duration: float, max_bytes: int) -> Path | None:
    """Small 540x960 copy that fits Discord's upload limit (for previews and the rater)."""
    audio_kbps = 64
    total_kbps = int(max_bytes * 8 * 0.92 / max(duration, 1) / 1000)
    video_kbps = total_kbps - audio_kbps
    if video_kbps < 250:
        return None
    with heavy_sync("preview"):
        run([
        "ffmpeg", "-v", "error", "-y", "-i", str(source), "-vf", "scale=540:960",
        "-c:v", "libx264", "-preset", settings.render_preset, "-b:v", f"{video_kbps}k",
        "-maxrate", f"{int(video_kbps * 1.2)}k", "-bufsize", f"{video_kbps * 2}k",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-movflags", "+faststart", str(out),
        ])
    if out.stat().st_size > max_bytes:
        out.unlink(missing_ok=True)
        return None
    return out
