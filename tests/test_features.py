import asyncio
import dataclasses
import shutil
import subprocess
import threading
import time

import numpy as np
import pytest

from clipper import bot as clipper_bot
from clipper import censor, pipeline, posting, render
from clipper.facecam import SceneLayout
from clipper.subtitles import build_ass
from shared import health, locks, media
from shared.config import settings
from shared.db import Database

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


# ---------------------------------------------------------------- slur filter
def test_censor_extra_words_and_masking():
    matcher = censor.build_matcher("banana, heck")
    words = [{"w": "the", "s": 0.0, "e": 0.2}, {"w": "Banana!", "s": 0.3, "e": 0.7},
             {"w": "spice", "s": 0.8, "e": 1.0}, {"w": "raccoon", "s": 1.1, "e": 1.4}]
    assert censor.find_bleeps(words, matcher) == [(pytest.approx(0.25), pytest.approx(0.75))]
    masked = censor.mask_words(words, matcher)
    assert masked[1]["w"] == "B*****" and masked[2]["w"] == "spice"


def test_default_patterns_avoid_common_false_positives():
    matcher = censor.build_matcher("")
    for ok in ("spice", "raccoon", "cocoon", "figure", "chunky", "kick", "good", "snigger"):
        assert not matcher.match(censor._normalise(ok)), ok
    assert censor._normalise("N!gga,") == "n!gga" and censor._normalise("'banana!'") == "banana"


def test_audio_filter_modes():
    assert censor.audio_filter([], "beep") == ("", "")
    mute, beep = censor.audio_filter([(1.0, 1.5)], "beep")
    assert mute == "between(t,1.00,1.50)" and beep == mute
    assert censor.audio_filter([(1.0, 1.5)], "mute")[1] == ""


# ---------------------------------------------------------------- hook text
def test_hook_text_in_ass():
    ass = build_ass([], 700, "Font", 30, hook_text="he did NOT see that coming 💀", hook_y=900, hook_seconds=4)
    hook_lines = [line for line in ass.splitlines() if line.startswith("Dialogue: 1")]
    assert len(hook_lines) == 1
    assert "0:00:04.00" in hook_lines[0] and "HE DID NOT SEE THAT COMING" in hook_lines[0]
    assert "💀" not in ass


@needs_ffmpeg
def test_render_with_hook_and_mute(tmp_path, monkeypatch):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    monkeypatch.setattr(render, "settings", dataclasses.replace(settings, bleep_mode="mute"))
    out = tmp_path / "out.mp4"
    words = [{"w": "B*****", "s": 1.0, "e": 2.0}]
    render.render(src, out, SceneLayout("nocam", 1280, 720), words, 4.0, "fit", True, None,
                  hook_text="watch this", bleeps=[(0.8, 2.2)])
    loud = media.loudness_per_second(out)
    assert loud[1] < loud[3] - 20  # the bleeped second is (near) silent

    monkeypatch.setattr(render, "settings", dataclasses.replace(settings, bleep_mode="beep"))
    render.render(src, out, SceneLayout("nocam", 1280, 720), words, 4.0, "fit", True, None,
                  hook_text="watch this", bleeps=[(1.0, 2.0)])
    assert 3.8 < media.duration(out) < 4.3


# ---------------------------------------------------------------- dedupe / re-edits
def test_vod_key_matches_url_variants():
    assert pipeline.vod_key("https://www.twitch.tv/videos/123456?t=1h2m") == \
        pipeline.vod_key("https://twitch.tv/videos/123456") == "twitch:123456"
    assert pipeline.vod_key("https://www.youtube.com/watch?v=abc123&t=5") == "youtube:abc123"


def test_resolve_layout_uses_streamer_default():
    assert pipeline.resolve_layout("auto", {"layout": "fit"}) == "fit"
    assert pipeline.resolve_layout("split", {"layout": "fit"}) == "split"
    assert pipeline.resolve_layout(None, {}) == "auto"


def test_shorter_window():
    words = [{"w": f"w{i}", "s": i * 0.5, "e": i * 0.5 + 0.3} for i in range(200)]
    loud = np.full(101, -30.0)
    loud[60:80] = -10  # the lively bit
    start, end = pipeline.shorter_window(words, loud, (0.0, 100.0), 100.0, min_len=60)
    assert 60 <= end - start <= 80
    assert start <= 60 and end >= 80
    with pytest.raises(pipeline.JobError):
        pipeline.shorter_window(words, loud, (0.0, 62.0), 62.0, min_len=60)


