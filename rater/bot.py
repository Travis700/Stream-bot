"""Rater bot: watches the clips channel and scores each clip 1-10 for short-form potential."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import traceback
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from shared import health, platforms
from shared.config import settings
from shared.db import Database, dumps

from . import learner, scorer

log = logging.getLogger("rater")

CLIP_FOOTER = re.compile(r"Clip #(\d+)")
MESSAGE_LINK = re.compile(r"discord\.com/channels/(\d+)/(\d+)/(\d+)")


def score_color(score: int) -> discord.Color:
    if score >= 8:
        return discord.Color.green()
    if score >= 5:
        return discord.Color.gold()
    return discord.Color.red()


def rating_embed(rating: dict, clip_id: int | None) -> discord.Embed:
    score = max(1, min(10, int(rating["score"])))
    hook = max(1, min(10, int(rating["hook_score"])))
    embed = discord.Embed(title=f"Rating: {score}/10  {'🔥' * (score >= 8)}", description=rating["verdict"][:1000],
                          color=score_color(score))
    embed.add_field(name="Hook (first 3s)", value=f"{hook}/10")
    embed.add_field(name="Predicted views", value=rating["predicted_views"][:100])
    embed.add_field(name="Best platform", value=rating["best_platform"].replace("_", " ").title())
    if rating["strengths"]:
        embed.add_field(name="✅ Strengths", value="\n".join(f"• {s}" for s in rating["strengths"][:5])[:1024],
                        inline=False)
    if rating["weaknesses"]:
        embed.add_field(name="⚠️ Weaknesses", value="\n".join(f"• {s}" for s in rating["weaknesses"][:5])[:1024],
                        inline=False)
    if rating["edit_suggestions"]:
        embed.add_field(name="✂️ Edit suggestions",
                        value="\n".join(f"• {s}" for s in rating["edit_suggestions"][:5])[:1024], inline=False)
    tags = " ".join(t if t.startswith("#") else f"#{t}" for t in rating["hashtags"][:8])
    embed.add_field(name="Caption", value=f"{rating['suggested_caption'][:800]}\n{tags}"[:1024], inline=False)
    if clip_id:
        embed.set_footer(text=f"Clip #{clip_id} · after posting, report real views with /outcome")
    return embed


class RaterBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # needed to see attachments/embeds in the clips channel
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = Database(settings.db_path)
        self.rate_lock = asyncio.Lock()
        register_commands(self)

    async def setup_hook(self) -> None:
        if settings.guild_id:
            guild = discord.Object(id=settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.reference_watcher.start()
        self.view_tracker.start()
        self.self_update.start()

    async def on_ready(self) -> None:
        log.info("Rater bot ready as %s", self.user)
        if not settings.llm_available("rating"):
            log.error("No AI configured for rating (provider %r); the rater cannot rate or learn.",
                      settings.provider_for("rating"))

    def is_clips_channel(self, channel_id: int) -> bool:
        return bool(self.db.one("SELECT 1 FROM guild_config WHERE clips_channel_id=?", (channel_id,)))

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user or not message.guild or not self.is_clips_channel(message.channel.id):
            return
        has_video = any((a.content_type or "").startswith("video/") for a in message.attachments)
        clip_id = self._clip_id(message)
        if clip_id or has_video:
            await self.rate_message(message)

    @staticmethod
    def _clip_id(message: discord.Message) -> int | None:
        for embed in message.embeds:
            if embed.footer and embed.footer.text:
                match = CLIP_FOOTER.search(embed.footer.text)
                if match:
                    return int(match.group(1))
        return None

    async def rate_message(self, message: discord.Message) -> discord.Embed | None:
        clip_id = self._clip_id(message)
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (clip_id,)) if clip_id else None
        work = settings.work_dir / f"rate_{message.id}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            async with self.rate_lock:
                words, title, streamer = None, "", None
                if clip and clip.get("file_path") and Path(clip["file_path"]).exists():
                    video = Path(clip["file_path"])  # same server: use the full-quality file directly
                    words = json.loads(clip["transcript"] or "null")
                    title = clip["title"] or ""
                    s = self.db.one("SELECT channel FROM streamers WHERE id=?", (clip["streamer_id"],))
                    streamer = s["channel"] if s else None
                else:
                    attachment = next((a for a in message.attachments
                                       if (a.content_type or "").startswith("video/")), None)
                    if attachment is None:
                        return None
                    video = work / attachment.filename
                    await attachment.save(video)
                    title = message.content[:200]
                await message.add_reaction("👀")
                rating = await scorer.rate_video(self.db, video, work / "frames", words, title, streamer)
            if clip:
                self.db.execute("UPDATE clips SET rating=?, rating_json=?, rating_synced=0 WHERE id=?",
                                (int(rating["score"]), dumps(rating), clip["id"]))
            embed = rating_embed(rating, clip["id"] if clip else None)
            await message.reply(embed=embed, mention_author=False)
            await message.remove_reaction("👀", self.user)
            return embed
        except Exception as exc:  # noqa: BLE001
            log.error("Rating failed: %s", traceback.format_exc())
            await message.reply(f"Couldn't rate this clip: {str(exc)[:500]}", mention_author=False)
            return None
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @tasks.loop(hours=6)
    async def view_tracker(self) -> None:
        """Re-check view counts of clips you posted, so ratings calibrate on real results."""
        cutoff = time.time() - settings.view_tracking_days * 86400
        for post in self.db.all("SELECT * FROM clip_posts WHERE created_at > ? AND url LIKE 'http%'", (cutoff,)):
            try:
                await update_post_views(self.db, post)
            except Exception as exc:  # noqa: BLE001
                log.warning("View check failed for %s: %s", post["url"], exc)
            await asyncio.sleep(5)  # be gentle with the platforms

    @view_tracker.before_loop
    async def _wait_ready_views(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(hours=24)
    async def self_update(self) -> None:
        """Restart (the container entrypoint then updates yt-dlp) when a new yt-dlp is out and we're idle."""
        if self.self_update.current_loop == 0:
            return
        if not await health.ytdlp_update_available():
            return
        while self.rate_lock.locked():
            await asyncio.sleep(60)
        log.info("Newer yt-dlp available; restarting to update")
        await self.close()

    @tasks.loop(minutes=settings.reference_poll_minutes)
    async def reference_watcher(self) -> None:
        if not settings.llm_available("rating"):
            return
        for account in self.db.all("SELECT * FROM reference_accounts"):
            try:
                added = await learner.poll_reference_account(self.db, account)
                if added:
                    log.info("Learned %d new reference clips from %s", added, account["url"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Reference account %s failed: %s", account["url"], exc)

    @reference_watcher.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()


async def update_post_views(db: Database, post: dict) -> int | None:
    info = await asyncio.to_thread(platforms.extract_info, post["url"])
    views = info.get("view_count")
    db.execute("UPDATE clip_posts SET views=?, likes=?, last_checked=? WHERE id=?",
               (views, info.get("like_count"), time.time(), post["id"]))
    best = db.one("SELECT MAX(views) AS v FROM clip_posts WHERE clip_id=?", (post["clip_id"],))
    if best and best["v"] is not None:
        db.execute("UPDATE clips SET actual_views=? WHERE id=?", (best["v"], post["clip_id"]))
    return views


def register_commands(bot: RaterBot) -> None:
    db = bot.db
    admin = app_commands.default_permissions(manage_guild=True)

    @bot.tree.command(name="rate", description="Rate a clip: a message link from the clips channel, or a video file")
    @admin
    async def rate(interaction: discord.Interaction, message_link: str | None = None,
                   video: discord.Attachment | None = None) -> None:
        await interaction.response.defer(thinking=True)
        if message_link:
            match = MESSAGE_LINK.search(message_link)
            if not match:
                await interaction.followup.send("That isn't a Discord message link.")
                return
            channel = bot.get_channel(int(match.group(2))) or await bot.fetch_channel(int(match.group(2)))
            message = await channel.fetch_message(int(match.group(3)))  # type: ignore[union-attr]
            embed = await bot.rate_message(message)
            await interaction.followup.send("Rated — see the reply on that message." if embed else
                                            "No clip found in that message.")
            return
        if video is None or not (video.content_type or "").startswith("video/"):
            await interaction.followup.send("Give me a message link or attach a video.")
            return
        work = settings.work_dir / f"rate_cmd_{interaction.id}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            path = work / video.filename
            await video.save(path)
            async with bot.rate_lock:
                rating = await scorer.rate_video(db, path, work / "frames")
            await interaction.followup.send(embed=rating_embed(rating, None))
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Couldn't rate it: {str(exc)[:500]}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @bot.tree.command(name="posted", description="Link a posted TikTok/Reel/Short to a clip; views are tracked automatically")
    @admin
    @app_commands.describe(clip_id="The number in 'Clip #…' under the clip", url="Link to the post")
    async def posted(interaction: discord.Interaction, clip_id: int, url: str) -> None:
        await interaction.response.defer(thinking=True)
        clip = db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            await interaction.followup.send("Clip not found.")
            return
        db.execute("INSERT OR IGNORE INTO clip_posts (clip_id, platform, url, created_at) VALUES (?,?,?,?)",
                   (clip_id, platforms.detect_platform(url), url.strip(), time.time()))
        post = db.one("SELECT * FROM clip_posts WHERE clip_id=? AND url=?", (clip_id, url.strip()))
        try:
            views = await update_post_views(db, post)
            now = f" It has {views:,} views right now." if views is not None else ""
        except Exception as exc:  # noqa: BLE001
            now = f" (Couldn't read its views yet: {str(exc)[:200]})"
        await interaction.followup.send(f"Tracking views for clip #{clip_id} on <{url}> for the next "
                                        f"{settings.view_tracking_days} days.{now}")

    @bot.tree.command(name="outcome", description="Tell the rater how many views a posted clip actually got")
    @admin
    async def outcome(interaction: discord.Interaction, clip_id: int, views: app_commands.Range[int, 0]) -> None:
        clip = db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            await interaction.response.send_message("Clip not found.", ephemeral=True)
            return
        db.execute("UPDATE clips SET actual_views=? WHERE id=?", (views, clip_id))
        await interaction.response.send_message(
            f"Saved: clip #{clip_id} was rated {clip['rating'] or '?'}/10 and got {views:,} views. "
            "Future ratings will use this to calibrate.")

    learn = app_commands.Group(name="learn", description="Teach the rater what viral streamer clips look like",
                               default_permissions=discord.Permissions(manage_guild=True))

    @learn.command(name="clip", description="Add one viral TikTok/Instagram/Shorts clip to the reference library")
    async def learn_clip(interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(thinking=True)
        row = await learner.add_reference(db, url.strip())
        if row["status"] != "ready":
            await interaction.followup.send(f"Couldn't learn from it ({row['status']}): {row.get('error') or ''}"[:1900])
            return
        a = learner.analysis_of(row)
        await interaction.followup.send(
            f"Learned from <{url}> ({row['views'] or 0:,} views)\n**Hook:** {a.get('hook')}\n"
            f"**Why it worked:** {a.get('why_it_worked')}\n**Format:** {a.get('format_notes')}"[:1900])

    @learn.command(name="account", description="Keep learning from an account's high-view clips")
    @app_commands.describe(url="TikTok / Instagram / YouTube profile URL", min_views="Only learn from clips above this")
    async def learn_account(interaction: discord.Interaction, url: str,
                            min_views: app_commands.Range[int, 0] = 100_000) -> None:
        await interaction.response.defer(thinking=True)
        db.execute("INSERT INTO reference_accounts (guild_id, url, min_views, created_at) VALUES (?,?,?,?) "
                   "ON CONFLICT(guild_id, url) DO UPDATE SET min_views=excluded.min_views",
                   (interaction.guild_id, url.strip(), min_views, time.time()))
        account = db.one("SELECT * FROM reference_accounts WHERE guild_id=? AND url=?", (interaction.guild_id, url.strip()))
        try:
            added = await learner.poll_reference_account(db, account)
            await interaction.followup.send(f"Watching <{url}> (≥{min_views:,} views). Learned {added} clip(s) now; "
                                            f"more every {settings.reference_poll_minutes} min.")
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Saved, but the first check failed: {str(exc)[:1000]}")

    @learn.command(name="accounts", description="List accounts the rater learns from")
    async def learn_accounts(interaction: discord.Interaction) -> None:
        rows = db.all("SELECT * FROM reference_accounts WHERE guild_id=?", (interaction.guild_id,))
        text = "\n".join(f"• <{r['url']}> (≥{r['min_views']:,} views)" for r in rows)
        await interaction.response.send_message(text[:2000] or "None yet.", ephemeral=True)

    @learn.command(name="forget", description="Stop learning from an account")
    async def learn_forget(interaction: discord.Interaction, url: str) -> None:
        db.execute("DELETE FROM reference_accounts WHERE guild_id=? AND url=?", (interaction.guild_id, url.strip()))
        await interaction.response.send_message("Removed (if it existed).", ephemeral=True)

    @learn.command(name="library", description="Show the top clips the rater has learned from")
    async def learn_library(interaction: discord.Interaction) -> None:
        rows = db.all("SELECT * FROM reference_clips WHERE status='ready' ORDER BY views DESC LIMIT 10")
        lines = [f"Library: {learner.library_summary(db)}"]
        for r in rows:
            a = learner.analysis_of(r)
            lines.append(f"• **{r['views'] or 0:,}** views — {a.get('streamer', '?')}: {a.get('hook', '')[:120]} <{r['url']}>")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    bot.tree.add_command(learn)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    if not settings.rater_token:
        raise SystemExit("DISCORD_RATER_TOKEN is not set")
    RaterBot().run(settings.rater_token, log_handler=None)
