import shutil
import subprocess

import numpy as np
import pytest

from clipper import facecam, highlights, render, subtitles
from clipper.facecam import Box, SceneLayout
from shared import media, platforms
from shared.db import Database
from shared.permissions import classify_text


# ---------------------------------------------------------------- permissions
@pytest.mark.parametrize("text,expected", [
    ("Welcome! NO CLIPPING or reuploading of my streams.", "denied"),
    ("Clips will be reported for copyright.", "denied"),
    ("Don't re-upload my content without permission", "denied"),
    ("Clipping is allowed! Tag me on TikTok", "allowed"),
    ("Feel free to clip my streams and post them", "allowed"),
    ("Join my clipping program on Vyro to earn money", "allowed"),
    ("Clippers welcome. No editing out the sponsor though.", "allowed"),
    ("I play valorant every day at 5pm", "unknown"),
    ("Clipping is allowed but only my official clippers may monetise", "denied"),
])
def test_classify_text(text, expected):
    assert classify_text(text)[0] == expected


def test_parse_channel():
    assert platforms.parse_channel("twitch", "https://www.twitch.tv/xQc") == "xqc"
    assert platforms.parse_channel("kick", "kick.com/adinross") == "adinross"
    assert platforms.parse_channel("youtube", "https://www.youtube.com/@MrBeast/streams") == "@MrBeast"
    assert platforms.parse_channel("youtube", "somehandle") == "@somehandle"
    assert platforms.parse_channel("twitch", "Shroud") == "shroud"
    assert platforms.detect_platform("https://www.tiktok.com/@x/video/1") == "tiktok"
    assert platforms.detect_platform("https://kick.com/x/videos/abc") == "kick"


def test_timestamp_url():
    assert platforms.timestamp_url("twitch", "https://www.twitch.tv/videos/1", 3725) == \
        "https://www.twitch.tv/videos/1?t=1h2m5s"


# ---------------------------------------------------------------- highlights
def test_pick_windows_finds_spikes():
    rng = np.random.default_rng(0)
    loud = -30 + rng.normal(0, 1.5, 4 * 3600).astype(np.float32)
    for spike in (1000, 5000, 9000):
        loud[spike:spike + 40] += 15
    exc = highlights.excitement_curve(loud)
    wins = highlights.pick_windows(exc, 3, window=190)
    assert len(wins) == 3
    for spike in (1000, 5000, 9000):
        assert any(s <= spike and spike + 40 <= e for s, e, _ in wins), (spike, wins)
    # non-overlapping
    for (s1, e1, _), (s2, _, _) in zip(wins, wins[1:]):
        assert e1 <= s2


def test_pick_windows_short_vod():
    wins = highlights.pick_windows(np.zeros(100), 3, window=190)
    assert wins == [(0.0, 100.0, 0.0)]


WORDS = [
    {"w": "so", "s": 0.0, "e": 0.2}, {"w": "anyway.", "s": 0.3, "e": 0.8},
    {"w": "Then", "s": 10.0, "e": 10.3}, {"w": "he", "s": 10.35, "e": 10.5},
    {"w": "said", "s": 10.55, "e": 10.9}, {"w": "what?!", "s": 11.0, "e": 11.6},
    {"w": "No", "s": 13.0, "e": 13.3}, {"w": "way", "s": 13.35, "e": 13.7},
]


def test_snap_to_sentences():
    assert highlights.snap_start(WORDS, 9.0) == pytest.approx(9.85)
    assert highlights.snap_end(WORDS, 12.0) == pytest.approx(11.95)


def test_heuristic_plans_respect_length():
    words = [{"w": f"w{i}", "s": i * 0.5, "e": i * 0.5 + 0.3} for i in range(380)]
    cands = [highlights.Candidate(1, 1000, 1190, 5.0, words), highlights.Candidate(2, 3000, 3190, 2.0, [])]
    plans = highlights.heuristic_plans(cands, 2, 60, 120)
    assert len(plans) == 2
    for p in plans:
        assert 60 <= p.end - p.start <= 120
        assert all(w["s"] >= 0 for w in p.words)


# ---------------------------------------------------------------- subtitles
def test_chunk_and_ass():
    chunks = subtitles.chunk_words(WORDS)
    assert [len(c) for c in chunks] == [2, 4, 2] or all(len(c) <= 3 for c in chunks)
    ass = subtitles.build_ass(WORDS, 700, "Montserrat ExtraBold", 15)
    assert "PlayResY: 1920" in ass
    assert ass.count("Dialogue:") == len(WORDS)
    assert "WHAT?!" in ass
    assert subtitles.ass_time(3725.5) == "1:02:05.50"


