"""Tiny HTTP server so full-quality clips (too big for Discord uploads) can be downloaded."""
from __future__ import annotations

import logging
import re

from aiohttp import web

from shared.config import settings
from shared.db import Database

from . import posting

log = logging.getLogger(__name__)
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


async def _serve_clip(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    # Only finished clips are served, never the saved sources (<token>_src) or previews.
    if not _TOKEN.match(token) or token.endswith(("_src", "_preview")):
        raise web.HTTPNotFound()
    path = settings.clips_dir / f"{token}.mp4"
    if not path.is_file():
        raise web.HTTPNotFound(text="Clip expired or not found")
    return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="clip_{token[:6]}.mp4"'})


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _tiktok_callback(request: web.Request) -> web.Response:
    """TikTok redirects here after you approve the app in /connect tiktok."""
    if not posting.check_state(request.query.get("state", "")):
        return web.Response(status=400, text="This login link expired. Run /connect tiktok again.")
    if "code" not in request.query:
        return web.Response(status=400, text=f"TikTok login was cancelled: {request.query.get('error_description')}")
    try:
        await posting.tiktok_exchange_code(request.app["db"], request.query["code"])
    except posting.PostError as exc:
        return web.Response(status=400, text=str(exc))
    return web.Response(text="TikTok connected! You can close this tab and go back to Discord.")


async def start(db: Database) -> web.AppRunner:
    app = web.Application()
    app["db"] = db
    app.router.add_get("/oauth/tiktok", _tiktok_callback)
    app.router.add_get("/c/{token}.mp4", _serve_clip)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.file_server_port).start()
    log.info("File server listening on :%s (public URL: %s)", settings.file_server_port,
             settings.public_base_url or "not set")
    return runner
