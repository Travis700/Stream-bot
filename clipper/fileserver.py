"""Tiny HTTP server so full-quality clips (too big for Discord uploads) can be downloaded."""
from __future__ import annotations

import logging
import re

from aiohttp import web

from shared.config import settings

log = logging.getLogger(__name__)
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


async def _serve_clip(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    if not _TOKEN.match(token):
        raise web.HTTPNotFound()
    path = settings.clips_dir / f"{token}.mp4"
    if not path.is_file():
        raise web.HTTPNotFound(text="Clip expired or not found")
    return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="clip_{token[:6]}.mp4"'})


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/c/{token}.mp4", _serve_clip)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.file_server_port).start()
    log.info("File server listening on :%s (public URL: %s)", settings.file_server_port,
             settings.public_base_url or "not set")
    return runner
