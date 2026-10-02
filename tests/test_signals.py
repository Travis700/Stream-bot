import json
import shutil
import subprocess

import numpy as np
import pytest

from clipper import highlights, render, tighten
from clipper.facecam import SceneLayout
from shared import chat, media


# ---------------------------------------------------------------- chat
def test_chat_spike_shows_up_and_is_shifted_earlier():
    rng = np.random.default_rng(1)
    messages = [(float(t), "hello") for t in rng.uniform(0, 3600, 3600)]  # ~1 msg/s background
    messages += [(1508.0 + i * 0.1, "KEKW") for i in range(200)]  # chat explodes at ~25:08
    exc = chat.chat_excitement(chat.chat_activity(messages, 3600, delay=8))
    peak = int(np.argmax(exc))
    assert 1495 <= peak <= 1525  # moved ~8s earlier than chat's reaction
    assert exc[600] < exc[peak] / 5


def test_message_weights():
    assert chat.message_weight("CLIP IT") == 3.0
    assert chat.message_weight("KEKW") > chat.message_weight("what game is this")


def test_summarize():
    msgs = [(108.0, "KEKW")] * 10 + [(110.0, "no way bro")] * 4 + [(500.0, "late")]
    text = chat.summarize(msgs, 100, 160)
    assert text.startswith("14 messages")
    assert "KEKW ×10" in text


def test_parse_youtube_chat(tmp_path):
    line = {"replayChatItemAction": {"videoOffsetTimeMsec": "61500", "actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"message": {"runs": [{"text": "W "}, {"emoji": {"shortcuts": [":fire:"]}}]}}
    }}}]}}
    path = tmp_path / "x.live_chat.json"
    path.write_text(json.dumps(line) + "\nnot json\n", encoding="utf-8")
    assert chat.parse_youtube_chat(path) == [(61.5, "W :fire:")]


def test_combine_signals_chat_can_win():
    audio = np.zeros(1000)
    audio[100:110] = 5  # loud moment
    chat_curve = np.zeros(1000)
    chat_curve[700:740] = 3  # big chat moment, streamer stayed calm
    combined = highlights.combine_signals(audio, chat_curve)
    wins = highlights.pick_windows(combined, 2, window=100, skip_start=0, skip_end=0)
    assert any(s <= 700 and 740 <= e for s, e, _ in wins)
    assert any(s <= 100 and 110 <= e for s, e, _ in wins)


# ---------------------------------------------------------------- dead-air cuts
def _words(times):
    return [{"w": f"w{i}", "s": s, "e": e} for i, (s, e) in enumerate(times)]


def test_cuts_only_quiet_pauses():
    words = _words([(0, 1), (1.1, 2), (6, 7), (7.1, 8), (14, 15), (15.1, 70)])
    loud = np.full(71, -25.0)
    loud[9:13] = -10.0  # the 8–14s pause has loud game audio: keep it
    keep = tighten.plan_keep_segments(words, 70, loud, min_total=30)
    assert keep == [(0.0, 2.25), (5.75, 70.0)]
    assert tighten.kept_duration(keep) == pytest.approx(66.5)
    remapped = tighten.remap_words(words, keep)
    assert remapped[2]["s"] == pytest.approx(2.5)  # word at 6s now 0.25s after the cut
    assert remapped[-1]["e"] == pytest.approx(66.5)


def test_cuts_never_go_below_minimum():
    words = _words([(0, 1), (5, 6), (10, 11), (15, 61)])
    loud = np.full(62, -30.0)
    keep = tighten.plan_keep_segments(words, 61, loud, min_total=55)
    assert tighten.kept_duration(keep) >= 55
    assert len(keep) == 2  # only one ~3.5s cut fits


def test_no_loudness_means_no_cuts():
    assert tighten.plan_keep_segments(_words([(0, 1), (9, 10)]), 10, None, 5) == [(0.0, 10)]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_render_with_cuts(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
                    "-f", "lavfi", "-i", "sine=duration=8", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(src)], check=True)
    keep = [(0.0, 2.0), (5.0, 8.0)]
    words = tighten.remap_words(_words([(0.5, 1.0), (5.5, 6.0)]), keep)
    out = tmp_path / "out.mp4"
    render.render(src, out, SceneLayout("nocam", 1280, 720), words, tighten.kept_duration(keep), "fit", True, keep)
    assert 4.8 < media.duration(out) < 5.3
    assert media.video_size(out) == (1080, 1920)
