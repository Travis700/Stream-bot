"""Turn the rater's library of viral reference clips into prompt text."""
from __future__ import annotations

import json

from .db import Database


def reference_cards(db: Database, limit: int = 20, streamer: str | None = None) -> list[dict]:
    rows = db.all("SELECT * FROM reference_clips WHERE status='ready' ORDER BY views DESC LIMIT 200")
    if streamer:
        key = streamer.lower().lstrip("@")
        same = [r for r in rows if key in (r.get("streamer") or "").lower() or key in (r.get("caption") or "").lower()]
        rows = same[: limit // 2] + [r for r in rows if r not in same]
    cards = []
    for row in rows[:limit]:
        try:
            analysis = json.loads(row["analysis"] or "{}")
        except json.JSONDecodeError:
            analysis = {}
        cards.append({**row, "analysis": analysis})
    return cards


def format_card(card: dict) -> str:
    a = card["analysis"]
    views = f"{card['views']:,}" if card.get("views") else "?"
    return (f"- [{card.get('platform')}, {views} views, {int(card.get('duration') or 0)}s, "
            f"streamer: {card.get('streamer') or 'unknown'}] hook: {a.get('hook', '')} | "
            f"why it worked: {a.get('why_it_worked', '')} | format: {a.get('format_notes', '')}")


def learned_patterns_text(db: Database, limit: int = 12, streamer: str | None = None) -> str:
    return "\n".join(format_card(c) for c in reference_cards(db, limit, streamer))
