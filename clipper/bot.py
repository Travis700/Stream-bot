"""Clipper bot: VODs -> vertical clips posted to Discord, plus a feed of the streamers' own clippers."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from shared import platforms
from shared.config import settings
from shared.db import Database
from shared.permissions import check_permission

from . import fileserver, pipeline
from .render import LAYOUTS

log = logging.getLogger("clipper")

PLATFORM_CHOICES = [app_commands.Choice(name=p.title(), value=p) for p in platforms.PLATFORMS]
LAYOUT_CHOICES = [app_commands.Choice(name=n, value=n) for n in LAYOUTS]
PERMISSION_COLORS = {"allowed": discord.Color.green(), "denied": discord.Color.red(),
                     "unknown": discord.Color.orange()}


def permission_embed(streamer: dict, sources: list[str] | None = None) -> discord.Embed:
    status = streamer["permission"]
    embed = discord.Embed(
        title=f"{streamer['display_name'] or streamer['channel']} ({streamer['platform']})",
        url=platforms.channel_url(streamer["platform"], streamer["channel"]),
        color=PERMISSION_COLORS.get(status, discord.Color.greyple()),
    )
    embed.add_field(name="Clipping", value=f"**{status.upper()}** ({streamer['permission_source']})")
    embed.add_field(name="Auto-clip new VODs", value="on" if streamer["auto_clip"] else "off")
    if sources:
        embed.add_field(name="Checked", value=", ".join(sources), inline=False)
    if streamer.get("permission_evidence"):
        embed.add_field(name="Evidence", value=streamer["permission_evidence"][:1000], inline=False)
    if status == "unknown":
        embed.set_footer(text="No clear rule found. Check their panels/socials yourself, then use "
                              "/streamer permission to allow or deny. Unknown streamers are never clipped.")
    elif status == "denied":
        embed.set_footer(text="This streamer does not allow clipping. The bot will not clip them.")
    return embed


class ClipperBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = Database(settings.db_path)
        self._last_edit: dict[int, float] = {}
        register_commands(self)

    async def setup_hook(self) -> None:
        self.db.requeue_interrupted_jobs()
        await fileserver.start()
        if settings.guild_id:
            guild = discord.Object(id=settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.job_worker.start()
        self.vod_watcher.start()
        self.clipper_watcher.start()
        self.cleanup.start()

    async def on_ready(self) -> None:
        log.info("Clipper bot ready as %s", self.user)

    # ------------------------------------------------------------------ helpers
    async def channel(self, channel_id: int | None) -> discord.abc.Messageable | None:
        if not channel_id:
            return None
        ch = self.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return ch  # type: ignore[return-value]

    async def log_to_guild(self, guild_id: int, text: str) -> None:
        cfg = self.db.guild_config(guild_id)
        ch = await self.channel(cfg.get("log_channel_id"))
        if ch:
            await ch.send(text[:2000])

    async def set_job_status(self, job: dict, text: str, force: bool = False) -> None:
        self.db.update_job(job["id"], progress=text)
        if not job.get("status_message_id"):
            return
        now = time.monotonic()
        if not force and now - self._last_edit.get(job["id"], 0) < 4:
            return
        self._last_edit[job["id"]] = now
        ch = await self.channel(job.get("status_channel_id"))
        if ch is None:
            return
        try:
            msg = ch.get_partial_message(job["status_message_id"])  # type: ignore[union-attr]
            await msg.edit(content=f"**Job #{job['id']}** — {text}")
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ jobs
    @tasks.loop(seconds=5)
    async def job_worker(self) -> None:
        job = self.db.next_job()
        if not job:
            return
        if job["status_channel_id"] and not job["status_message_id"]:
            ch = await self.channel(job["status_channel_id"])
            if ch:
                msg = await ch.send(f"**Job #{job['id']}** — starting…")
                job["status_message_id"] = msg.id
                self.db.update_job(job["id"], status_message_id=msg.id)
        try:
            started = time.monotonic()
            clip_ids = await pipeline.process_vod(self.db, job, lambda t: self.set_job_status(job, t))
            took = pipeline._fmt_len(time.monotonic() - started)
            for clip_id in clip_ids:
                await self.post_clip(clip_id)
            self.db.update_job(job["id"], status="done")
            if job["payload"].get("vod_row_id"):
                self.db.execute("UPDATE vods SET status='done' WHERE id=?", (job["payload"]["vod_row_id"],))
            await self.set_job_status(job, f"✅ done in {took} — {len(clip_ids)} clip(s) posted.", force=True)
        except Exception as exc:  # noqa: BLE001
            log.error("Job %s failed: %s", job["id"], traceback.format_exc())
            message = str(exc) if isinstance(exc, pipeline.JobError) else f"{type(exc).__name__}: {exc}"
            self.db.update_job(job["id"], status="failed", error=message[:2000])
            if job["payload"].get("vod_row_id"):
                self.db.execute("UPDATE vods SET status='failed' WHERE id=?", (job["payload"]["vod_row_id"],))
            await self.set_job_status(job, f"❌ failed: {message[:1500]}", force=True)
            if not job["status_message_id"]:
                await self.log_to_guild(job["guild_id"], f"Job #{job['id']} failed: {message[:1500]}")

    @job_worker.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()

    async def post_clip(self, clip_id: int) -> None:
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            return
        cfg = self.db.guild_config(clip["guild_id"])
        ch = await self.channel(cfg.get("clips_channel_id"))
        if ch is None:
            log.warning("No clips channel configured for guild %s", clip["guild_id"])
            return
        streamer = self.db.one("SELECT * FROM streamers WHERE id=?", (clip["streamer_id"],)) or {}
        guild = self.get_guild(clip["guild_id"])
        limit = guild.filesize_limit if guild else 10 * 1024 * 1024
        attachment_path = await asyncio.to_thread(pipeline.make_preview_for, self.db, clip, limit - 200_000)

        length = clip.get("duration") or (clip["end_s"] - clip["start_s"])
        platform = streamer.get("platform", "")
        embed = discord.Embed(title=clip["title"][:256], description=(clip["reason"] or "")[:1500],
                              color=discord.Color.purple())
        embed.add_field(name="Streamer", value=streamer.get("display_name") or streamer.get("channel", "?"))
        embed.add_field(name="Length", value=f"{int(length // 60)}:{int(length % 60):02d}")
        embed.add_field(name="Layout", value=clip["layout"])
        embed.add_field(name="Source", value=f"[VOD @ {_hms(clip['start_s'])}]"
                        f"({platforms.timestamp_url(platform, clip['vod_url'], clip['start_s'])})", inline=False)
        embed.set_footer(text=f"Clip #{clip['id']}")

        view = discord.ui.View()
        if settings.public_base_url:
            view.add_item(discord.ui.Button(label="Download full quality",
                                            url=f"{settings.public_base_url}/c/{clip['token']}.mp4"))
        files = []
        if attachment_path:
            files.append(discord.File(attachment_path, filename=f"clip_{clip['id']}.mp4"))
        elif not settings.public_base_url:
            embed.add_field(name="⚠️", value="Clip is too big to upload and PUBLIC_BASE_URL is not set, so "
                                            "there is no download link.", inline=False)
        msg = await ch.send(embed=embed, files=files, view=view if view.children else None)
        self.db.execute("UPDATE clips SET channel_id=?, message_id=? WHERE id=?", (ch.id, msg.id, clip_id))

    # ------------------------------------------------------------------ watchers
    @tasks.loop(minutes=settings.vod_poll_minutes)
    async def vod_watcher(self) -> None:
        streamers = self.db.all("SELECT * FROM streamers WHERE auto_clip=1 AND permission='allowed'")
        for s in streamers:
            try:
                await self.check_new_vods(s)
            except Exception as exc:  # noqa: BLE001
                log.warning("VOD check failed for %s: %s", s["channel"], exc)

    @vod_watcher.before_loop
    async def _wait_ready_vods(self) -> None:
        await self.wait_until_ready()

    async def check_new_vods(self, streamer: dict, seed_only: bool = False) -> int:
        vods = await asyncio.to_thread(platforms.list_vods, streamer["platform"], streamer["channel"], 5)
        queued = 0
        for vod in reversed(vods):  # oldest first
            if vod.get("live_status") == "is_live":
                continue  # still streaming; pick it up when it's finished
            exists = self.db.one("SELECT id FROM vods WHERE streamer_id=? AND vod_id=?", (streamer["id"], vod["id"]))
            if exists:
                continue
            status = "seen" if seed_only else "queued"
            row_id = self.db.execute(
                "INSERT INTO vods (streamer_id, vod_id, url, title, status, created_at) VALUES (?,?,?,?,?,?)",
                (streamer["id"], vod["id"], vod["url"], vod.get("title"), status, time.time()),
            )
            if not seed_only:
                cfg = self.db.guild_config(streamer["guild_id"])
                self.db.enqueue_job(streamer["guild_id"], "vod",
                                    {"url": vod["url"], "streamer_id": streamer["id"], "vod_row_id": row_id},
                                    status_channel_id=cfg.get("log_channel_id"))
                queued += 1
        return queued

    @tasks.loop(minutes=settings.clipper_poll_minutes)
    async def clipper_watcher(self) -> None:
        for account in self.db.all("SELECT * FROM clipper_accounts"):
            try:
                await self.check_clipper_account(account)
            except Exception as exc:  # noqa: BLE001
                log.warning("Clipper account check failed for %s: %s", account["url"], exc)

    @clipper_watcher.before_loop
    async def _wait_ready_clippers(self) -> None:
        await self.wait_until_ready()

    async def check_clipper_account(self, account: dict, seed_only: bool = False) -> int:
        posts = await asyncio.to_thread(platforms.list_recent_videos, account["url"], 15)
        cfg = self.db.guild_config(account["guild_id"])
        ch = await self.channel(cfg.get("clipper_feed_channel_id") or cfg.get("clips_channel_id"))
        streamer = self.db.one("SELECT * FROM streamers WHERE id=?", (account["streamer_id"],)) if account[
            "streamer_id"] else None
        new = 0
        for post in reversed(posts):
            if not post.get("id") or self.db.one(
                    "SELECT id FROM clipper_posts WHERE account_id=? AND post_id=?", (account["id"], post["id"])):
                continue
            self.db.execute(
                "INSERT INTO clipper_posts (account_id, post_id, url, title, views, created_at) VALUES (?,?,?,?,?,?)",
                (account["id"], post["id"], post["url"], post.get("title"), post.get("views"), time.time()),
            )
            if seed_only or ch is None:
                continue
            who = f" for **{streamer['display_name'] or streamer['channel']}**" if streamer else ""
            views = f" · {post['views']:,} views" if post.get("views") else ""
            await ch.send(f"📎 New clip from <{account['url']}>{who}{views}\n{post['url']}")
            new += 1
        return new

    @tasks.loop(hours=6)
    async def cleanup(self) -> None:
        cutoff = time.time() - settings.clip_retention_days * 86400
        for clip in self.db.all("SELECT * FROM clips WHERE created_at < ? AND file_path IS NOT NULL", (cutoff,)):
            for key in ("file_path", "preview_path"):
                if clip.get(key):
                    Path(clip[key]).unlink(missing_ok=True)
            self.db.execute("UPDATE clips SET file_path=NULL, preview_path=NULL WHERE id=?", (clip["id"],))


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ====================================================================== commands
def register_commands(bot: ClipperBot) -> None:
    db = bot.db
    admin = app_commands.default_permissions(manage_guild=True)

    async def streamer_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        rows = db.all("SELECT channel, platform FROM streamers WHERE guild_id=? AND channel LIKE ? LIMIT 25",
                      (interaction.guild_id, f"%{current}%"))
        return [app_commands.Choice(name=f"{r['channel']} ({r['platform']})", value=r["channel"]) for r in rows]

    # ---------------- /setup
    @bot.tree.command(name="setup", description="Choose where clips, clipper posts and job logs go")
    @admin
    @app_commands.describe(clips_channel="Where generated clips are posted (the rater bot watches this)",
                           clipper_feed_channel="Where posts from streamers' own clippers are collected",
                           log_channel="Where job progress and errors are posted")
    async def setup(interaction: discord.Interaction, clips_channel: discord.TextChannel,
                    clipper_feed_channel: discord.TextChannel | None = None,
                    log_channel: discord.TextChannel | None = None) -> None:
        db.set_guild_config(interaction.guild_id, clips_channel_id=clips_channel.id,
                            clipper_feed_channel_id=clipper_feed_channel.id if clipper_feed_channel else None,
                            log_channel_id=log_channel.id if log_channel else None)
        await interaction.response.send_message(
            f"Clips → {clips_channel.mention}"
            + (f"\nClipper feed → {clipper_feed_channel.mention}" if clipper_feed_channel else "")
            + (f"\nLogs → {log_channel.mention}" if log_channel else ""), ephemeral=True)

    # ---------------- /streamer
    streamer_group = app_commands.Group(name="streamer", description="Manage the streamers you clip",
                                        default_permissions=discord.Permissions(manage_guild=True))

    @streamer_group.command(name="add", description="Track a streamer and check if they allow clipping")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    @app_commands.describe(channel="Channel name, @handle or URL",
                           auto_clip="Automatically clip every new VOD (only if clipping is allowed)")
    async def streamer_add(interaction: discord.Interaction, platform: app_commands.Choice[str], channel: str,
                           auto_clip: bool = True) -> None:
        await interaction.response.defer(thinking=True)
        try:
            slug = platforms.parse_channel(platform.value, channel)
        except ValueError as exc:
            await interaction.followup.send(str(exc))
            return
        existing = db.find_streamer_by_channel(interaction.guild_id, platform.value, slug)
        if existing:
            await interaction.followup.send("Already tracked.", embed=permission_embed(existing))
            return
        result = await check_permission(platform.value, slug)
        streamer_id = db.execute(
            "INSERT INTO streamers (guild_id, platform, channel, display_name, permission, permission_source, "
            "permission_evidence, permission_checked_at, auto_clip, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (interaction.guild_id, platform.value, slug, slug, result.status, "auto", result.evidence, time.time(),
             int(auto_clip), time.time()),
        )
        streamer = db.one("SELECT * FROM streamers WHERE id=?", (streamer_id,))
        # Don't clip the back catalogue: only VODs that appear from now on are auto-clipped.
        try:
            await bot.check_new_vods(streamer, seed_only=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not list VODs for %s: %s", slug, exc)
        await interaction.followup.send(embed=permission_embed(streamer, result.sources))

    @streamer_group.command(name="remove", description="Stop tracking a streamer")
    @app_commands.autocomplete(name=streamer_autocomplete)
    async def streamer_remove(interaction: discord.Interaction, name: str) -> None:
        s = db.find_streamer(interaction.guild_id, name)
        if not s:
            await interaction.response.send_message("Not found.", ephemeral=True)
            return
        db.execute("DELETE FROM streamers WHERE id=?", (s["id"],))
        db.execute("DELETE FROM vods WHERE streamer_id=?", (s["id"],))
        db.execute("UPDATE clipper_accounts SET streamer_id=NULL WHERE streamer_id=?", (s["id"],))
        await interaction.response.send_message(f"Removed {s['channel']}.")

    @streamer_group.command(name="list", description="List tracked streamers and their clipping status")
    async def streamer_list(interaction: discord.Interaction) -> None:
        rows = db.all("SELECT * FROM streamers WHERE guild_id=? ORDER BY channel", (interaction.guild_id,))
        if not rows:
            await interaction.response.send_message("No streamers yet. Use /streamer add.", ephemeral=True)
            return
        icon = {"allowed": "🟢", "denied": "🔴", "unknown": "🟠"}
        lines = [f"{icon.get(r['permission'], '⚪')} **{r['channel']}** ({r['platform']}) — {r['permission']}"
                 f"{' · auto' if r['auto_clip'] else ''}" for r in rows]
        await interaction.response.send_message("\n".join(lines)[:2000])

    @streamer_group.command(name="permission", description="Manually set whether a streamer may be clipped")
    @app_commands.autocomplete(name=streamer_autocomplete)
    @app_commands.choices(status=[app_commands.Choice(name=s, value=s) for s in ("allowed", "denied", "unknown")])
    @app_commands.describe(note="Where you confirmed it, e.g. 'Discord #rules says clipping is fine'")
    async def streamer_permission(interaction: discord.Interaction, name: str, status: app_commands.Choice[str],
                                  note: str | None = None) -> None:
        s = db.find_streamer(interaction.guild_id, name)
        if not s:
            await interaction.response.send_message("Not found.", ephemeral=True)
            return
        db.set_permission(s["id"], status.value, "manual",
                          f"Set by {interaction.user} — {note}" if note else f"Set by {interaction.user}")
        s = db.one("SELECT * FROM streamers WHERE id=?", (s["id"],))
        await interaction.response.send_message(embed=permission_embed(s))

    @streamer_group.command(name="recheck", description="Re-read a streamer's profile for clipping rules")
    @app_commands.autocomplete(name=streamer_autocomplete)
    async def streamer_recheck(interaction: discord.Interaction, name: str) -> None:
        s = db.find_streamer(interaction.guild_id, name)
        if not s:
            await interaction.response.send_message("Not found.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        result = await check_permission(s["platform"], s["channel"])
        if s["permission_source"] == "manual" and result.status == "unknown":
            await interaction.followup.send("Nothing new found; keeping your manual setting.", embed=permission_embed(s))
            return
        db.set_permission(s["id"], result.status, "auto", result.evidence)
        s = db.one("SELECT * FROM streamers WHERE id=?", (s["id"],))
        await interaction.followup.send(embed=permission_embed(s, result.sources))

    @streamer_group.command(name="autoclip", description="Turn automatic clipping of new VODs on/off")
    @app_commands.autocomplete(name=streamer_autocomplete)
    async def streamer_autoclip(interaction: discord.Interaction, name: str, enabled: bool) -> None:
        s = db.find_streamer(interaction.guild_id, name)
        if not s:
            await interaction.response.send_message("Not found.", ephemeral=True)
            return
        db.execute("UPDATE streamers SET auto_clip=? WHERE id=?", (int(enabled), s["id"]))
        await interaction.response.send_message(f"Auto-clip for {s['channel']}: {'on' if enabled else 'off'}")

    bot.tree.add_command(streamer_group)

    # ---------------- /clip
    clip_group = app_commands.Group(name="clip", description="Make clips from VODs",
                                    default_permissions=discord.Permissions(manage_guild=True))

    async def enqueue_vod(interaction: discord.Interaction, streamer: dict, url: str, count: int | None,
                          layout: str, subtitles: bool, cut_dead_air: bool) -> None:
        if streamer["permission"] != "allowed":
            await interaction.followup.send(
                f"Not clipping **{streamer['channel']}** — clipping status is **{streamer['permission']}**.",
                embed=permission_embed(streamer))
            return
        job_id = db.enqueue_job(interaction.guild_id, "vod",
                                {"url": url, "streamer_id": streamer["id"], "count": count, "layout": layout,
                                 "subtitles": subtitles, "tighten": cut_dead_air},
                                requested_by=interaction.user.id, status_channel_id=interaction.channel_id)
        ahead = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running') AND id < ?", (job_id,))
        await interaction.followup.send(f"Queued job **#{job_id}** for {url} "
                                        f"({ahead['n']} job(s) ahead). Progress will appear here.")

    @clip_group.command(name="vod", description="Clip a specific VOD (Twitch, Kick or YouTube URL)")
    @app_commands.choices(layout=LAYOUT_CHOICES)
    @app_commands.describe(count="How many clips to make", layout="auto = detect facecam",
                           subtitles="Burn in captions",
                           cut_dead_air="Jump-cut pauses where nothing is said or happening")
    async def clip_vod(interaction: discord.Interaction, url: str, count: app_commands.Range[int, 1, 10] | None = None,
                       layout: app_commands.Choice[str] | None = None, subtitles: bool = True,
                       cut_dead_air: bool = True) -> None:
        await interaction.response.defer(thinking=True)
        platform = platforms.detect_platform(url)
        if platform not in platforms.PLATFORMS:
            await interaction.followup.send("Only Twitch, Kick and YouTube VOD links are supported.")
            return
        try:
            info = await asyncio.to_thread(platforms.extract_info, url)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Couldn't read that VOD: {str(exc)[:1500]}")
            return
        slug = platforms.channel_from_info(platform, info)
        if not slug:
            await interaction.followup.send("Couldn't tell which channel this VOD belongs to.")
            return
        streamer = db.find_streamer_by_channel(interaction.guild_id, platform, slug)
        if not streamer:
            result = await check_permission(platform, slug)
            sid = db.execute(
                "INSERT INTO streamers (guild_id, platform, channel, display_name, permission, permission_source, "
                "permission_evidence, permission_checked_at, auto_clip, created_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
                (interaction.guild_id, platform, slug, info.get("uploader") or slug, result.status, "auto",
                 result.evidence, time.time(), time.time()),
            )
            streamer = db.one("SELECT * FROM streamers WHERE id=?", (sid,))
        await enqueue_vod(interaction, streamer, url, count, layout.value if layout else "auto", subtitles,
                          cut_dead_air)

    @clip_group.command(name="latest", description="Clip a tracked streamer's most recent finished VOD")
    @app_commands.autocomplete(streamer=streamer_autocomplete)
    @app_commands.choices(layout=LAYOUT_CHOICES)
    async def clip_latest(interaction: discord.Interaction, streamer: str,
                          count: app_commands.Range[int, 1, 10] | None = None,
                          layout: app_commands.Choice[str] | None = None, subtitles: bool = True,
                       cut_dead_air: bool = True) -> None:
        await interaction.response.defer(thinking=True)
        s = db.find_streamer(interaction.guild_id, streamer)
        if not s:
            await interaction.followup.send("Not found. Use /streamer add first.")
            return
        vods = await asyncio.to_thread(platforms.list_vods, s["platform"], s["channel"], 5)
        vods = [v for v in vods if v.get("live_status") != "is_live"]
        if not vods:
            await interaction.followup.send("No finished VODs found.")
            return
        await enqueue_vod(interaction, s, vods[0]["url"], count, layout.value if layout else "auto", subtitles,
                          cut_dead_air)

    @clip_group.command(name="jobs", description="Show the clip job queue")
    async def clip_jobs(interaction: discord.Interaction) -> None:
        rows = db.all("SELECT * FROM jobs WHERE guild_id=? ORDER BY id DESC LIMIT 10", (interaction.guild_id,))
        if not rows:
            await interaction.response.send_message("No jobs yet.", ephemeral=True)
            return
        icon = {"queued": "⏳", "running": "⚙️", "done": "✅", "failed": "❌"}
        lines = []
        for r in rows:
            url = json.loads(r["payload"]).get("url", "")
            detail = r["error"] if r["status"] == "failed" else (r["progress"] or "")
            lines.append(f"{icon.get(r['status'], '•')} **#{r['id']}** <{url}> — {detail[:150]}")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @clip_group.command(name="cancel", description="Cancel a queued job")
    async def clip_cancel(interaction: discord.Interaction, job_id: int) -> None:
        row = db.one("SELECT * FROM jobs WHERE id=? AND guild_id=?", (job_id, interaction.guild_id))
        if not row or row["status"] != "queued":
            await interaction.response.send_message("Only queued jobs can be cancelled.", ephemeral=True)
            return
        db.update_job(job_id, status="failed", error="cancelled")
        await interaction.response.send_message(f"Cancelled job #{job_id}.")

    bot.tree.add_command(clip_group)

    # ---------------- /clippers
    clippers_group = app_commands.Group(name="clippers", description="Collect posts from streamers' own clippers",
                                        default_permissions=discord.Permissions(manage_guild=True))

    @clippers_group.command(name="add", description="Watch a clipper's TikTok/YouTube/Instagram account")
    @app_commands.autocomplete(streamer=streamer_autocomplete)
    @app_commands.describe(url="Profile URL, e.g. https://www.tiktok.com/@someclipper",
                           streamer="Which tracked streamer they clip (optional)",
                           learn="Also let the rater bot learn from this account's high-view clips")
    async def clippers_add(interaction: discord.Interaction, url: str, streamer: str | None = None,
                           learn: bool = True) -> None:
        await interaction.response.defer(thinking=True)
        s = db.find_streamer(interaction.guild_id, streamer) if streamer else None
        if streamer and not s:
            await interaction.followup.send("Streamer not found.")
            return
        try:
            account_id = db.execute(
                "INSERT INTO clipper_accounts (guild_id, streamer_id, url, created_at) VALUES (?,?,?,?)",
                (interaction.guild_id, s["id"] if s else None, url.strip(), time.time()))
        except Exception:  # noqa: BLE001
            await interaction.followup.send("That account is already being watched.")
            return
        if learn:
            db.execute("INSERT OR IGNORE INTO reference_accounts (guild_id, url, min_views, created_at) VALUES (?,?,?,?)",
                       (interaction.guild_id, url.strip(), settings.rater_min_reference_views, time.time()))
        account = db.one("SELECT * FROM clipper_accounts WHERE id=?", (account_id,))
        try:
            await bot.check_clipper_account(account, seed_only=True)
            count = db.one("SELECT COUNT(*) AS n FROM clipper_posts WHERE account_id=?", (account_id,))["n"]
            await interaction.followup.send(f"Watching <{url}> ({count} existing posts found; only new ones will "
                                            f"be posted).")
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Added, but the first check failed: {str(exc)[:1000]}\n"
                                            "Instagram needs a cookies file (see README).")

    @clippers_group.command(name="remove", description="Stop watching a clipper account")
    async def clippers_remove(interaction: discord.Interaction, url: str) -> None:
        db.execute("DELETE FROM clipper_accounts WHERE guild_id=? AND url=?", (interaction.guild_id, url.strip()))
        await interaction.response.send_message("Removed (if it existed).")

    @clippers_group.command(name="list", description="List watched clipper accounts")
    async def clippers_list(interaction: discord.Interaction) -> None:
        rows = db.all("SELECT a.url, s.channel FROM clipper_accounts a LEFT JOIN streamers s ON s.id=a.streamer_id "
                      "WHERE a.guild_id=?", (interaction.guild_id,))
        text = "\n".join(f"• <{r['url']}>" + (f" → {r['channel']}" if r["channel"] else "") for r in rows)
        await interaction.response.send_message(text[:2000] or "None yet.", ephemeral=True)

    @clippers_group.command(name="check", description="Check all clipper accounts for new posts now")
    async def clippers_check(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        total = 0
        for account in db.all("SELECT * FROM clipper_accounts WHERE guild_id=?", (interaction.guild_id,)):
            try:
                total += await bot.check_clipper_account(account)
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(f"<{account['url']}> failed: {str(exc)[:300]}")
        await interaction.followup.send(f"Done — {total} new post(s).")

    bot.tree.add_command(clippers_group)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    if not settings.clipper_token:
        raise SystemExit("DISCORD_CLIPPER_TOKEN is not set")
    ClipperBot().run(settings.clipper_token, log_handler=None)
