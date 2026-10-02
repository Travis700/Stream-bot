"""One heavy CPU task at a time across BOTH bots (separate containers sharing /data).

Transcription, video rendering and local-AI calls each use every core; running two at
once just makes both slower. A file lock (flock) on the shared volume serialises them.
Only leaf functions take the lock, so it is never held twice by the same caller.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

from .config import settings

log = logging.getLogger(__name__)


def _lock_path():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / "heavy.lock"


@contextmanager
def heavy_sync(label: str = "") -> Iterator[None]:
    """Blocking version, for code already running in a worker thread."""
    with open(_lock_path(), "a+") as fh:
        started = time.monotonic()
        fcntl.flock(fh, fcntl.LOCK_EX)
        waited = time.monotonic() - started
        if waited > 5:
            log.info("Waited %.0fs for the CPU (%s)", waited, label)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@asynccontextmanager
async def heavy(label: str = "") -> AsyncIterator[None]:
    """Async version: waits in a thread so the Discord connection stays alive."""
    fh = open(_lock_path(), "a+")
    try:
        await asyncio.to_thread(fcntl.flock, fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def is_busy() -> bool:
    """True if some heavy task currently holds the CPU lock."""
    with open(_lock_path(), "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
