"""SQLite storage shared by both bots (they mount the same data volume)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    clips_channel_id INTEGER,
    clipper_feed_channel_id INTEGER,
    log_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS streamers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    platform TEXT NOT NULL,              -- twitch | kick | youtube
    channel TEXT NOT NULL,               -- slug / handle
    display_name TEXT,
    permission TEXT NOT NULL DEFAULT 'unknown',   -- allowed | denied | unknown
    permission_source TEXT NOT NULL DEFAULT 'auto', -- auto | manual
    permission_evidence TEXT,
    permission_checked_at REAL,
    auto_clip INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    UNIQUE (guild_id, platform, channel)
);

CREATE TABLE IF NOT EXISTS vods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    streamer_id INTEGER NOT NULL,
    vod_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'seen',  -- seen | queued | done | failed
    created_at REAL NOT NULL,
    UNIQUE (streamer_id, vod_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', -- queued | running | done | failed
    progress TEXT,
    error TEXT,
    requested_by INTEGER,
    status_channel_id INTEGER,
    status_message_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    guild_id INTEGER,
    streamer_id INTEGER,
    vod_url TEXT,
    start_s REAL,
    end_s REAL,
    duration REAL,            -- final length after dead-air cuts
    title TEXT,
    reason TEXT,
    layout TEXT,
    transcript TEXT,          -- JSON list of {w, s, e} relative to clip start
    file_path TEXT,
    preview_path TEXT,
    token TEXT UNIQUE,
    channel_id INTEGER,
    message_id INTEGER,
    rating INTEGER,
    rating_json TEXT,
    actual_views INTEGER,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS clipper_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    streamer_id INTEGER,
    url TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (guild_id, url)
);

CREATE TABLE IF NOT EXISTS clipper_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    post_id TEXT NOT NULL,
    url TEXT,
    title TEXT,
    views INTEGER,
    created_at REAL NOT NULL,
    UNIQUE (account_id, post_id)
);

CREATE TABLE IF NOT EXISTS reference_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    min_views INTEGER NOT NULL,
    last_checked REAL,
    created_at REAL NOT NULL,
    UNIQUE (guild_id, url)
);

CREATE TABLE IF NOT EXISTS clip_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL,
    platform TEXT,
    url TEXT,
    external_id TEXT,
    views INTEGER,
    likes INTEGER,
    last_checked REAL,
    created_at REAL NOT NULL,
    UNIQUE (clip_id, url)
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    service TEXT PRIMARY KEY,
    access_token TEXT,
    refresh_token TEXT,
    expires_at REAL,
    extra TEXT
);

CREATE TABLE IF NOT EXISTS reference_clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    platform TEXT,
    uploader TEXT,
    streamer TEXT,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    duration REAL,
    caption TEXT,
    analysis TEXT,           -- JSON pattern card produced by Claude
    status TEXT NOT NULL DEFAULT 'pending', -- pending | ready | failed
    error TEXT,
    created_at REAL NOT NULL
);
"""


# Columns added after the first release: (table, column, type)
MIGRATIONS = [
    ("clips", "duration", "REAL"),
    ("clips", "status", "TEXT NOT NULL DEFAULT 'new'"),   # new | approved | discarded
    ("clips", "hook_text", "TEXT"),
    ("clips", "source_path", "TEXT"),      # un-edited source video, kept for re-edits
    ("clips", "source_words", "TEXT"),     # transcript of the source (relative to its start)
    ("clips", "options", "TEXT"),          # JSON render options (subtitles, tighten)
    ("clips", "parent_clip_id", "INTEGER"),
    ("streamers", "layout", "TEXT"),       # preferred layout for this streamer
]


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for table, column, kind in MIGRATIONS:
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        """Run a write statement; returns lastrowid."""
        with self.connect() as conn:
            return conn.execute(sql, params).lastrowid

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # --- guild config ---------------------------------------------------
    def guild_config(self, guild_id: int) -> dict[str, Any]:
        return self.one("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)) or {"guild_id": guild_id}

    def set_guild_config(self, guild_id: int, **values: int | None) -> None:
        self.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        for key, value in values.items():
            if key not in {"clips_channel_id", "clipper_feed_channel_id", "log_channel_id"}:
                raise ValueError(key)
            if value is not None:
                self.execute(f"UPDATE guild_config SET {key}=? WHERE guild_id=?", (value, guild_id))

    def all_guild_configs(self) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM guild_config")

    # --- streamers ------------------------------------------------------
    def find_streamer(self, guild_id: int, name: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT * FROM streamers WHERE guild_id=? AND (lower(channel)=lower(?) OR lower(display_name)=lower(?)) "
            "ORDER BY id LIMIT 1",
            (guild_id, name, name),
        )

    def find_streamer_by_channel(self, guild_id: int, platform: str, channel: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT * FROM streamers WHERE guild_id=? AND platform=? AND lower(channel)=lower(?)",
            (guild_id, platform, channel),
        )

    def set_permission(self, streamer_id: int, permission: str, source: str, evidence: str | None) -> None:
        self.execute(
            "UPDATE streamers SET permission=?, permission_source=?, permission_evidence=?, permission_checked_at=? "
            "WHERE id=?",
            (permission, source, evidence, time.time(), streamer_id),
        )

    # --- jobs -----------------------------------------------------------
    def enqueue_job(self, guild_id: int, kind: str, payload: dict, requested_by: int | None = None,
                    status_channel_id: int | None = None) -> int:
        now = time.time()
        return self.execute(
            "INSERT INTO jobs (guild_id, kind, payload, requested_by, status_channel_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (guild_id, kind, json.dumps(payload), requested_by, status_channel_id, now, now),
        )

    def next_job(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (time.time(), row["id"]))
            job = dict(row)
            job["payload"] = json.loads(job["payload"])
            return job

    def update_job(self, job_id: int, **values: Any) -> None:
        values["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in values)
        self.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*values.values(), job_id))

    def requeue_interrupted_jobs(self) -> None:
        self.execute("UPDATE jobs SET status='queued', progress='restarted' WHERE status='running'")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
