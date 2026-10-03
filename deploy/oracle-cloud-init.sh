#!/bin/bash
# No-terminal setup: paste this whole file into Oracle's "Create compute instance" page under
#   Show advanced options > Management > Initialization script > Paste cloud-init script
# after filling in the values below. The server sets itself up on first boot (~15 minutes),
# then the bots come online in Discord by themselves.

DISCORD_CLIPPER_TOKEN="paste-clipper-bot-token-here"
DISCORD_RATER_TOKEN="paste-rater-bot-token-here"
DISCORD_GUILD_ID="paste-your-discord-server-id-here"
# Only if your GitHub repo is private: a read-only access token (see README). Leave empty if public.
GITHUB_TOKEN=""

# ---- nothing to change below ----
export DISCORD_CLIPPER_TOKEN DISCORD_RATER_TOKEN DISCORD_GUILD_ID GITHUB_TOKEN
export HOME=/home/ubuntu DIR=/home/ubuntu/stream-bot
AUTH=()
if [ -n "$GITHUB_TOKEN" ]; then AUTH=(-H "Authorization: token $GITHUB_TOKEN"); fi
curl -fsSL "${AUTH[@]}" \
    https://raw.githubusercontent.com/Travis700/Stream-bot/claude/clip-and-rater-bots/deploy/install.sh \
    -o /tmp/install.sh
bash /tmp/install.sh > /var/log/stream-bot-install.log 2>&1
chown -R ubuntu:ubuntu /home/ubuntu/stream-bot