@needs_ffmpeg
def test_rerender_end_to_end(tmp_path, monkeypatch):
    local = dataclasses.replace(settings, data_dir=tmp_path, clip_min_seconds=4)
    monkeypatch.setattr(pipeline, "settings", local)
    local.ensure_dirs()
    src = local.clips_dir / "tok_src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12",
                    "-f", "lavfi", "-i", "sine=duration=12", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(src)], check=True)
    db = Database(tmp_path / "db.sqlite3")
    words = [{"w": f"word{i}", "s": i * 0.5, "e": i * 0.5 + 0.4} for i in range(24)]
    sid = db.execute("INSERT INTO streamers (guild_id, platform, channel, permission, created_at) "
                     "VALUES (1,'twitch','x','allowed',0)")
    clip_id = db.execute(
        "INSERT INTO clips (guild_id, streamer_id, vod_url, start_s, end_s, duration, title, layout, file_path, token, "
        "source_path, source_words, options, created_at) VALUES (1,?,?,1000,1012,12,'t','split','x','tok',?,?,?,0)",
        (sid, "https://www.twitch.tv/videos/1", str(src), pipeline.dumps(words),
         pipeline.dumps({"subtitles": True, "tighten": False})))
    job = {"id": 1, "guild_id": 1, "payload": {"clip_id": clip_id, "layout": "fit", "shorter": True}}

    async def progress(_text):
        return None

    [new_id] = asyncio.run(pipeline.rerender_clip(db, job, progress))
    new = db.one("SELECT * FROM clips WHERE id=?", (new_id,))
    assert new["parent_clip_id"] == clip_id and new["layout"] == "fit"
    assert 1000 <= new["start_s"] and new["end_s"] <= 1012
    assert new["duration"] < 12
    assert media.video_size(pipeline.Path(new["file_path"])) == (1080, 1920)
    # A second re-edit of the re-edit still maps back to the same VOD times.
    job2 = {"id": 2, "guild_id": 1, "payload": {"clip_id": new_id, "layout": "fit", "shorter": False}}
    [third] = asyncio.run(pipeline.rerender_clip(db, job2, progress))
    third_row = db.one("SELECT * FROM clips WHERE id=?", (third,))
    assert third_row["start_s"] == pytest.approx(new["start_s"])
    # Discarding the first clip keeps the shared source for the others.
    pipeline.delete_clip_files(db, db.one("SELECT * FROM clips WHERE id=?", (clip_id,)))
    assert src.exists()


# ---------------------------------------------------------------- buttons
def test_clip_button_custom_ids_round_trip():
    button = clipper_bot.ClipButton("post_tiktok", 42)
    match = clipper_bot.ClipButton.__discord_ui_compiled_template__.fullmatch(button.item.custom_id)
    assert match and match["action"] == "post_tiktok" and match["id"] == "42"


# ---------------------------------------------------------------- posting helpers
def test_tiktok_chunks():
    mb = posting.MB
    assert posting.tiktok_chunks(3 * mb) == [(0, 3 * mb - 1)]
    chunks = posting.tiktok_chunks(25 * mb)
    assert len(chunks) == 2 and chunks[0] == (0, 10 * mb - 1) and chunks[-1] == (10 * mb, 25 * mb - 1)
    assert sum(e - s + 1 for s, e in chunks) == 25 * mb


def test_build_caption_prefers_rating():
    clip = {"title": "Big moment", "rating_json": '{"suggested_caption": "he really did that", "hashtags": ["fyp", "#gaming"]}'}
    assert posting.build_caption(clip) == "he really did that\n\n#fyp #gaming"
    assert posting.build_caption({"title": "Big moment"}).startswith("Big moment\n\n#clips")


# ---------------------------------------------------------------- maintenance
def test_heavy_lock_serialises_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "settings", dataclasses.replace(settings, data_dir=tmp_path))
    active, overlap = [0], [False]

    def work():
        with locks.heavy_sync("test"):
            active[0] += 1
            overlap[0] |= active[0] > 1
            time.sleep(0.05)
            active[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlap[0]
    assert not locks.is_busy()


def test_version_compare():
    assert health._version_tuple("2026.10.01") > health._version_tuple("2026.8.19")
    assert health._version_tuple("2026.08.19") == health._version_tuple("2026.8.19")


def test_file_server_hides_sources(tmp_path, monkeypatch):
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import web
    from clipper import fileserver

    local = dataclasses.replace(settings, data_dir=tmp_path)
    local.ensure_dirs()
    monkeypatch.setattr(fileserver, "settings", local)
    (local.clips_dir / "abcdefghij.mp4").write_bytes(b"clip")
    (local.clips_dir / "abcdefghij_src.mp4").write_bytes(b"source")

    async def fetch(token):
        req = make_mocked_request("GET", f"/c/{token}.mp4", match_info={"token": token})
        return await fileserver._serve_clip(req)

    assert isinstance(asyncio.run(fetch("abcdefghij")), web.FileResponse)
    for bad in ("abcdefghij_src", "abcdefghij_preview", "../etc"):
        with pytest.raises(web.HTTPNotFound):
            asyncio.run(fetch(bad))
