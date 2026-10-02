"""Numbers for the daily summary and /top."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from shared.db import Database


def message_link(guild_id: int, clip: dict) -> str | None:
    if clip.get("channel_id") and clip.get("message_id"):
        return f"https://discord.com/channels/{guild_id}/{clip['channel_id']}/{clip['message_id']}"
    return None


@dataclass
class Digest:
    clips_made: int = 0
    approved: int = 0
    discarded: int = 0
    awaiting_review: int = 0
    jobs_failed: int = 0
    avg_rating: float | None = None
    best_clip: dict | None = None
    views_tracked: int = 0
    top_post: dict | None = None
    unknown_streamers: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.clips_made or self.jobs_failed or self.views_tracked or self.awaiting_review)


def digest(db: Database, guild_id: int, hours: float = 24) -> Digest:
    since = time.time() - hours * 3600
    d = Digest()
    clips = db.all("SELECT * FROM clips WHERE guild_id=? AND created_at>?", (guild_id, since))
    d.clips_made = len(clips)
    d.approved = sum(1 for c in clips if c.get("status") == "approved")
    d.discarded = sum(1 for c in clips if c.get("status") == "discarded")
    rated = [c for c in clips if c.get("rating") is not None]
    if rated:
        d.avg_rating = sum(c["rating"] for c in rated) / len(rated)
        d.best_clip = max(rated, key=lambda c: c["rating"])
    d.awaiting_review = db.one("SELECT COUNT(*) AS n FROM clips WHERE guild_id=? AND status='new' "
                               "AND file_path IS NOT NULL", (guild_id,))["n"]
    d.jobs_failed = db.one("SELECT COUNT(*) AS n FROM jobs WHERE guild_id=? AND status='failed' AND updated_at>? "
                           "AND COALESCE(error,'')<>'cancelled'", (guild_id, since))["n"]
    posts = db.all("SELECT p.*, c.title FROM clip_posts p JOIN clips c ON c.id=p.clip_id "
                   "WHERE c.guild_id=? AND p.views IS NOT NULL ORDER BY p.views DESC", (guild_id,))
    d.views_tracked = sum(p["views"] for p in posts)
    d.top_post = posts[0] if posts else None
    d.unknown_streamers = [r["channel"] for r in db.all(
        "SELECT channel FROM streamers WHERE guild_id=? AND permission='unknown'", (guild_id,))]
    return d


def spearman(a: list[float], b: list[float]) -> float | None:
    """Rank correlation: 1 = the rater orders clips exactly like real views, 0 = no relation."""
    if len(a) < 3 or len(set(a)) < 2 or len(set(b)) < 2:
        return None

    def ranks(values: list[float]) -> np.ndarray:
        arr = np.asarray(values, dtype=float)
        order = arr.argsort()
        r = np.empty(len(arr))
        r[order] = np.arange(len(arr))
        for v in np.unique(arr):  # average ranks for ties
            r[arr == v] = r[arr == v].mean()
        return r

    return float(np.corrcoef(ranks(a), ranks(b))[0, 1])


def accuracy_label(rho: float | None, n: int) -> str:
    if rho is None:
        return f"not enough data yet ({n} clip(s) with real views; need 5+ with different scores)"
    quality = "good" if rho >= 0.5 else "okay" if rho >= 0.25 else "poor"
    return f"{quality} (rank correlation {rho:.2f} over {n} clips)"


def top_clips(db: Database, guild_id: int, days: int = 30, limit: int = 10) -> tuple[list[dict], str]:
    since = time.time() - days * 86400
    rows = db.all("SELECT * FROM clips WHERE guild_id=? AND actual_views IS NOT NULL AND created_at>? "
                  "ORDER BY actual_views DESC", (guild_id, since))
    pairs = [(r["rating"], r["actual_views"]) for r in rows if r.get("rating") is not None]
    rho = spearman([p[0] for p in pairs], [p[1] for p in pairs]) if len(pairs) >= 5 else None
    return rows[:limit], accuracy_label(rho, len(pairs))
