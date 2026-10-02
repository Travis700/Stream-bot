import asyncio
import dataclasses
import shutil
import time

import pytest

from clipper import pipeline, selftest
from shared.config import settings
from shared.db import Database


def test_retry_waits_until_not_before(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    job_id = db.enqueue_job(1, "vod", {"url": "u"})
    db.update_job(job_id, status="queued", attempts=1, not_before=time.time() + 600)
    assert db.next_job() is None  # still waiting
    db.update_job(job_id, not_before=time.time() - 1)
    assert db.next_job()["id"] == job_id


def test_backup_is_a_working_copy(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.enqueue_job(1, "vod", {"url": "u"})
    dest = tmp_path / "backups" / "copy.sqlite3"
    db.backup(dest)
    assert Database(dest).one("SELECT COUNT(*) AS n FROM jobs")["n"] == 1


@pytest.mark.parametrize("message,transient,friendly", [
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot", False, "cookies"),
    ("ERROR: [twitch:vod] 1: This video is only available to subscribers", False, "subscriber-only"),
    ("HTTP Error 503: Service Unavailable", True, None),
    ("Read timed out. (read timeout=20)", True, None),
    ("[Errno 28] No space left on device", False, "disk is full"),
    ("KeyError: 'width'", False, None),
])
def test_error_classification(message, transient, friendly):
    exc = RuntimeError(message)
    assert pipeline.is_transient(exc) is transient
    if friendly:
        assert friendly in pipeline.explain_error(exc)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_selftest_reports_every_step(tmp_path, monkeypatch):
    local = dataclasses.replace(settings, data_dir=tmp_path, llm_provider="none")
    local.ensure_dirs()
    for module in (selftest,):
        monkeypatch.setattr(module, "settings", local)
    monkeypatch.setattr(selftest.llm, "settings", local)
    # Speech-to-text needs a model download; stub it so the test runs offline.
    monkeypatch.setattr(selftest, "transcribe", lambda path: [{"w": "okay", "s": 1.0, "e": 1.3},
                                                              {"w": "chat", "s": 1.4, "e": 1.8}])
    result = asyncio.run(selftest.run(9_000_000))
    names = [s.name for s in result.steps]
    assert names == ["ffmpeg + test video", "Speech-to-text", "Face detector", "AI",
                     "Render (captions + hook + cuts)", "Discord preview"]
    assert result.ok, [(s.name, s.detail) for s in result.steps if not s.ok]
    assert result.video and result.video.exists()
