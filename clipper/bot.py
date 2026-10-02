"""Clipper bot: VODs -> vertical clips posted to Discord, plus a feed of the streamers' own clippers."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
import traceback

import discord
from discord import app_commands
from discord.ext import tasks

from shared import health, locks, platforms
from shared.config import settings
from shared.db import Database
from shared.permissions import check_permission

from . import fileserver, pipeline, posting, selftest, stats
from .render import LAYOUTS

log = logging.getLogger("clipper")

PLATFORM_CHOICES = [app_commands.Choice(name=p.title(), value=p) for p in platforms.PLATFORMS]
LAYOUT_CHOICES = [app_commands.Choice(name=n, value=n) for n in LAYOUTS]
PERMISSION_COLORS = {"allowed": discord.Color.green(), "denied": discord.Color.red(),
                     "unknown": discord.Color.orange()}
PLATFORM_LABELS = {"tiktok": "TikTok", "instagram": "Instagram", "facebook": "Facebook"}
MAX_RETRIES = 2
RETRY_MINUTES = 15
BACKUPS_KEPT = 7


def permission_embed(streamer: dict, sources: list[str] | None = None) -> discord.Embed:
    status = streamer["permission"]
    embed = discord.Embed(
        title=f"{streamer['display_name'] or streamer['channel']} ({streamer['platform']})",
        url=platforms.channel_url(streamer["platform"], streamer["channel"]),
        color=PERMISSION_COLORS.get(status, discord.Color.greyple()),
    )
    embed.add_field(name="Clipping", value=f"**{status.upper()}** ({streamer['permission_source']})")
    embed.add_field(name="Auto-clip new VODs", value="on" if streamer["auto_clip"] else "off")
    embed.add_field(name="Layout", value=streamer.get("layout") or "auto")
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


# ====================================================================== clip buttons
class ClipButton(discord.ui.DynamicItem[discord.ui.Button], template=r"clip:(?P<action>[a-z_]+):(?P<id>\d+)"):
    """Buttons under each clip. Dynamic, so they keep working after the bot restarts."""

    STYLES = {
        "approve": ("Approve", "✅", discord.ButtonStyle.success),
        "discard": ("Discard", "🗑️", discord.ButtonStyle.danger),
        "shorter": ("Make shorter", "✂️", discord.ButtonStyle.secondary),
        "layout": ("Change layout", "🔄", discord.ButtonStyle.secondary),
        "post_tiktok": ("Post to TikTok", "📤", discord.ButtonStyle.primary),
        "post_instagram": ("Post to Instagram", "📤", discord.ButtonStyle.primary),
        "post_facebook": ("Post to Facebook", "📤", discord.ButtonStyle.primary),
    }

    def __init__(self, action: str, clip_id: int) -> None:
        label, emoji, style = self.STYLES.get(action, (action, None, discord.ButtonStyle.secondary))
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style,
                                           custom_id=f"clip:{action}:{clip_id}"))
        self.action = action
        self.clip_id = clip_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> "ClipButton":
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: ClipperBot = interaction.client  # type: ignore[assignment]
        await bot.handle_clip_action(interaction, self.action, self.clip_id)


class LayoutPicker(discord.ui.View):
    def __init__(self, bot: "ClipperBot", clip_id: int, current: str) -> None:
        super().__init__(timeout=300)
        self.bot, self.clip_id = bot, clip_id
        options = [discord.SelectOption(label=name, value=name, default=name == current,
                                        description={"split": "Facecam on top, gameplay below",
                                                     "fullcam": "Vertical crop following the face",
                                                     "fit": "Zoomed gameplay on blurred background"}[name])
                   for name in ("split", "fullcam", "fit")]
        select = discord.ui.Select(placeholder="Pick a layout", options=options)
        select.callback = self._picked  # type: ignore[method-assign]
        self.select = select
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        layout = self.select.values[0]
        job_id = self.bot.enqueue_rerender(interaction, self.clip_id, layout=layout)
        await interaction.response.edit_message(content=f"Re-rendering with **{layout}** layout (job #{job_id}).",
                                                view=None)


def clip_view(clip: dict, platforms_enabled: list[str]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    status = clip.get("status") or "new"
    if status == "new":
        for action in ("approve", "discard", "shorter", "layout"):
            view.add_item(ClipButton(action, clip["id"]))
    elif status == "approved":
        posted = {p for p in clip.get("_posted", [])}
        for platform in platforms_enabled:
            if platform not in posted:
                view.add_item(ClipButton(f"post_{platform}", clip["id"]))
        view.add_item(ClipButton("shorter", clip["id"]))
        view.add_item(ClipButton("layout", clip["id"]))
    if status != "discarded" and settings.public_base_url and clip.get("token"):
        view.add_item(discord.ui.Button(label="Download full quality",
                                        url=f"{settings.public_base_url}/c/{clip['token']}.mp4"))
    return view


# ====================================================================== bot
class ClipperBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = Database(settings.db_path)
        self._last_edit: dict[int, float] = {}
        self.job_running = False
        register_commands(self)

    async def setup_hook(self) -> None:
        self.db.requeue_interrupted_jobs()
        self.add_dynamic_items(ClipButton)
        await fileserver.start(self.db)
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
        self.self_update.start()
        self.backup_db.start()
        self.rating_sync.start()
        if settings.digest_hour_utc >= 0:
            self.daily_digest.start()

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

    def find_existing_job(self, guild_id: int, key: str) -> dict | None:
        """A queued, running or finished clip job for the same VOD (to avoid duplicate clips)."""
        return self.db.one(
            "SELECT * FROM jobs WHERE guild_id=? AND kind='vod' AND json_extract(payload, '$.vod_key')=? "
            "AND status IN ('queued','running','done') ORDER BY id DESC LIMIT 1", (guild_id, key))

    def enqueue_rerender(self, interaction: discord.Interaction, clip_id: int, layout: str | None = None,
                         shorter: bool = False) -> int:
        # Progress goes to the log channel (if set) so the clips channel stays clean.
        status_channel = self.db.guild_config(interaction.guild_id).get("log_channel_id") or interaction.channel_id
        return self.db.enqueue_job(interaction.guild_id, "rerender",
                                   {"clip_id": clip_id, "layout": layout, "shorter": shorter},
                                   requested_by=interaction.user.id, status_channel_id=status_channel)

    # ------------------------------------------------------------------ jobs
    @tasks.loop(seconds=5)
    async def job_worker(self) -> None:
        job = self.db.next_job()
        if not job:
            return
        self.job_running = True
        if job["status_channel_id"] and not job["status_message_id"]:
            ch = await self.channel(job["status_channel_id"])
            if ch:
                msg = await ch.send(f"**Job #{job['id']}** — starting…")
                job["status_message_id"] = msg.id
                self.db.update_job(job["id"], status_message_id=msg.id)
        try:
            started = time.monotonic()
            progress = lambda t: self.set_job_status(job, t)  # noqa: E731
            if job["kind"] == "rerender":
                clip_ids = await pipeline.rerender_clip(self.db, job, progress)
            else:
                clip_ids = await pipeline.process_vod(self.db, job, progress)
            took = pipeline._fmt_len(time.monotonic() - started)
            for clip_id in clip_ids:
                await self.post_clip(clip_id)
            self.db.update_job(job["id"], status="done")
            if job["payload"].get("vod_row_id"):
                self.db.execute("UPDATE vods SET status='done' WHERE id=?", (job["payload"]["vod_row_id"],))
            await self.set_job_status(job, f"✅ done in {took} — {len(clip_ids)} clip(s) posted.", force=True)
        except Exception as exc:  # noqa: BLE001
            log.error("Job %s failed: %s", job["id"], traceback.format_exc())
            message = pipeline.explain_error(exc)
            attempts = (job.get("attempts") or 0) + 1
            if pipeline.is_transient(exc) and attempts <= MAX_RETRIES:
                delay = RETRY_MINUTES * attempts
                self.db.update_job(job["id"], status="queued", attempts=attempts,
                                   not_before=time.time() + delay * 60, error=message[:2000])
                await self.set_job_status(job, f"⚠️ network problem, retrying in {delay} min "
                                               f"(try {attempts}/{MAX_RETRIES}): {message[:300]}", force=True)
                return
            self.db.update_job(job["id"], status="failed", error=message[:2000])
            if job["payload"].get("vod_row_id"):
                self.db.execute("UPDATE vods SET status='failed' WHERE id=?", (job["payload"]["vod_row_id"],))
            await self.set_job_status(job, f"❌ failed: {message[:1500]}", force=True)
            if not job["status_message_id"]:
                await self.log_to_guild(job["guild_id"], f"Job #{job['id']} failed: {message[:1500]}")
        finally:
            self.job_running = False

    @job_worker.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()

    def clip_embed(self, clip: dict) -> discord.Embed:
        streamer = self.db.one("SELECT * FROM streamers WHERE id=?", (clip["streamer_id"],)) or {}
        length = clip.get("duration") or (clip["end_s"] - clip["start_s"])
        platform = streamer.get("platform", "")
        status = clip.get("status") or "new"
        color = {"approved": discord.Color.green(), "discarded": discord.Color.dark_grey()}.get(
            status, discord.Color.purple())
        title = ("🗑️ " if status == "discarded" else "") + (clip["title"] or "Clip")
        embed = discord.Embed(title=title[:256], description=(clip["reason"] or "")[:1500], color=color)
        if clip.get("rating") is not None:
            rating = json.loads(clip.get("rating_json") or "{}")
            predicted = f" · predicted {rating['predicted_views']}" if rating.get("predicted_views") else ""
            embed.add_field(name="Rater", value=f"**{clip['rating']}/10**{predicted}", inline=False)
        if clip.get("hook_text"):
            embed.add_field(name="On-screen hook", value=clip["hook_text"][:200], inline=False)
        embed.add_field(name="Streamer", value=streamer.get("display_name") or streamer.get("channel", "?"))
        embed.add_field(name="Length", value=f"{int(length // 60)}:{int(length % 60):02d}")
        embed.add_field(name="Layout", value=clip["layout"])
        embed.add_field(name="Source", value=f"[VOD @ {_hms(clip['start_s'])}]"
                        f"({platforms.timestamp_url(platform, clip['vod_url'], clip['start_s'])})", inline=False)
        posts = self.db.all("SELECT * FROM clip_posts WHERE clip_id=?", (clip["id"],))
        if posts:
            embed.add_field(name="Posted", value="\n".join(
                f"{PLATFORM_LABELS.get(p['platform'], p['platform'])}: "
                + (p["url"] if (p["url"] or "").startswith("http") else "uploaded")
                + (f" · {p['views']:,} views" if p.get("views") else "") for p in posts)[:1024], inline=False)
        footer = f"Clip #{clip['id']}"
        if clip.get("parent_clip_id"):
            footer += f" · re-edit of #{clip['parent_clip_id']}"
        embed.set_footer(text=footer)
        return embed

    def clip_view_for(self, clip: dict) -> discord.ui.View:
        clip = dict(clip)
        clip["_posted"] = [p["platform"] for p in self.db.all("SELECT platform FROM clip_posts WHERE clip_id=?",
                                                               (clip["id"],))]
        return clip_view(clip, posting.enabled_platforms(self.db))

    async def post_clip(self, clip_id: int) -> None:
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            return
        cfg = self.db.guild_config(clip["guild_id"])
        ch = await self.channel(cfg.get("clips_channel_id"))
        if ch is None:
            log.warning("No clips channel configured for guild %s", clip["guild_id"])
            return
        guild = self.get_guild(clip["guild_id"])
        limit = guild.filesize_limit if guild else 10 * 1024 * 1024
        attachment_path = await asyncio.to_thread(pipeline.make_preview_for, self.db, clip, limit - 200_000)
        embed = self.clip_embed(clip)
        files = []
        if attachment_path:
            files.append(discord.File(attachment_path, filename=f"clip_{clip['id']}.mp4"))
        elif not settings.public_base_url:
            embed.add_field(name="⚠️", value="Clip is too big to upload and PUBLIC_BASE_URL is not set, so "
                                            "there is no download link.", inline=False)
        reference = None
        if clip.get("parent_clip_id"):
            parent = self.db.one("SELECT channel_id, message_id FROM clips WHERE id=?", (clip["parent_clip_id"],))
            if parent and parent["message_id"] and parent["channel_id"] == ch.id:  # type: ignore[union-attr]
                reference = discord.MessageReference(message_id=parent["message_id"], channel_id=parent["channel_id"],
                                                     fail_if_not_exists=False)
        msg = await ch.send(embed=embed, files=files, view=self.clip_view_for(clip), reference=reference)
        self.db.execute("UPDATE clips SET channel_id=?, message_id=? WHERE id=?", (ch.id, msg.id, clip_id))

    async def refresh_clip_message(self, clip_id: int, interaction: discord.Interaction | None = None,
                                   remove_video: bool = False) -> None:
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip or not clip.get("message_id"):
            return
        kwargs: dict = {"embed": self.clip_embed(clip), "view": self.clip_view_for(clip)}
        if remove_video:
            kwargs["attachments"] = []
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(**kwargs)
            return
        ch = await self.channel(clip["channel_id"])
        if ch is not None:
            try:
                await ch.get_partial_message(clip["message_id"]).edit(**kwargs)  # type: ignore[union-attr]
            except discord.HTTPException:
                pass

    async def handle_clip_action(self, interaction: discord.Interaction, action: str, clip_id: int) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
            await interaction.response.send_message("Only admins (Manage Server) can use these buttons.",
                                                    ephemeral=True)
            return
        clip = self.db.one("SELECT * FROM clips WHERE id=? AND guild_id=?", (clip_id, interaction.guild_id))
        if not clip:
            await interaction.response.send_message("Clip not found.", ephemeral=True)
            return
        if action == "approve":
            self.db.execute("UPDATE clips SET status='approved' WHERE id=?", (clip_id,))
            await self.refresh_clip_message(clip_id, interaction)
            await interaction.followup.send(f"✅ Clip #{clip_id} approved by {member.mention}.")
        elif action == "discard":
            self.db.execute("UPDATE clips SET status='discarded' WHERE id=?", (clip_id,))
            await asyncio.to_thread(pipeline.delete_clip_files, self.db, clip)
            await self.refresh_clip_message(clip_id, interaction, remove_video=True)
        elif action == "shorter":
            job_id = self.enqueue_rerender(interaction, clip_id, shorter=True)
            await interaction.response.send_message(f"✂️ Making a shorter version of clip #{clip_id} (job #{job_id}).")
        elif action == "layout":
            await interaction.response.send_message("Which layout?", view=LayoutPicker(self, clip_id, clip["layout"]),
                                                    ephemeral=True)
        elif action.startswith("post_"):
            platform = action.removeprefix("post_")
            if platform not in posting.enabled_platforms(self.db):
                await interaction.response.send_message(f"{PLATFORM_LABELS.get(platform, platform)} isn't set up.",
                                                        ephemeral=True)
                return
            await interaction.response.send_message(
                f"📤 Posting clip #{clip_id} to {PLATFORM_LABELS[platform]}… (this can take a few minutes)")
            asyncio.create_task(self._post_in_background(interaction, clip, platform))

    async def _post_in_background(self, interaction: discord.Interaction, clip: dict, platform: str) -> None:
        clip["rating_json"] = (self.db.one("SELECT rating_json FROM clips WHERE id=?", (clip["id"],)) or {}).get(
            "rating_json")
        try:
            result = await posting.post_clip(self.db, clip, platform)
            where = result.get("url") or result.get("note") or "done"
            await interaction.followup.send(f"✅ Posted clip #{clip['id']} to {PLATFORM_LABELS[platform]}: {where}")
            await self.refresh_clip_message(clip["id"])
        except Exception as exc:  # noqa: BLE001
            log.error("Posting failed: %s", traceback.format_exc())
            await interaction.followup.send(f"❌ Posting to {PLATFORM_LABELS[platform]} failed: {str(exc)[:1500]}")

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
            key = pipeline.vod_key(vod["url"])
            already = self.find_existing_job(streamer["guild_id"], key)
            status = "seen" if (seed_only or already) else "queued"
            row_id = self.db.execute(
                "INSERT INTO vods (streamer_id, vod_id, url, title, status, created_at) VALUES (?,?,?,?,?,?)",
                (streamer["id"], vod["id"], vod["url"], vod.get("title"), status, time.time()),
            )
            if status == "queued":
                cfg = self.db.guild_config(streamer["guild_id"])
                self.db.enqueue_job(streamer["guild_id"], "vod",
                                    {"url": vod["url"], "vod_key": key, "streamer_id": streamer["id"],
                                     "vod_row_id": row_id},
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
            await asyncio.to_thread(pipeline.delete_clip_files, self.db, clip)

    @tasks.loop(time=dt.time(hour=max(0, min(23, settings.digest_hour_utc)), tzinfo=dt.timezone.utc))
    async def daily_digest(self) -> None:
        for cfg in self.db.all_guild_configs():
            if not cfg.get("log_channel_id"):
                continue
            embed = digest_embed(self.db, cfg["guild_id"])
            if embed is not None:
                ch = await self.channel(cfg["log_channel_id"])
                if ch is not None:
                    await ch.send(embed=embed)

    @daily_digest.before_loop
    async def _wait_ready_digest(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def rating_sync(self) -> None:
        """Show new ratings on the clip messages and auto-approve high scorers."""
        rows = self.db.all("SELECT * FROM clips WHERE rating IS NOT NULL AND rating_synced=0 "
                           "AND message_id IS NOT NULL LIMIT 20")
        for clip in rows:
            auto = (settings.auto_approve_rating and clip["rating"] >= settings.auto_approve_rating
                    and (clip.get("status") or "new") == "new")
            if auto:
                self.db.execute("UPDATE clips SET status='approved' WHERE id=?", (clip["id"],))
            self.db.execute("UPDATE clips SET rating_synced=1 WHERE id=?", (clip["id"],))
            await self.refresh_clip_message(clip["id"])
            if auto:
                await self.log_to_guild(clip["guild_id"], f"✅ Auto-approved clip #{clip['id']} "
                                                          f"(rated {clip['rating']}/10).")

    @rating_sync.before_loop
    async def _wait_ready_ratings(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(hours=24)
    async def backup_db(self) -> None:
        """Daily copy of the database to data/backups (last 7 kept)."""
        folder = settings.data_dir / "backups"
        dest = folder / f"streambot-{time.strftime('%Y%m%d')}.sqlite3"
        await asyncio.to_thread(self.db.backup, dest)
        for old in sorted(folder.glob("streambot-*.sqlite3"))[:-BACKUPS_KEPT]:
            old.unlink(missing_ok=True)

    @tasks.loop(hours=24)
    async def self_update(self) -> None:
        """Restart (container entrypoint then updates yt-dlp) when a new yt-dlp is out and we're idle."""
        if self.self_update.current_loop == 0:
            return  # just started; the entrypoint already updated
        newer = await health.ytdlp_update_available()
        if not newer:
            return
        while self.job_running or self.db.one("SELECT 1 FROM jobs WHERE status='running'"):
            await asyncio.sleep(60)
        log.info("yt-dlp %s is available; restarting to update", newer)
        await self.close()


