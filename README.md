# Stream Bot

Two Discord bots that run on a cloud server (built for Oracle Cloud's free ARM VM). Nothing runs on your PC and no GPU is needed. You control everything with slash commands in your Discord server.

| Bot | What it does |
|---|---|
| **Clipper** | Watches streamers' Twitch / Kick / YouTube VODs, finds the best moments, and cuts them into 1–2 minute **vertical (9:16)** clips. It moves the facecam so it doesn't cover the game and burns in TikTok-style captions. It posts the clips to your clips channel, checks whether each streamer allows clipping, and collects new posts from the streamers' own clipper accounts. |
| **Rater** | Watches the clips channel and rates every clip **1–10** for how well it will do on TikTok / Reels / Shorts. It learns from high-view clips of streamers on TikTok and Instagram, and from the real view counts you report back. |

## How it works

```
VOD ──► download audio only ──► loudness "hype" curve ──┐
    └─► chat replay (Twitch/YouTube) ──► chat-spike curve ─┴─► top moments
                                                            │ transcribe only those (CPU Whisper)
                                                            ▼
                                    the AI picks and trims the best 1–2 min clips
                                                            │
     download just those minutes of video ◄─────────────────┘
        │
        ▼
 cut dead air ──► find facecam (OpenCV) ──► 1080×1920 layout ──► burn captions ──► post to #clips
                                                                      │
                                       Rater bot ◄────────────────────┘
          frames + transcript + learned viral examples ──► AI ──► 1–10 rating, fixes, caption
```

* **CPU only.** Speech-to-text uses `faster-whisper` (int8 on CPU), face detection uses OpenCV, and video uses ffmpeg/x264. Only the hype moments get transcribed, so a 6-hour VOD doesn't need 6 hours of transcription.
* **Chat as a signal.** On Twitch and YouTube the bot reads the VOD's chat replay. Chat suddenly spamming "KEKW", "LMAO", "💀" or "CLIP IT" marks a moment even when the streamer stays quiet. Chat counts alongside loudness, and the AI also sees what chat spammed for each moment. Kick has no usable chat replay, so Kick VODs use audio only.
* **Viewer clips (Twitch).** If viewers already clipped moments from the VOD on Twitch, those moments get a big boost, more for clips with more views, and the AI is told about them. A moment viewers chose to clip is the strongest sign it's good. This uses Twitch's unofficial web API; if it stops working, the bot just skips this step.
* **1–2 minute clips with no slow middle.** The AI is told to keep clips tight and use the shortest length that has both the setup and the payoff. Then pauses of 1.5s or more, where nobody is talking *and* the audio is quiet, are jump-cut out. Loud game moments are never cut, and a clip is never trimmed below 1 minute, since TikTok only pays for videos over 1 minute. Turn this off with `cut_dead_air:False` on `/clip` or `CUT_DEAD_AIR=0`.
* **Facecam handling.** The bot samples frames and finds a face that stays in the same spot, which is the webcam overlay. It then snaps to the overlay's border.
  * `split` layout (gaming): facecam on top, gameplay below. The gameplay crop is moved away from the webcam if the webcam would cover it.
  * `fullcam` layout (Just Chatting / IRL): a 9:16 crop that **pans smoothly to follow the streamer** as they move. The pan is speed-limited so it never jerks, and it stays still when they sit still.
  * `fit` layout (no facecam found): zoomed gameplay over a blurred background.
  * You can force any layout with `layout:` on `/clip`.
* **Captions.** 1–3 words at a time, the current word highlighted in yellow, placed on the seam between cam and game.
* **Hook text.** For the first 4 seconds a short line like "HE DID NOT SEE THIS COMING" appears in a white box. The AI writes it when it picks the clip. Change the time with `HOOK_TEXT_SECONDS`, or set it to `0` to turn it off.
* **Slur filter.** Slurs are bleeped (`BLEEP_MODE=beep`) or muted (`mute`) and shown as `N****` in the captions, because TikTok and Instagram often limit the reach of clips that contain them. Normal swearing is left alone; add your own words with `BLEEP_WORDS=word1,word2`.
* **Review in Discord.** Every clip gets buttons: ✅ **Approve**, 🗑️ **Discard** (deletes the files), ✂️ **Make shorter** (about 30% shorter, keeping the liveliest part), 🔄 **Change layout**. Re-edits reuse the saved source video, so nothing is downloaded again, and the new version is posted as a reply. Only admins can press the buttons.
* **Ratings on the clip.** When the rater scores a clip, the score shows on the clip's own post. Set `AUTO_APPROVE_RATING=8` to approve clips rated 8+ automatically (off by default).
* **Daily summary.** Every day at `DIGEST_HOUR_UTC` (default 9:00 UTC), the log channel gets a summary: clips made and approved, average rating, the best clip, total views on posted clips, and a to-do list (clips waiting for review, streamers needing a permission decision, failed jobs).
* **No duplicates.** A VOD that's already queued or clipped isn't clipped again; use `force:True` to override.
* **Shares the CPU fairly.** Transcription, rendering and the local AI never run at the same time across the two bots, so neither slows the other down.
* **Retries network failures.** A job that fails because of a network blip or a site timeout is retried automatically, up to 2 more times, 15 and 30 minutes later. Errors you need to fix yourself (subscriber-only VOD, YouTube asking for cookies, disk full) are explained in plain English instead.
* **Daily backups.** The database (streamers, clips, ratings, view history) is copied to `data/backups/` every day, and the last 7 copies are kept.
* **Keeps itself updated.** yt-dlp (the downloader) updates every time the bots start. Once a day, if a newer version is out and nothing is running, the bots restart themselves to pick it up.
* **Clipping permission.** The bot reads the streamer's Twitch bio and panels, Kick bio, or YouTube channel description and looks for rules like *"no clipping"* or *"clips will be DMCA'd"* versus *"feel free to clip"* or *"clipping program"*. If the text is unclear, the AI reads it, but its answer is only used when it can quote the profile word for word. **Streamers marked `unknown` or `denied` are never clipped.** You approve them yourself with `/streamer permission`. Check their Discord rules and socials too, because many streamers only post clipping rules there.

## Discord commands

**Clipper bot** (admin-only; needs *Manage Server*)

| Command | |
|---|---|
| `/help` | Quick-start guide inside Discord. |
| `/setup clips_channel [clipper_feed_channel] [log_channel]` | Where things get posted. Run this first. |
| `/top [days]` | Your best clips by real views, next to what the rater predicted, plus a **rater accuracy** score. This shows whether the ratings are worth trusting yet. |
| `/selftest` | Makes a 20-second test clip on the server with a synthetic voice and runs every step (speech-to-text, face detector, AI, cuts, captions, hook text, rendering), then posts the clip. **Run this right after deploying.** |
| `/status` | Health check: can the server reach each site, is the AI running, disk space, job queue, recent errors. **Run this first if something isn't working.** |
| `/streamer add platform channel [auto_clip]` | Track a streamer and run the clipping-permission check. New VODs are clipped automatically if allowed. |
| `/streamer list` · `remove` · `recheck` · `autoclip` | Manage tracked streamers. |
| `/streamer permission name allowed\|denied\|unknown [note]` | Your manual decision overrides the auto check. |
| `/streamer layout name layout` | Always use this layout for a streamer (if facecam detection gets them wrong). |
| `/clip vod url [count] [layout] [subtitles] [cut_dead_air] [force]` | Clip a specific VOD. |
| `/clip latest streamer` | Clip the newest finished VOD. |
| `/clip jobs` · `/clip cancel id` | Show the queue / cancel a queued job. |
| `/clippers add url [streamer] [learn]` | Watch a streamer's own clipper (TikTok/YouTube/Instagram profile). New posts go to the feed channel. With `learn`, the rater also learns from that account's big clips. |
| `/clippers list` · `remove` · `check` | |
| `/connect tiktok` | Log in to TikTok for auto-posting (optional, see below). |

**Rater bot** (admin-only)

| Command | |
|---|---|
| *(automatic)* | Every clip posted in the clips channel gets a rating reply. Videos you upload there yourself are rated too. Clips posted while the rater was offline are picked up within 30 minutes. |
| `/rate [message_link] [video]` | Rate a specific message or an uploaded video. |
| `/posted clip_id url` | Link the TikTok/Reel/Short you posted. The bot checks its views every 6 hours for 14 days and uses them to calibrate future ratings. Posts made with the auto-post buttons are tracked automatically when the platform gives back a link. |
| `/outcome clip_id views` | Enter views by hand instead. |
| `/learn clip url` | Add one viral TikTok/IG/Shorts clip to the reference library. |
| `/learn account url [min_views]` | Keep learning from an account's clips above `min_views`. |
| `/learn accounts` · `forget` · `library` | |

## Setup (about 20 minutes, no coding)

The bot does nearly everything itself. You do three things that need your own accounts.

### 1. Create the two Discord bots (5 min)
1. Go to <https://discord.com/developers/applications> → **New Application** → name it "Clipper". Open **Bot** → **Reset Token** → copy the token somewhere safe.
2. Do the same for "Rater". On the Rater's **Bot** page, also turn on **Message Content Intent**, so it can see the videos it rates.
3. In Discord: *User Settings → Advanced →* turn on **Developer Mode**. Then right-click your server icon → **Copy Server ID**.

You now have three values: two tokens and a server ID. You don't need to invite the bots yet; the installer gives you the invite links.

### 2. Create the free Oracle server (10 min)
Oracle Cloud console → *Compute → Instances → Create instance*:
- **Image:** Ubuntu 24.04
- **Shape:** VM.Standard.A1.Flex (Ampere), 4 OCPU / 24 GB RAM. This is covered by *Always Free*.
- **Boot volume:** 100 GB or more

Then pick **one** of these:

**Option A, no terminal at all:** before clicking Create, open *Show advanced options → Management → Initialization script → Paste cloud-init script*. Paste the contents of [`deploy/oracle-cloud-init.sh`](deploy/oracle-cloud-init.sh) with your three values filled in at the top. The server installs everything by itself on first boot, which takes about 15 minutes.

**Option B, one command:** create the server, connect with *Cloud Shell* or SSH, and run:
```bash
curl -fsSL https://raw.githubusercontent.com/Travis700/Stream-bot/claude/clip-and-rater-bots/deploy/install.sh | bash
```
It asks for your three values, then installs Docker, downloads the bots, writes the settings, opens the firewall and starts everything. At the end it prints the bot invite links.

> **Private GitHub repo?** Both options need to download the code. Either make the repo public (*GitHub → repo Settings → Danger Zone → Change visibility*; no secrets are stored in it), or create a read-only token (*GitHub → Settings → Developer settings → Fine-grained tokens*, access to this repo, *Contents: Read-only*). For Option A, put the token in `GITHUB_TOKEN=""`. For Option B, run:
> `export GITHUB_TOKEN=<token>; curl -fsSL -H "Authorization: token $GITHUB_TOKEN" https://raw.githubusercontent.com/Travis700/Stream-bot/claude/clip-and-rater-bots/deploy/install.sh | bash`

### 3. Invite the bots and start (2 min)
1. Open each invite link and pick your server. Option B prints the links. For Option A, the link is `https://discord.com/oauth2/authorize?client_id=<Application ID>&scope=bot%20applications.commands&permissions=117824`, where the Application ID is on each app's *General Information* page in the developer portal.
2. In Discord: `/setup` (pick a clips channel and a log channel), then `/selftest`, then `/streamer add`. `/help` explains the rest.

**Optional:** for the "Download full quality" buttons, add an Ingress rule for **TCP 8080** in the Oracle console (*Networking → Virtual Cloud Networks → your VCN → Security Lists*). Without it, you still get the preview video in Discord.

**Updating later:** run the Option B command again. It keeps your settings.

### The AI (free by default)
The AI picks clip moments, reads unclear clipping rules, and powers the rater. Set it in `~/stream-bot/.env`:

| `LLM_PROVIDER` | Cost | What you get |
|---|---|---|
| `ollama` *(default)* | **$0** | Free open models (Qwen 2.5 7B for text, Qwen 2.5-VL 7B for looking at frames) running on your Oracle server. They download automatically the first time (~10 GB total). Slower (each AI step can take a few minutes on CPU) and less accurate, especially the 1–10 ratings. |
| `anthropic` | Paid per use | Claude: fast and much better judgement. Needs an API key from <https://console.anthropic.com/>. |
| `none` | $0 | No AI. Clips are picked by loudness + amount of talking; the rater can't rate. |

You can mix them: keep everything free but rate with Claude by setting `LLM_PROVIDER=ollama`, `RATER_LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`. After editing `.env`, run `cd ~/stream-bot && sudo docker compose up -d`.

### Auto-posting (optional)
After you approve a clip, buttons appear to post it straight to each platform you've set up. Each one needs a developer app from that platform, so this is the most setup-heavy part. Skip it if you're happy downloading and posting by hand.

You'll need **HTTPS** for `PUBLIC_BASE_URL`, because TikTok only redirects logins to https and Meta fetches the video from it. The free way is a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) pointing at port 8080. Another option is a free DuckDNS domain with Caddy in front.

**TikTok**
1. Create an app at <https://developers.tiktok.com/>. Add the *Login Kit* and *Content Posting API* (Direct Post) products and the `video.publish` scope.
2. Set the redirect URI to `https://<your PUBLIC_BASE_URL host>/oauth/tiktok`.
3. Put `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` in `.env`, restart, then run `/connect tiktok` in Discord.
4. Until TikTok audits your app, posts are **private** (`TIKTOK_PRIVACY=SELF_ONLY`). After approval, set `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE`. TikTok doesn't return a post link, so use `/posted` once it's public to track views.

**Instagram Reels / Facebook Reels**
1. You need a Facebook Page, and for Instagram, an Instagram *professional* account linked to that Page.
2. Create a Meta app at <https://developers.facebook.com/> with the `instagram_content_publish`, `pages_manage_posts` and `pages_read_engagement` permissions. Generate a **long-lived Page access token**.
3. Set `META_PAGE_ACCESS_TOKEN`, `FACEBOOK_PAGE_ID` and/or `INSTAGRAM_USER_ID` in `.env`.

### Cookies (Instagram, and often YouTube)
Instagram won't serve videos without a logged-in session. YouTube often blocks cloud-server IPs with "Sign in to confirm you're not a bot". Export a `cookies.txt` from a browser where you're logged in, using a cookies.txt export extension. **Use a throwaway account**, because the platforms can ban accounts used for scraping. Then put it in `./data/cookies.txt` and set `YTDLP_COOKIES=/data/cookies.txt`.

## Things to know

* **Speed.** Everything runs on CPU. A long VOD takes a while: downloading audio, scanning it, transcribing ~12 moments, then rendering each clip with x264. Jobs run one at a time and post progress to Discord. To trade quality for speed, set `WHISPER_MODEL=base` and `RENDER_PRESET=superfast`.
* **Discord's 10 MB upload limit.** A 1–2 minute 1080p clip is usually bigger than that, so Discord gets a smaller 540p preview and the full-quality file is served from the VM (`PUBLIC_BASE_URL`). Files (and the saved source videos used for re-edits) are deleted after `CLIP_RETENTION_DAYS`. `/status` shows how much disk they use.
* **AI costs.** The default free local AI costs nothing but shares the server's 4 CPU cores with transcription and rendering, so jobs take longer. If you switch to Claude: clip selection, permission checks, rating and learning call `claude-opus-5-5` by default, roughly a few cents per clip rated or reference learned. `CLAUDE_MODEL=claude-sonnet-5-5` is about half the price. Requests opt into Anthropic's server-side refusal fallback, so a false-positive safety decline is retried on a fallback model instead of failing the job.
* **TikTok / Instagram scraping.** Collecting clipper posts and learning from viral clips uses yt-dlp. It can break when those sites change, and it's against their terms of service. yt-dlp updates itself automatically (see above).
* **Permission check is best-effort.** It can only read what's in the public profile. You're still responsible for following each streamer's rules. That's why `unknown` never auto-clips.
* **Auto-posting is untested against the live platforms.** It's written to TikTok's and Meta's published APIs, but it can only be tried once your developer apps exist. If a post fails, the bot replies with the platform's error message, so paste that to me.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest            # unit tests + ffmpeg render tests
DATA_DIR=./data python -m clipper   # or: python -m rater
```

Layout: `shared/` (config, SQLite, yt-dlp, AI providers, whisper, permissions), `clipper/` (highlights, facecam, tighten, censor, subtitles, render, pipeline, posting, bot), `rater/` (learner, scorer, bot).