# ---------------------------------------------------------------- layout
def test_split_layout_avoids_facecam():
    cam = Box(1400, 650, 480, 400)  # bottom-right webcam
    scene = SceneLayout("gaming", 1920, 1080, face=Box(1580, 720, 110, 120), cam=cam)
    plan = render.plan_layout(scene)
    assert plan.layout == "split"
    assert "vstack" in plan.filter_graph
    assert 560 <= plan.caption_y <= 800


def test_game_crop_moves_off_cam():
    cam = Box(800, 0, 400, 300)  # centred webcam
    def overlap(x):
        return max(0, min(x + 900, cam.x + cam.w) - max(x, cam.x))

    x = render.game_crop_x(1920, 900, cam)
    assert x % 2 == 0
    assert 0 <= x <= 1920 - 900
    assert overlap(x) < overlap((1920 - 900) // 2)
    # A corner webcam that barely touches the crop leaves it centred.
    assert render.game_crop_x(1920, 900, Box(0, 0, 400, 300)) == 510


def test_layout_fallbacks():
    assert render.plan_layout(SceneLayout("nocam", 1920, 1080)).layout == "fit"
    assert render.plan_layout(SceneLayout("nocam", 1920, 1080), "split").layout == "fit"
    full = SceneLayout("fullcam", 1920, 1080, face=Box(900, 300, 300, 350))
    assert render.plan_layout(full).layout == "fullcam"


# ---------------------------------------------------------------- db
def test_job_queue(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    a = db.enqueue_job(1, "vod", {"url": "a"})
    db.enqueue_job(1, "vod", {"url": "b"})
    job = db.next_job()
    assert job["id"] == a and job["payload"]["url"] == "a"
    db.requeue_interrupted_jobs()
    assert db.next_job()["id"] == a


# ---------------------------------------------------------------- ffmpeg end-to-end
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture
def sample_video(tmp_path):
    path = tmp_path / "src.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)], check=True)
    return path


@needs_ffmpeg
@pytest.mark.parametrize("layout", ["split", "fit"])
def test_render_vertical(tmp_path, sample_video, layout):
    if layout == "split":
        scene = SceneLayout("gaming", 1920, 1080, face=Box(80, 760, 100, 110), cam=Box(0, 700, 420, 380))
    else:
        scene = facecam.analyse(sample_video)
    out = tmp_path / "out.mp4"
    words = [{"w": "hello", "s": 0.5, "e": 1.0}, {"w": "chat!", "s": 1.1, "e": 1.6}]
    used = render.render(sample_video, out, scene, words, 5.0, layout)
    assert used == layout
    assert media.video_size(out) == (1080, 1920)
    assert 4.5 < media.duration(out) < 5.6
    preview = render.make_preview(out, tmp_path / "p.mp4", 5.0, 2_000_000)
    assert preview and preview.stat().st_size <= 2_000_000
    assert len(media.loudness_per_second(out)) in (5, 6)


def test_llm_plans_clamps_answer(monkeypatch):
    import asyncio

    async def fake_ask_json(system, content, schema, **kw):
        assert "Candidate 1" in content[0]["text"]
        return {"clips": [
            {"candidate_id": 1, "start_offset": 10, "end_offset": 400, "title": " Big moment ", "reason": "r",
             "virality": 8},
            {"candidate_id": 1, "start_offset": 12, "end_offset": 140, "title": "dup", "reason": "r", "virality": 7},
            {"candidate_id": 99, "start_offset": 0, "end_offset": 130, "title": "bad id", "reason": "r",
             "virality": 9},
        ]}

    monkeypatch.setattr(highlights.llm, "ask_json", fake_ask_json)
    words = [{"w": f"w{i}", "s": i * 0.5, "e": i * 0.5 + 0.3} for i in range(380)]
    cands = [highlights.Candidate(1, 1000, 1190, 5.0, words)]
    plans = asyncio.run(highlights.llm_plans(cands, 3, 120, 150, "streamer", "title"))
    assert len(plans) == 1
    assert plans[0].title == "Big moment"
    assert plans[0].start == 1010 and plans[0].end == 1160