def digest_embed(db: Database, guild_id: int) -> discord.Embed | None:
    d = stats.digest(db, guild_id)
    if d.empty:
        return None
    embed = discord.Embed(title="📊 Daily summary (last 24h)", color=discord.Color.blurple())
    embed.add_field(name="Clips", value=f"{d.clips_made} made · {d.approved} approved · {d.discarded} discarded")
    if d.avg_rating is not None:
        embed.add_field(name="Average rating", value=f"{d.avg_rating:.1f}/10")
    if d.best_clip:
        link = stats.message_link(guild_id, d.best_clip)
        title = d.best_clip["title"] or f"Clip #{d.best_clip['id']}"
        embed.add_field(name="Best clip", inline=False,
                        value=f"{d.best_clip['rating']}/10 — " + (f"[{title}]({link})" if link else title))
    if d.views_tracked:
        top = d.top_post
        embed.add_field(name="Views on posted clips", inline=False,
                        value=f"{d.views_tracked:,} total · best: {top['views']:,} on <{top['url']}>")
    todo = []
    if d.awaiting_review:
        todo.append(f"{d.awaiting_review} clip(s) waiting for ✅/🗑️")
    if d.unknown_streamers:
        todo.append(f"Decide clipping permission for: {', '.join(d.unknown_streamers[:10])}")
    if d.jobs_failed:
        todo.append(f"{d.jobs_failed} job(s) failed — see /clip jobs")
    if todo:
        embed.add_field(name="To do", value="\n".join(f"• {t}" for t in todo), inline=False)
    return embed


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

    # ---------------- /status
    @bot.tree.command(name="status", description="Check that everything is working (sites, AI, disk, queue)")
    @admin
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        sites, ai = await asyncio.gather(health.check_sites(), health.check_ai())
        free, total, clips_gb = await asyncio.to_thread(health.disk_report)
        newer = await health.ytdlp_update_available()
        ok = lambda good: "🟢" if good else "🔴"  # noqa: E731
        embed = discord.Embed(title="Bot status", color=discord.Color.blurple())
        embed.add_field(name="Sites (reachable ≠ downloads work)", inline=False,
                        value="\n".join(f"{ok(g)} {n}: {d}" for n, g, d in sites))
        embed.add_field(name="AI", inline=False, value="\n".join(f"{ok(g)} {n}: {d}" for n, g, d in ai) or "—")
        cookies = bool(settings.ytdlp_cookies) and os.path.exists(settings.ytdlp_cookies)
        embed.add_field(name="Downloads", inline=False, value=(
            f"yt-dlp {health.ytdlp_version()}" + (f" (update {newer} available, auto-restart pending)" if newer else "")
            + f"\nCookies file: {'loaded' if cookies else 'none'}"))
        embed.add_field(name="Disk", value=f"{ok(free > 15)} {free:.0f} GB free of {total:.0f} GB "
                                           f"(clips use {clips_gb:.1f} GB)", inline=False)
        counts = {r["status"]: r["n"] for r in db.all(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE guild_id=? GROUP BY status", (interaction.guild_id,))}
        embed.add_field(name="Jobs", inline=False, value=(
            f"⚙️ {counts.get('running', 0)} running · ⏳ {counts.get('queued', 0)} queued · "
            f"✅ {counts.get('done', 0)} done · ❌ {counts.get('failed', 0)} failed"
            + f"\nCPU: {'busy (a heavy task is running)' if locks.is_busy() else 'idle'}"))
        fails = db.all("SELECT id, error FROM jobs WHERE guild_id=? AND status='failed' AND error<>'cancelled' "
                       "ORDER BY id DESC LIMIT 3", (interaction.guild_id,))
        if fails:
            embed.add_field(name="Recent failures", inline=False,
                            value="\n".join(f"#{f['id']}: {(f['error'] or '')[:250]}" for f in fails)[:1024])
        week = time.time() - 7 * 86400
        stats = db.one("SELECT COUNT(*) AS made, SUM(rating IS NOT NULL) AS rated, SUM(status='approved') AS approved "
                       "FROM clips WHERE guild_id=? AND created_at>?", (interaction.guild_id, week))
        embed.add_field(name="Last 7 days", inline=False,
                        value=f"{stats['made'] or 0} clips made · {stats['rated'] or 0} rated · "
                              f"{stats['approved'] or 0} approved")
        enabled = posting.enabled_platforms(db)
        embed.add_field(name="Auto-posting", inline=False,
                        value=", ".join(PLATFORM_LABELS[p] for p in enabled) or "not set up (optional)")
        embed.add_field(name="Setup checklist", inline=False, value=setup_checklist(interaction.guild_id))
        await interaction.followup.send(embed=embed, ephemeral=True)

    def setup_checklist(guild_id: int) -> str:
        cfg = db.guild_config(guild_id)
        allowed = db.one("SELECT COUNT(*) AS n FROM streamers WHERE guild_id=? AND permission='allowed'", (guild_id,))
        unknown = db.one("SELECT COUNT(*) AS n FROM streamers WHERE guild_id=? AND permission='unknown'", (guild_id,))
        items = [
            (bool(cfg.get("clips_channel_id")), "Clips channel set (`/setup`)"),
            (bool(cfg.get("log_channel_id")), "Log channel set (optional, keeps progress messages out of #clips)"),
            (allowed["n"] > 0, f"{allowed['n']} streamer(s) allowed to clip"
                               + (f" · {unknown['n']} waiting for your decision" if unknown["n"] else "")),
            (bool(settings.public_base_url), "PUBLIC_BASE_URL set (full-quality download links)"),
            (settings.llm_available() and settings.llm_available("rating"), "AI configured for clipping and rating"),
        ]
        return "\n".join(f"{'✅' if done else '⬜'} {text}" for done, text in items)

    # ---------------- /top
    @bot.tree.command(name="top", description="Best clips by real views, and how well the rater predicted them")
    @admin
    async def top(interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30) -> None:
        rows, accuracy = stats.top_clips(db, interaction.guild_id, days)
        if not rows:
            await interaction.response.send_message(
                f"No view data from the last {days} days yet. Link posted clips with `/posted` (rater bot) or "
                f"use the auto-post buttons.", ephemeral=True)
            return
        lines = []
        for i, r in enumerate(rows, 1):
            link = stats.message_link(interaction.guild_id, r)
            title = (r["title"] or f"Clip #{r['id']}")[:60]
            rated = f"rated {r['rating']}/10" if r.get("rating") is not None else "not rated"
            lines.append(f"**{i}.** {f'[{title}]({link})' if link else title} — **{r['actual_views']:,}** views "
                         f"({rated})")
        embed = discord.Embed(title=f"🏆 Top clips, last {days} days", description="\n".join(lines)[:4000],
                              color=discord.Color.gold())
        embed.add_field(name="Rater accuracy", value=accuracy)
        await interaction.response.send_message(embed=embed)

    # ---------------- /selftest
    @bot.tree.command(name="selftest", description="Make a test clip on the server to check every step works")
    @admin
    async def selftest_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "🧪 Running the self-test: making a 20-second test stream, transcribing it, asking the AI, and "
            "rendering a clip. The first run downloads the speech model, so it can take a few minutes…")
        limit = (interaction.guild.filesize_limit if interaction.guild else 10 * 1024 * 1024) - 200_000
        result = await selftest.run(limit)
        embed = discord.Embed(title="Self-test " + ("passed ✅" if result.ok else "found problems ❌"),
                              color=discord.Color.green() if result.ok else discord.Color.red())
        for step in result.steps:
            embed.add_field(name=f"{'✅' if step.ok else '❌'} {step.name} ({step.seconds:.0f}s)",
                            value=step.detail[:1000] or "—", inline=False)
        if not result.ok:
            embed.set_footer(text="Paste the ❌ lines to whoever is helping you set this up.")
        files = [discord.File(result.video, filename="selftest_clip.mp4")] if result.video else []
        await interaction.followup.send(embed=embed, files=files)

    # ---------------- /connect
    @bot.tree.command(name="connect", description="Connect an account for auto-posting")
    @admin
    @app_commands.choices(platform=[app_commands.Choice(name="TikTok", value="tiktok")])
    async def connect(interaction: discord.Interaction, platform: app_commands.Choice[str]) -> None:
        if not (settings.tiktok_client_key and settings.tiktok_client_secret and settings.public_base_url):
            await interaction.response.send_message(
                "Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and an https PUBLIC_BASE_URL first (see README).",
                ephemeral=True)
            return
        await interaction.response.send_message(
            f"Open this link, log in to the TikTok account you post from and approve the app (link works for 15 "
            f"min):\n{posting.tiktok_authorize_url()}", ephemeral=True)

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
                 f"{' · auto' if r['auto_clip'] else ''}{' · ' + r['layout'] if r.get('layout') else ''}"
                 for r in rows]
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

    @streamer_group.command(name="layout", description="Always use this layout for a streamer's clips")
    @app_commands.autocomplete(name=streamer_autocomplete)
    @app_commands.choices(layout=LAYOUT_CHOICES)
    @app_commands.describe(layout="auto = detect the facecam every time")
    async def streamer_layout(interaction: discord.Interaction, name: str, layout: app_commands.Choice[str]) -> None:
        s = db.find_streamer(interaction.guild_id, name)
        if not s:
            await interaction.response.send_message("Not found.", ephemeral=True)
            return
        db.execute("UPDATE streamers SET layout=? WHERE id=?",
                   (None if layout.value == "auto" else layout.value, s["id"]))
        await interaction.response.send_message(f"{s['channel']}'s clips will use the **{layout.value}** layout.")

    bot.tree.add_command(streamer_group)

    # ---------------- /clip
    clip_group = app_commands.Group(name="clip", description="Make clips from VODs",
                                    default_permissions=discord.Permissions(manage_guild=True))

    async def enqueue_vod(interaction: discord.Interaction, streamer: dict, url: str, count: int | None,
                          layout: str, subtitles: bool, cut_dead_air: bool, force: bool) -> None:
        if streamer["permission"] != "allowed":
            await interaction.followup.send(
                f"Not clipping **{streamer['channel']}** — clipping status is **{streamer['permission']}**.",
                embed=permission_embed(streamer))
            return
        key = pipeline.vod_key(url)
        existing = bot.find_existing_job(interaction.guild_id, key)
        if existing and not force:
            what = {"queued": "is already queued", "running": "is being clipped right now",
                    "done": "was already clipped"}[existing["status"]]
            await interaction.followup.send(f"This VOD {what} (job #{existing['id']}). "
                                            f"Run the command again with `force:True` to clip it again anyway.")
            return
        job_id = db.enqueue_job(interaction.guild_id, "vod",
                                {"url": url, "vod_key": key, "streamer_id": streamer["id"], "count": count,
                                 "layout": layout, "subtitles": subtitles, "tighten": cut_dead_air},
                                requested_by=interaction.user.id, status_channel_id=interaction.channel_id)
        ahead = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running') AND id < ?", (job_id,))
        await interaction.followup.send(f"Queued job **#{job_id}** for {url} "
                                        f"({ahead['n']} job(s) ahead). Progress will appear here.")

    @clip_group.command(name="vod", description="Clip a specific VOD (Twitch, Kick or YouTube URL)")
    @app_commands.choices(layout=LAYOUT_CHOICES)
    @app_commands.describe(count="How many clips to make", layout="auto = streamer's saved layout or detect facecam",
                           subtitles="Burn in captions",
                           cut_dead_air="Jump-cut pauses where nothing is said or happening",
                           force="Clip it even if this VOD was already clipped")
    async def clip_vod(interaction: discord.Interaction, url: str, count: app_commands.Range[int, 1, 10] | None = None,
                       layout: app_commands.Choice[str] | None = None, subtitles: bool = True,
                       cut_dead_air: bool = True, force: bool = False) -> None:
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
                          cut_dead_air, force)

    @clip_group.command(name="latest", description="Clip a tracked streamer's most recent finished VOD")
    @app_commands.autocomplete(streamer=streamer_autocomplete)
    @app_commands.choices(layout=LAYOUT_CHOICES)
    async def clip_latest(interaction: discord.Interaction, streamer: str,
                          count: app_commands.Range[int, 1, 10] | None = None,
                          layout: app_commands.Choice[str] | None = None, subtitles: bool = True,
                          cut_dead_air: bool = True, force: bool = False) -> None:
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
                          cut_dead_air, force)

    @clip_group.command(name="jobs", description="Show the clip job queue")
    async def clip_jobs(interaction: discord.Interaction) -> None:
        rows = db.all("SELECT * FROM jobs WHERE guild_id=? ORDER BY id DESC LIMIT 10", (interaction.guild_id,))
        if not rows:
            await interaction.response.send_message("No jobs yet.", ephemeral=True)
            return
        icon = {"queued": "⏳", "running": "⚙️", "done": "✅", "failed": "❌"}
        lines = []
        for r in rows:
            payload = json.loads(r["payload"])
            what = f"<{payload['url']}>" if payload.get("url") else f"re-edit of clip #{payload.get('clip_id')}"
            detail = r["error"] if r["status"] == "failed" else (r["progress"] or "")
            lines.append(f"{icon.get(r['status'], '•')} **#{r['id']}** {what} — {(detail or '')[:150]}")
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
