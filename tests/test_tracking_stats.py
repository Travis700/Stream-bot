import shutil
import subprocess
import time

import pytest

from clipper import facecam, render, stats
from clipper.facecam import Box, SceneLayout
from shared import media
from shared.db import Database


def ffmpeg_expr(expr: str, t: float) -> float:
    """Evaluate the subset of ffmpeg's expression language that crop_x_expression uses."""
    py = expr.replace("if(", "_if(").replace("lt(", "_lt(")
    return eval(py, {"_if": lambda c, a, b: a if c else b, "_lt": lambda a, b: a < b, "t": t})  # noqa: S307


def test_crop_expression_pans_between_keyframes():
    keys = [(0.0, 500.0), (2.0, 700.0), (4.0, 700.0)]
    expr = render.crop_x_expression(keys, crop_w=600, src_w=1920)
    assert ffmpeg_expr(expr, 0.0) == pytest.approx(200)   # centre 500 -> left edge 200
    assert ffmpeg_expr(expr, 1.0) == pytest.approx(300)   # halfway between 200 and 400
    assert ffmpeg_expr(expr, 3.0) == pytest.approx(400)
    assert ffmpeg_expr(expr, 99.0) == pytest.approx(400)  # holds the last position


def test_crop_expression_clamps_to_frame():
    expr = render.crop_x_expression([(0.0, 50.0), (1.0, 1900.0)], crop_w=600, src_w=1920)
    assert ffmpeg_expr(expr, 0.0) == 0
    assert ffmpeg_expr(expr, 5.0) == 1320


def test_smooth_track():
    still = [(t * 0.5, 960.0 + (t % 2)) for t in range(40)]
    assert facecam.smooth_track(still, 20, 1920, 608) is None  # barely moves: static crop
    walking = [(t * 0.5, 600.0 + t * 25) for t in range(40)]  # walks right ~50 px/s
    keys = facecam.smooth_track(walking, 20, 1920, 608)
    assert keys and keys[0][0] == 0.0
    xs = [x for _, x in keys]
    assert xs[-1] > xs[0] + 500
    assert all(b - a <= 0.25 * 1920 + 1e-6 for a, b in zip(xs, xs[1:]))  # pan speed limit
    jumpy = [(t * 0.5, 300.0 if t < 20 else 1600.0) for t in range(40)]  # face detector jumps
    xs = [x for _, x in facecam.smooth_track(jumpy, 20, 1920, 608)]
    assert max(b - a for a, b in zip(xs, xs[1:])) <= 0.25 * 1920 + 1e-6


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_fullcam_render_with_track(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=duration=6", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(src)], check=True)
    scene = SceneLayout("fullcam", 1920, 1080, face=Box(900, 300, 250, 300),
                        track=[(0.0, 500.0), (2.0, 800.0), (4.0, 1400.0), (6.0, 1400.0)])
    out = tmp_path / "out.mp4"
    keep = [(0.0, 2.0), (3.0, 6.0)]
    assert render.render(src, out, scene, [], 5.0, "auto", False, keep) == "fullcam"
    assert media.video_size(out) == (1080, 1920)
    assert 4.8 < media.duration(out) < 5.3


# ---------------------------------------------------------------- stats
def _db(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    now = time.time()
    for i, (rating, views, status) in enumerate([(9, 120_000, "approved"), (8, 60_000, "approved"),
                                                 (6, 9_000, "new"), (4, 3_000, "discarded"), (3, 800, "new")]):
        db.execute("INSERT INTO clips (guild_id, title, rating, actual_views, status, file_path, channel_id, "
                   "message_id, created_at) VALUES (1,?,?,?,?,'f',10,?,?)",
                   (f"clip {i}", rating, views, status, 100 + i, now - 3600))
    db.execute("INSERT INTO clip_posts (clip_id, platform, url, views, created_at) VALUES (1,'tiktok','https://t/1',120000,?)",
               (now,))
    db.execute("INSERT INTO streamers (guild_id, platform, channel, permission, created_at) VALUES (1,'kick','x','unknown',0)")
    return db


def test_digest(tmp_path):
    d = stats.digest(_db(tmp_path), 1)
    assert (d.clips_made, d.approved, d.discarded, d.awaiting_review) == (5, 2, 1, 2)
    assert d.best_clip["rating"] == 9 and d.avg_rating == pytest.approx(6.0)
    assert d.views_tracked == 120_000 and d.unknown_streamers == ["x"]
    assert stats.message_link(1, d.best_clip) == "https://discord.com/channels/1/10/100"
    assert stats.digest(Database(tmp_path / "empty.sqlite3"), 1).empty


def test_top_and_accuracy(tmp_path):
    rows, accuracy = stats.top_clips(_db(tmp_path), 1)
    assert [r["actual_views"] for r in rows] == [120_000, 60_000, 9_000, 3_000, 800]
    assert accuracy.startswith("good (rank correlation 1.00")
    assert stats.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1)
    assert stats.spearman([5, 5, 5], [1, 2, 3]) is None
    assert "not enough data" in stats.accuracy_label(None, 2)


# ---------------------------------------------------------------- Twitch viewer clips
def test_viewer_clips_signal():
    import numpy as np

    from clipper import highlights
    from shared import chat

    clips = [{"offset": 3000.0, "duration": 30.0, "views": 50_000, "title": "HE ACTUALLY DID IT"},
             {"offset": 3010.0, "duration": 20.0, "views": 200, "title": "lol"}]
    curve = chat.clips_curve(clips, 7200)
    assert curve[3015] > curve[3005] > 0 and curve[100] == 0
    audio = np.random.default_rng(0).uniform(0, 1, 7200).astype(np.float32)
    combined = highlights.combine_signals(audio, None, viewer_clips=curve)
    best = highlights.pick_windows(combined, 1, window=160)[0]
    assert best[0] <= 3000 and 3030 <= best[1]
    assert "HE ACTUALLY DID IT" in chat.clips_summary(clips, 2990, 3150)
    assert chat.clips_summary(clips, 0, 100) == ""


def test_twitch_vod_clips_filters_by_vod(monkeypatch):
    import asyncio

    from aiohttp import web

    from shared import chat

    async def gql(request):
        return web.json_response({"data": {"user": {"clips": {"pageInfo": {"hasNextPage": False}, "edges": [
            {"cursor": "a", "node": {"title": "mine", "viewCount": 10, "durationSeconds": 30,
                                     "videoOffsetSeconds": 120, "video": {"id": "555"}}},
            {"cursor": "b", "node": {"title": "other vod", "viewCount": 99, "durationSeconds": 30,
                                     "videoOffsetSeconds": 50, "video": {"id": "777"}}},
        ]}}}})

    async def run():
        app = web.Application()
        app.router.add_post("/gql", gql)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        monkeypatch.setattr(chat, "TWITCH_GQL", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/gql")
        try:
            return await chat.twitch_vod_clips("someone", "v555")
        finally:
            await runner.cleanup()

    assert asyncio.run(run()) == [{"offset": 120.0, "duration": 30.0, "views": 10, "title": "mine"}]
