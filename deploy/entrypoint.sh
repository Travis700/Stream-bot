#!/bin/sh
# Update yt-dlp on every start: Twitch/Kick/YouTube/TikTok change their sites often and
# old yt-dlp versions stop working. The bots restart themselves daily when an update exists.
if pip install --no-cache-dir -q -U "yt-dlp[default,curl-cffi]" >/dev/null 2>&1; then
  echo "yt-dlp $(python -c 'import yt_dlp; print(yt_dlp.version.__version__)')"
else
  echo "yt-dlp update failed; using the installed version"
fi
exec "$@"
