#!/usr/bin/env bash
# One-command installer for an Ubuntu server (Oracle Cloud free ARM VM or any other).
#
#   curl -fsSL https://raw.githubusercontent.com/Travis700/Stream-bot/claude/clip-and-rater-bots/deploy/install.sh | bash
#
# It asks for your two Discord bot tokens and your server ID, then does everything else:
# installs Docker, downloads the bots, writes the settings, opens the firewall, starts
# everything and prints the links to invite the bots to your Discord server.
#
# Non-interactive use (e.g. Oracle's "initialization script"): set the variables first:
#   DISCORD_CLIPPER_TOKEN=... DISCORD_RATER_TOKEN=... DISCORD_GUILD_ID=... bash install.sh
#
# Private GitHub repo? Create a read-only token (GitHub > Settings > Developer settings >
# Fine-grained tokens, "Contents: Read-only" on this repo) and download/run with it:
#   export GITHUB_TOKEN=github_pat_...
#   curl -fsSL -H "Authorization: token $GITHUB_TOKEN" <raw link to this file> | bash
set -euo pipefail

REPO="${REPO:-https://github.com/Travis700/Stream-bot.git}"
BRANCH="${BRANCH:-claude/clip-and-rater-bots}"
DIR="${DIR:-$HOME/stream-bot}"
PORT="${FILE_SERVER_PORT:-8080}"
DRY_RUN="${DRY_RUN:-0}"   # 1 = only write .env and print what would happen (used by the tests)

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
run() { if [ "$DRY_RUN" = "1" ]; then echo "[dry-run] $*"; else "$@"; fi; }
SUDO=""; if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

ask() {  # ask VAR "question" — keeps an existing value from the environment
    local var="$1" question="$2" value="${!1:-}"
    while [ -z "$value" ]; do
        if [ -r /dev/tty ]; then
            read -r -p "$question: " value </dev/tty || true
        else
            echo "Missing $var (no terminal to ask on). Set it as an environment variable." >&2
            exit 1
        fi
    done
    printf -v "$var" '%s' "$value"
}

# The first part of a Discord bot token is the bot's ID in base64, which is all an invite link needs.
client_id_from_token() {
    local part="${1%%.*}"
    while [ $(( ${#part} % 4 )) -ne 0 ]; do part="${part}="; done
    printf '%s' "$part" | tr '_-' '/+' | base64 -d 2>/dev/null || true
}

invite_link() {
    # View Channel, Send Messages, Embed Links, Attach Files, Read History, Add Reactions
    echo "https://discord.com/oauth2/authorize?client_id=$1&scope=bot%20applications.commands&permissions=117824"
}

if [ "$DRY_RUN" != "1" ]; then
    say "Installing Docker and git"
    if ! command -v docker >/dev/null 2>&1; then
        curl -fsSL https://get.docker.com | $SUDO sh
    fi
    if ! command -v git >/dev/null 2>&1; then
        $SUDO apt-get update -q && $SUDO apt-get install -y -q git
    fi
    $SUDO usermod -aG docker "${SUDO_USER:-${USER:-ubuntu}}" 2>/dev/null || true

    say "Downloading the bots"
    if [ -d "$DIR/.git" ]; then
        git -C "$DIR" fetch -q origin "$BRANCH" && git -C "$DIR" checkout -q "$BRANCH" && git -C "$DIR" pull -q
    else
        CLONE_URL="$REPO"
        if [ -n "${GITHUB_TOKEN:-}" ]; then
            CLONE_URL="https://x-access-token:${GITHUB_TOKEN}@${REPO#https://}"
        fi
        if ! git clone -q --branch "$BRANCH" "$CLONE_URL" "$DIR"; then
            echo "Couldn't download $REPO. The repo is private: either make it public on GitHub, or set" \
                 "GITHUB_TOKEN to a read-only access token (see the top of this script), then run it again." >&2
            exit 1
        fi
    fi
fi
mkdir -p "$DIR"
cd "$DIR"

if [ ! -f .env ]; then
    say "Settings"
    echo "Paste the values from the Discord developer portal (they stay on this server)."
    ask DISCORD_CLIPPER_TOKEN "Clipper bot token"
    ask DISCORD_RATER_TOKEN "Rater bot token"
    ask DISCORD_GUILD_ID "Your Discord server ID (right-click the server icon > Copy Server ID)"
    PUBLIC_IP="${PUBLIC_IP:-$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)}"
    if [ -f .env.example ]; then cp .env.example .env; else : > .env; fi
    set_env() {  # set_env KEY VALUE  (replace the line or append it)
        local key="$1" value="$2"
        if grep -q "^${key}=" .env; then
            local escaped
            escaped=$(printf '%s' "$value" | sed -e 's/[\/&|]/\\&/g')
            sed -i "s|^${key}=.*|${key}=${escaped}|" .env
        else
            printf '%s=%s\n' "$key" "$value" >> .env
        fi
    }
    set_env DISCORD_CLIPPER_TOKEN "$DISCORD_CLIPPER_TOKEN"
    set_env DISCORD_RATER_TOKEN "$DISCORD_RATER_TOKEN"
    set_env DISCORD_GUILD_ID "$DISCORD_GUILD_ID"
    if [ -n "$PUBLIC_IP" ]; then set_env PUBLIC_BASE_URL "http://$PUBLIC_IP:$PORT"; fi
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then set_env ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"; fi
    chmod 600 .env
else
    say "Keeping your existing settings (.env)"
fi

if [ "$DRY_RUN" != "1" ]; then
    say "Opening port $PORT for clip download links"
    if ! $SUDO iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
        $SUDO iptables -I INPUT 1 -p tcp --dport "$PORT" -j ACCEPT || true
        $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -q iptables-persistent >/dev/null 2>&1 || true
        $SUDO netfilter-persistent save >/dev/null 2>&1 || true
    fi

    if [ ! -f /swapfile ]; then
        say "Adding swap (helps the first build)"
        $SUDO fallocate -l 4G /swapfile && $SUDO chmod 600 /swapfile && $SUDO mkswap /swapfile >/dev/null \
            && $SUDO swapon /swapfile && echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null || true
    fi

    say "Building and starting the bots (first time takes 5-15 minutes)"
    $SUDO docker compose up -d --build
fi

CLIPPER_ID=$(client_id_from_token "$(grep '^DISCORD_CLIPPER_TOKEN=' .env | cut -d= -f2-)")
RATER_ID=$(client_id_from_token "$(grep '^DISCORD_RATER_TOKEN=' .env | cut -d= -f2-)")

cat <<EOF

$(printf '\033[1;32m')All done!$(printf '\033[0m')

1. Invite both bots to your Discord server (open each link, pick your server, click Authorize):
   Clipper: $( [ -n "$CLIPPER_ID" ] && invite_link "$CLIPPER_ID" || echo "(couldn't read the token, use the developer portal)")
   Rater:   $( [ -n "$RATER_ID" ] && invite_link "$RATER_ID" || echo "(couldn't read the token, use the developer portal)")

2. In Discord type:  /setup   then   /selftest

Optional: for the "Download full quality" buttons, add an Ingress rule for TCP $PORT in the Oracle
console (Networking > Virtual Cloud Networks > your VCN > Security Lists).

Useful later:  cd $DIR && sudo docker compose logs -f      (see what the bots are doing)
               curl -fsSL https://raw.githubusercontent.com/Travis700/Stream-bot/claude/clip-and-rater-bots/deploy/install.sh | bash   (update)
EOF
